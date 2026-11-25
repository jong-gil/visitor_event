import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from flask import Flask, make_response, render_template, request

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

# app.py가 있는 폴더 기준으로 event.db 경로 고정 (실행 위치가 달라도 같은 DB 사용)
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "event.db"

COOKIE_NAME = "device_event_id"
WIN_EVERY_N = 3


def get_connection() -> sqlite3.Connection:
    # isolation_level=None: BEGIN IMMEDIATE / COMMIT / ROLLBACK을 직접 명시적으로 제어
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000;")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_visitors INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                device_id TEXT PRIMARY KEY,
                visitor_num INTEGER NOT NULL,
                is_winner INTEGER NOT NULL,
                client_ip TEXT,
                user_agent TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO counter (id, total_visitors)
            VALUES (1, 0);
            """
        )
    finally:
        conn.close()


def get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


@app.route("/")
def index():
    raw_cookie = request.cookies.get(COOKIE_NAME)
    device_id = raw_cookie.strip() if raw_cookie else None

    now_iso = datetime.now(timezone.utc).isoformat()
    client_ip = get_client_ip()
    user_agent = request.headers.get("User-Agent", "")[:300]

    conn = get_connection()
    try:
        # 1) 쿠키가 있으면 먼저 DB에서 기존 참여 기록 조회
        if device_id:
            row = conn.execute(
                "SELECT visitor_num, is_winner FROM participants WHERE device_id = ?",
                (device_id,),
            ).fetchone()

            if row is not None:
                return render_template(
                    "already.html",
                    visitor_num=row["visitor_num"],
                    is_winner=bool(row["is_winner"]),
                )

        # 2) 신규 참여(또는 DB에 없는 쿠키): 트랜잭션으로 카운터 증가 및 참여 기록 저장
        conn.execute("BEGIN IMMEDIATE;")

        counter_row = conn.execute(
            "SELECT total_visitors FROM counter WHERE id = 1"
        ).fetchone()
        current_total = int(counter_row["total_visitors"]) if counter_row else 0
        new_visitor_num = current_total + 1
        is_winner = 1 if (new_visitor_num % WIN_EVERY_N == 0) else 0

        conn.execute(
            "UPDATE counter SET total_visitors = ? WHERE id = 1",
            (new_visitor_num,),
        )

        new_device_id = device_id or str(uuid4())
        conn.execute(
            """
            INSERT INTO participants (
                device_id, visitor_num, is_winner, client_ip, user_agent, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_device_id,
                new_visitor_num,
                is_winner,
                client_ip,
                user_agent,
                now_iso,
            ),
        )
        conn.execute("COMMIT;")

        template_name = "win.html" if is_winner else "lose.html"
        resp = make_response(
            render_template(template_name, visitor_num=new_visitor_num)
        )
        resp.set_cookie(
            COOKIE_NAME,
            new_device_id,
            max_age=60 * 60 * 24 * 30,
            path="/",
            httponly=True,
            samesite="Lax",
        )
        return resp

    except Exception:
        try:
            conn.execute("ROLLBACK;")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


init_db()

if __name__ == "__main__":
    app.run(debug=True)