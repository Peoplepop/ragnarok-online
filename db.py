import os
import sqlite3

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "game.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Gss#12345678"

DEFAULT_COUNTRIES = [
    {
        "name": "百鍊流金國", "element": "金",
        "description": "初始幸運值較高，閃避與命中俱佳",
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 15,
    },
    {
        "name": "翡翠靈木國", "element": "木",
        "description": "防禦與生命力驚人，減傷效果顯著",
        "hp_bonus": 20, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 15, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "蔚藍千泉國", "element": "水",
        "description": "身法飄逸，擅長先發制人與連續攻擊",
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 15, "luk_bonus": 0,
    },
    {
        "name": "紅蓮業火國", "element": "火",
        "description": "烈焰焚天，魔力與傷害兼備",
        "hp_bonus": 0, "mp_bonus": 10, "str_bonus": 15, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "萬物母育國", "element": "土",
        "description": "厚德載物，六圍均衡發展",
        "hp_bonus": 5, "mp_bonus": 5, "str_bonus": 5, "def_bonus": 5, "agi_bonus": 5, "luk_bonus": 5,
    },
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_is_admin_column(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")


def _ensure_session_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
    if "last_login_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
    if "is_online" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_online INTEGER NOT NULL DEFAULT 0")


def init_db():
    conn = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    _ensure_is_admin_column(conn)
    _ensure_session_columns(conn)
    conn.commit()
    conn.close()


def log_activity(conn, user_id, username, action, detail="", ip_address=None):
    conn.execute(
        """INSERT INTO activity_log (user_id, username, action, detail, ip_address)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, username, action, detail, ip_address),
    )


def seed_defaults():
    conn = get_db()

    if conn.execute("SELECT COUNT(*) AS c FROM countries").fetchone()["c"] == 0:
        for c in DEFAULT_COUNTRIES:
            conn.execute(
                """INSERT INTO countries
                   (name, element, description, hp_bonus, mp_bonus, str_bonus, def_bonus, agi_bonus, luk_bonus)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c["name"], c["element"], c["description"],
                    c["hp_bonus"], c["mp_bonus"], c["str_bonus"],
                    c["def_bonus"], c["agi_bonus"], c["luk_bonus"],
                ),
            )

    admin = conn.execute(
        "SELECT id FROM users WHERE username = ?", (DEFAULT_ADMIN_USERNAME,)
    ).fetchone()
    if admin is None:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
            (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
        )

    conn.commit()
    conn.close()
