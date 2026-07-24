import os
import sqlite3

from werkzeug.security import generate_password_hash

from map_layout import generate_layout

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "game.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Gss#12345678"

DEFAULT_COUNTRIES = [
    {
        "name": "百鍊流金國", "element": "金",
        "description": "初始幸運值較高，閃避與命中俱佳",
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 1,
    },
    {
        "name": "翡翠靈木國", "element": "木",
        "description": "防禦與生命力驚人，減傷效果顯著",
        "hp_bonus": 1, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 1, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "蔚藍千泉國", "element": "水",
        "description": "身法飄逸，擅長先發制人與連續攻擊",
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 1, "luk_bonus": 0,
    },
    {
        "name": "紅蓮業火國", "element": "火",
        "description": "烈焰焚天，魔力與傷害兼備",
        "hp_bonus": 0, "mp_bonus": 1, "str_bonus": 1, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "萬物母育國", "element": "土",
        "description": "厚德載物，六圍均衡發展",
        "hp_bonus": 1, "mp_bonus": 1, "str_bonus": 1, "def_bonus": 1, "agi_bonus": 1, "luk_bonus": 1,
    },
]

LEVEL_CAP = 1000

DEFAULT_HUNTING_GROUNDS = [
    {"tier": "beginner", "name": "初級打怪場", "min_level": 1, "max_level": 30, "monster_exp": 10},
    {"tier": "intermediate", "name": "中級打怪場", "min_level": 31, "max_level": 70, "monster_exp": 20},
    {"tier": "advanced", "name": "高級打怪場", "min_level": 71, "max_level": 120, "monster_exp": 40},
    {"tier": "ultimate", "name": "究級打怪場", "min_level": 121, "max_level": LEVEL_CAP, "monster_exp": 80},
]

DEFAULT_ITEMS = [
    {"shop_type": "weapon", "name": "木劍", "price": 50, "stat": "str", "stat_bonus": 2},
    {"shop_type": "weapon", "name": "鐵劍", "price": 200, "stat": "str", "stat_bonus": 8},
    {"shop_type": "weapon", "name": "秘銀劍", "price": 800, "stat": "str", "stat_bonus": 20},
    {"shop_type": "armor", "name": "布甲", "price": 50, "stat": "def", "stat_bonus": 2},
    {"shop_type": "armor", "name": "鐵甲", "price": 200, "stat": "def", "stat_bonus": 8},
    {"shop_type": "armor", "name": "龍鱗甲", "price": 800, "stat": "def", "stat_bonus": 20},
    {"shop_type": "accessory", "name": "銅戒指", "price": 50, "stat": "luk", "stat_bonus": 2},
    {"shop_type": "accessory", "name": "銀戒指", "price": 200, "stat": "luk", "stat_bonus": 8},
    {"shop_type": "accessory", "name": "金戒指", "price": 800, "stat": "luk", "stat_bonus": 20},
]

# Monster stats are tuned to be roughly fair for a character right at the
# start of that ground's level range (base stats + LEVEL_STAT_GROWTH from
# app.py, before any gear) -- they get easier as you outlevel the tier.
DEFAULT_MONSTERS = [
    {"tier": "beginner", "name": "潑皮野狼", "is_boss": 0, "hp": 70, "atk": 14, "def": 5, "agi": 10, "currency_reward": 15},
    {"tier": "beginner", "name": "荒野土狼", "is_boss": 0, "hp": 85, "atk": 16, "def": 6, "agi": 12, "currency_reward": 18},
    {"tier": "beginner", "name": "銹刃盜賊", "is_boss": 0, "hp": 75, "atk": 15, "def": 7, "agi": 14, "currency_reward": 20},
    {"tier": "beginner", "name": "荒原狼王", "is_boss": 1, "hp": 300, "atk": 30, "def": 12, "agi": 20, "currency_reward": 100},

    {"tier": "intermediate", "name": "赤鱗蜥蜴", "is_boss": 0, "hp": 160, "atk": 26, "def": 13, "agi": 18, "currency_reward": 35},
    {"tier": "intermediate", "name": "岩甲蟹", "is_boss": 0, "hp": 200, "atk": 24, "def": 18, "agi": 14, "currency_reward": 38},
    {"tier": "intermediate", "name": "黑霧遊魂", "is_boss": 0, "hp": 150, "atk": 30, "def": 10, "agi": 26, "currency_reward": 36},
    {"tier": "intermediate", "name": "熔岩巨蠍王", "is_boss": 1, "hp": 650, "atk": 55, "def": 25, "agi": 32, "currency_reward": 220},

    {"tier": "advanced", "name": "鋼骨巨魔", "is_boss": 0, "hp": 320, "atk": 50, "def": 28, "agi": 26, "currency_reward": 70},
    {"tier": "advanced", "name": "幽冥劍靈", "is_boss": 0, "hp": 280, "atk": 58, "def": 22, "agi": 38, "currency_reward": 75},
    {"tier": "advanced", "name": "血眸狂虎", "is_boss": 0, "hp": 350, "atk": 52, "def": 25, "agi": 34, "currency_reward": 72},
    {"tier": "advanced", "name": "深淵魔狼王", "is_boss": 1, "hp": 1100, "atk": 90, "def": 45, "agi": 45, "currency_reward": 450},

    {"tier": "ultimate", "name": "天穹巨龍裔", "is_boss": 0, "hp": 600, "atk": 85, "def": 40, "agi": 42, "currency_reward": 150},
    {"tier": "ultimate", "name": "虛空吞噬者", "is_boss": 0, "hp": 550, "atk": 95, "def": 35, "agi": 50, "currency_reward": 160},
    {"tier": "ultimate", "name": "混沌石像鬼", "is_boss": 0, "hp": 700, "atk": 75, "def": 50, "agi": 35, "currency_reward": 155},
    {"tier": "ultimate", "name": "終焉魔神", "is_boss": 1, "hp": 2000, "atk": 150, "def": 70, "agi": 60, "currency_reward": 900},
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


def _ensure_character_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(characters)")]
    if "current_tile_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN current_tile_id INTEGER")
    if "currency" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN currency INTEGER NOT NULL DEFAULT 1000")
    if "level" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
    if "exp" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN exp INTEGER NOT NULL DEFAULT 0")
    if "next_action_at" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN next_action_at TEXT")
    if "equipped_weapon_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_weapon_id INTEGER")
    if "equipped_armor_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_armor_id INTEGER")
    if "equipped_accessory_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_accessory_id INTEGER")
    if "current_hp" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN current_hp INTEGER")
    if "current_mp" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN current_mp INTEGER")
    if "name" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN name TEXT")
        conn.execute(
            """UPDATE characters SET name = (
                   SELECT username FROM users WHERE users.id = characters.user_id
               ) WHERE name IS NULL"""
        )
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_characters_name ON characters(name)")
        except sqlite3.IntegrityError:
            pass


def _ensure_game_settings_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(game_settings)")]
    if "sell_back_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN sell_back_percent REAL NOT NULL DEFAULT 75")
    if "boss_encounter_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_encounter_percent REAL NOT NULL DEFAULT 15")
    if "boss_exp_multiplier" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_exp_multiplier REAL NOT NULL DEFAULT 5")


def init_db():
    conn = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    _ensure_is_admin_column(conn)
    _ensure_session_columns(conn)
    _ensure_character_columns(conn)
    _ensure_game_settings_columns(conn)
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

    map_regenerated = _seed_map_tiles(conn)
    if map_regenerated:
        conn.execute("UPDATE characters SET current_tile_id = NULL")
    _backfill_character_positions(conn)

    if conn.execute("SELECT COUNT(*) AS c FROM game_settings").fetchone()["c"] == 0:
        conn.execute("INSERT INTO game_settings (id) VALUES (1)")

    if conn.execute("SELECT COUNT(*) AS c FROM hunting_grounds").fetchone()["c"] == 0:
        for g in DEFAULT_HUNTING_GROUNDS:
            conn.execute(
                """INSERT INTO hunting_grounds (tier, name, min_level, max_level, monster_exp)
                   VALUES (?, ?, ?, ?, ?)""",
                (g["tier"], g["name"], g["min_level"], g["max_level"], g["monster_exp"]),
            )

    if conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0:
        for i in DEFAULT_ITEMS:
            conn.execute(
                """INSERT INTO items (shop_type, name, price, stat, stat_bonus)
                   VALUES (?, ?, ?, ?, ?)""",
                (i["shop_type"], i["name"], i["price"], i["stat"], i["stat_bonus"]),
            )

    if conn.execute("SELECT COUNT(*) AS c FROM monsters").fetchone()["c"] == 0:
        ground_ids = {
            row["tier"]: row["id"]
            for row in conn.execute("SELECT id, tier FROM hunting_grounds")
        }
        for m in DEFAULT_MONSTERS:
            conn.execute(
                """INSERT INTO monsters (hunting_ground_id, name, is_boss, hp, atk, def, agi, currency_reward)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ground_ids[m["tier"]], m["name"], m["is_boss"],
                    m["hp"], m["atk"], m["def"], m["agi"], m["currency_reward"],
                ),
            )

    conn.commit()
    conn.close()


def _seed_map_tiles(conn):
    layout = generate_layout()
    country_ids = [
        row["id"] for row in conn.execute("SELECT id FROM countries ORDER BY id")
    ]

    desired = sorted(
        (
            t["q"], t["r"], t["tile_type"], t["name"],
            country_ids[t["country_index"]] if t["country_index"] is not None else None,
        )
        for t in layout
    )
    current = sorted(
        (row["q"], row["r"], row["tile_type"], row["name"], row["country_id"])
        for row in conn.execute("SELECT q, r, tile_type, name, country_id FROM map_tiles")
    )
    if desired == current:
        return False

    conn.execute("DELETE FROM map_tiles")

    for tile in layout:
        country_id = (
            country_ids[tile["country_index"]] if tile["country_index"] is not None else None
        )
        conn.execute(
            "INSERT INTO map_tiles (q, r, tile_type, name, country_id) VALUES (?, ?, ?, ?, ?)",
            (tile["q"], tile["r"], tile["tile_type"], tile["name"], country_id),
        )
    return True


def _backfill_character_positions(conn):
    conn.execute(
        """UPDATE characters
           SET current_tile_id = (
               SELECT map_tiles.id FROM map_tiles
               WHERE map_tiles.country_id = characters.country_id
                 AND map_tiles.tile_type = 'fortress'
               LIMIT 1
           )
           WHERE current_tile_id IS NULL"""
    )
