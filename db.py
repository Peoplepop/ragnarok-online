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
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 15,
    },
    {
        "name": "翡翠靈木國", "element": "木",
        "description": "防禦與生命力驚人，減傷效果顯著",
        "hp_bonus": 8, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 15, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "蔚藍千泉國", "element": "水",
        "description": "身法飄逸，擅長先發制人與連續攻擊",
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 15, "luk_bonus": 0,
    },
    {
        "name": "紅蓮業火國", "element": "火",
        "description": "烈焰焚天，魔力與傷害兼備",
        "hp_bonus": 0, "mp_bonus": 8, "str_bonus": 15, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "萬物母育國", "element": "土",
        "description": "厚德載物，六圍均衡發展",
        "hp_bonus": 6, "mp_bonus": 6, "str_bonus": 6, "def_bonus": 6, "agi_bonus": 6, "luk_bonus": 6,
    },
]

# The bonuses DEFAULT_COUNTRIES originally shipped with (all a flat 1%, which
# rounds away to nothing until stats are fairly large) -- kept here so
# _upgrade_country_bonuses can safely retarget already-seeded rows to the
# stronger values above without clobbering any bonus an admin has since
# hand-edited in /admin.
LEGACY_DEFAULT_COUNTRY_BONUSES = {
    "百鍊流金國": (0, 0, 0, 0, 0, 1),
    "翡翠靈木國": (1, 0, 0, 1, 0, 0),
    "蔚藍千泉國": (0, 0, 0, 0, 1, 0),
    "紅蓮業火國": (0, 1, 1, 0, 0, 0),
    "萬物母育國": (1, 1, 1, 1, 1, 1),
}

# 五行相剋 (Wu Xing destructive cycle): key overcomes value.
ELEMENT_OVERCOMES = {"金": "木", "木": "土", "土": "水", "水": "火", "火": "金"}

LEVEL_CAP = 200

# The level cap used to be 1000 before the job/rebirth tier system replaced
# the flat exp curve -- kept so _upgrade_hunting_ground_bounds can recognize
# and bump an old "ultimate" hunting ground seeded under the old cap.
LEGACY_ULTIMATE_MAX_LEVEL = 1000

DEFAULT_HUNTING_GROUNDS = [
    {"tier": "beginner", "name": "初級打怪場", "min_level": 1, "max_level": 30, "monster_exp": 10},
    {"tier": "intermediate", "name": "中級打怪場", "min_level": 31, "max_level": 70, "monster_exp": 20},
    {"tier": "advanced", "name": "高級打怪場", "min_level": 71, "max_level": 120, "monster_exp": 40},
    {"tier": "ultimate", "name": "究級打怪場", "min_level": 121, "max_level": LEVEL_CAP, "monster_exp": 80},
]

DEFAULT_ITEMS = [
    {"shop_type": "weapon", "name": "木劍", "price": 50, "stat": "str", "stat_bonus": 2, "country_name": None},
    {"shop_type": "weapon", "name": "鐵劍", "price": 200, "stat": "str", "stat_bonus": 8, "country_name": None},
    {"shop_type": "weapon", "name": "秘銀劍", "price": 800, "stat": "str", "stat_bonus": 20, "country_name": None},
    {"shop_type": "armor", "name": "布甲", "price": 50, "stat": "def", "stat_bonus": 2, "country_name": None},
    {"shop_type": "armor", "name": "鐵甲", "price": 200, "stat": "def", "stat_bonus": 8, "country_name": None},
    {"shop_type": "armor", "name": "龍鱗甲", "price": 800, "stat": "def", "stat_bonus": 20, "country_name": None},
    {"shop_type": "accessory", "name": "銅戒指", "price": 50, "stat": "luk", "stat_bonus": 2, "country_name": None},
    {"shop_type": "accessory", "name": "銀戒指", "price": 200, "stat": "luk", "stat_bonus": 8, "country_name": None},
    {"shop_type": "accessory", "name": "金戒指", "price": 800, "stat": "luk", "stat_bonus": 20, "country_name": None},
]

# Country-themed equipment sets: only sold in that country's own fortress
# shop (items.country_id), priced well above the top regular tier (800) per
# the "套裝必須比一般裝備貴" requirement. The 4 elemental countries stack all
# 3 pieces onto their own signature stat (so the set rewards committing to
# one stat hard); the balanced earth country instead spreads its 3 pieces
# across str/def/luk like ordinary gear, matching its "六圍均衡" theme. Set
# bonuses for wearing 2 or 3 pieces together are computed at combat-stat
# time in app.py (SET_BONUS_TIERS / EARTH_SET_BONUS_TIERS), not stored here.
SET_ITEM_PRICE = 1400
SET_ITEM_STAT_BONUS = 26
DEFAULT_SET_ITEMS = [
    {"shop_type": "weapon", "name": "流金劍", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "armor", "name": "流金鎧", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "accessory", "name": "流金墜飾", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "weapon", "name": "靈木劍", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "armor", "name": "靈木鎧", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "accessory", "name": "靈木墜飾", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "weapon", "name": "千泉劍", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "armor", "name": "千泉鎧", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "accessory", "name": "千泉墜飾", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "weapon", "name": "業火劍", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "armor", "name": "業火鎧", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "accessory", "name": "業火墜飾", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "weapon", "name": "母育劍", "stat": "str", "country_name": "萬物母育國"},
    {"shop_type": "armor", "name": "母育鎧", "stat": "def", "country_name": "萬物母育國"},
    {"shop_type": "accessory", "name": "母育墜飾", "stat": "luk", "country_name": "萬物母育國"},
]
for _set_item in DEFAULT_SET_ITEMS:
    _set_item["price"] = SET_ITEM_PRICE
    _set_item["stat_bonus"] = SET_ITEM_STAT_BONUS
DEFAULT_ITEMS = DEFAULT_ITEMS + DEFAULT_SET_ITEMS

# Monster roster, generated rather than hand-typed: every 5-level bracket
# within a hunting ground gets 2 regular monsters (two long-running species
# per tier, escalating through an adjective ladder as the bracket climbs),
# plus exactly one 守衛怪 (guardian) and one 魔王 (boss, at the tier's milestone
# level: 30/70/120/200) per tier. Regular-monster stats interpolate linearly
# from a tier's low anchor (its first bracket) to its high anchor (its last
# bracket, i.e. the milestone level); guardian = high anchor x1.2, boss = high
# anchor x1.5 (per design: "魔王比一般[milestone]級怪物的各項屬性再多50%").
_MONSTER_TIER_CONFIG = [
    {
        "tier": "beginner", "min_level": 1, "max_level": 30, "brackets": 6,
        "low": {"hp": 70, "atk": 14, "def": 6, "agi": 10, "currency_reward": 15, "exp_reward": 9},
        "high": {"hp": 220, "atk": 24, "def": 10, "agi": 18, "currency_reward": 70, "exp_reward": 15},
        "species": [("野狼", "木"), ("山豬", "土")],
        "adjectives": ["弱小", "普通", "精壯", "兇猛", "兇暴", "狂暴"],
        "guardian": {"name": "荒野守衛犀", "element": "土"},
        "boss": {"name": "荒原狼王", "element": "木"},
    },
    {
        "tier": "intermediate", "min_level": 31, "max_level": 70, "brackets": 8,
        "low": {"hp": 170, "atk": 27, "def": 14, "agi": 19, "currency_reward": 38, "exp_reward": 18},
        "high": {"hp": 480, "atk": 42, "def": 22, "agi": 30, "currency_reward": 160, "exp_reward": 28},
        "species": [("蜥蜴", "火"), ("遊魂", "水")],
        "adjectives": ["幼年", "普通", "精壯", "兇猛", "猛烈", "兇暴", "狂暴", "嗜血"],
        "guardian": {"name": "熔岩守衛犬", "element": "火"},
        "boss": {"name": "熔岩巨蠍王", "element": "火"},
    },
    {
        "tier": "advanced", "min_level": 71, "max_level": 120, "brackets": 10,
        "low": {"hp": 320, "atk": 50, "def": 25, "agi": 30, "currency_reward": 75, "exp_reward": 36},
        "high": {"hp": 800, "atk": 75, "def": 38, "agi": 48, "currency_reward": 320, "exp_reward": 54},
        "species": [("巨魔", "金"), ("劍靈", "水")],
        "adjectives": ["幼年", "普通", "精壯", "兇猛", "猛烈", "兇暴", "狂暴", "嗜血", "煞氣", "修羅化"],
        "guardian": {"name": "幽冥守衛靈", "element": "水"},
        "boss": {"name": "深淵魔狼王", "element": "水"},
    },
    {
        "tier": "ultimate", "min_level": 121, "max_level": LEVEL_CAP, "brackets": 16,
        "low": {"hp": 620, "atk": 85, "def": 42, "agi": 48, "currency_reward": 160, "exp_reward": 72},
        "high": {"hp": 1400, "atk": 130, "def": 65, "agi": 75, "currency_reward": 650, "exp_reward": 104},
        "species": [("巨龍裔", "金"), ("石像鬼", "土")],
        "adjectives": [
            "幼年", "普通", "精壯", "兇猛", "猛烈", "兇暴", "狂暴", "嗜血",
            "煞氣", "修羅化", "半神化", "神威", "天怒", "滅世", "混沌", "終焉",
        ],
        "guardian": {"name": "虛空守衛神", "element": "土"},
        "boss": {"name": "終焉魔神", "element": "火"},
    },
]

GUARDIAN_STAT_MULT = 1.2
GUARDIAN_CURRENCY_MULT = 2.5
BOSS_STAT_MULT = 1.5
BOSS_CURRENCY_MULT = 5.0
_STAT_KEYS = ("hp", "atk", "def", "agi")


def _build_default_monsters():
    monsters = []
    for cfg in _MONSTER_TIER_CONFIG:
        n = cfg["brackets"]
        low, high = cfg["low"], cfg["high"]
        for i, adjective in enumerate(cfg["adjectives"]):
            t = i / (n - 1) if n > 1 else 0
            stats = {k: round(low[k] + (high[k] - low[k]) * t) for k in _STAT_KEYS}
            currency = round(low["currency_reward"] + (high["currency_reward"] - low["currency_reward"]) * t)
            exp = round(low["exp_reward"] + (high["exp_reward"] - low["exp_reward"]) * t)
            level_min = cfg["min_level"] + i * 5
            level_max = level_min + 4
            for species, element in cfg["species"]:
                monsters.append({
                    "tier": cfg["tier"], "name": f"{adjective}{species}", "is_boss": 0, "is_guardian": 0,
                    "level_min": level_min, "level_max": level_max,
                    "hp": stats["hp"], "atk": stats["atk"], "def": stats["def"], "agi": stats["agi"],
                    "currency_reward": currency, "exp_reward": exp, "element": element,
                })
        guardian_stats = {k: round(high[k] * GUARDIAN_STAT_MULT) for k in _STAT_KEYS}
        monsters.append({
            "tier": cfg["tier"], "name": cfg["guardian"]["name"], "is_boss": 0, "is_guardian": 1,
            "level_min": None, "level_max": None,
            "hp": guardian_stats["hp"], "atk": guardian_stats["atk"],
            "def": guardian_stats["def"], "agi": guardian_stats["agi"],
            "currency_reward": round(high["currency_reward"] * GUARDIAN_CURRENCY_MULT),
            "exp_reward": high["exp_reward"],
            "element": cfg["guardian"]["element"],
        })
        boss_stats = {k: round(high[k] * BOSS_STAT_MULT) for k in _STAT_KEYS}
        monsters.append({
            "tier": cfg["tier"], "name": cfg["boss"]["name"], "is_boss": 1, "is_guardian": 0,
            "level_min": None, "level_max": None,
            "hp": boss_stats["hp"], "atk": boss_stats["atk"],
            "def": boss_stats["def"], "agi": boss_stats["agi"],
            "currency_reward": round(high["currency_reward"] * BOSS_CURRENCY_MULT),
            "exp_reward": high["exp_reward"],
            "element": cfg["boss"]["element"],
        })
    return monsters


DEFAULT_MONSTERS = _build_default_monsters()


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
    if "battles_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN battles_count INTEGER NOT NULL DEFAULT 0")
    if "wins_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN wins_count INTEGER NOT NULL DEFAULT 0")
    if "bank_balance" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN bank_balance INTEGER NOT NULL DEFAULT 0")
    if "job_class" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN job_class TEXT NOT NULL DEFAULT '初心者'")
    if "job_tier" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN job_tier INTEGER NOT NULL DEFAULT 0")
    if "rebirth_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN rebirth_count INTEGER NOT NULL DEFAULT 0")
    for stat_col in ("stat_floor_hp", "stat_floor_mp", "stat_floor_str",
                     "stat_floor_def", "stat_floor_agi", "stat_floor_luk"):
        if stat_col not in cols:
            conn.execute(f"ALTER TABLE characters ADD COLUMN {stat_col} INTEGER")
    if "pending_boss_monster_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN pending_boss_monster_id INTEGER")
    for bonus_col in ("level_bonus_hp", "level_bonus_mp", "level_bonus_str",
                      "level_bonus_def", "level_bonus_agi", "level_bonus_luk"):
        if bonus_col not in cols:
            conn.execute(f"ALTER TABLE characters ADD COLUMN {bonus_col} INTEGER NOT NULL DEFAULT 0")
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


def _ensure_country_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(countries)")]
    if "treasury" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN treasury INTEGER NOT NULL DEFAULT 0")
    if "king_character_id" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN king_character_id INTEGER")
    if "advisor_character_id" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN advisor_character_id INTEGER")
    if "general_character_id" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN general_character_id INTEGER")


def _ensure_monster_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(monsters)")]
    if "element" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN element TEXT NOT NULL DEFAULT ''")
    if "is_guardian" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN is_guardian INTEGER NOT NULL DEFAULT 0")
    if "level_min" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN level_min INTEGER")
    if "level_max" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN level_max INTEGER")
    if "exp_reward" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN exp_reward INTEGER NOT NULL DEFAULT 0")


def _ensure_item_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(items)")]
    if "country_id" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN country_id INTEGER")


def _upgrade_country_bonuses(conn):
    """One-time retarget of countries seeded with the old flat 1% bonuses to
    the new differentiated values in DEFAULT_COUNTRIES -- skips any country an
    admin has since hand-edited (its bonuses no longer match the legacy set)."""
    rows = conn.execute(
        "SELECT id, name, hp_bonus, mp_bonus, str_bonus, def_bonus, agi_bonus, luk_bonus FROM countries"
    ).fetchall()
    by_name = {c["name"]: c for c in DEFAULT_COUNTRIES}
    for row in rows:
        legacy = LEGACY_DEFAULT_COUNTRY_BONUSES.get(row["name"])
        target = by_name.get(row["name"])
        if legacy is None or target is None:
            continue
        current = (
            row["hp_bonus"], row["mp_bonus"], row["str_bonus"],
            row["def_bonus"], row["agi_bonus"], row["luk_bonus"],
        )
        if current == legacy:
            conn.execute(
                """UPDATE countries SET hp_bonus = ?, mp_bonus = ?, str_bonus = ?,
                       def_bonus = ?, agi_bonus = ?, luk_bonus = ? WHERE id = ?""",
                (
                    target["hp_bonus"], target["mp_bonus"], target["str_bonus"],
                    target["def_bonus"], target["agi_bonus"], target["luk_bonus"], row["id"],
                ),
            )


def _upgrade_monster_elements(conn):
    """One-time backfill of the element column for monsters seeded before it
    existed (rows left with the '' default)."""
    by_name = {m["name"]: m["element"] for m in DEFAULT_MONSTERS}
    for row in conn.execute("SELECT id, name, element FROM monsters"):
        if not row["element"] and row["name"] in by_name:
            conn.execute(
                "UPDATE monsters SET element = ? WHERE id = ?", (by_name[row["name"]], row["id"])
            )


def _upgrade_items(conn, country_ids_by_name):
    """Add-only: inserts any DEFAULT_ITEMS row (currently just the 5 country
    equipment sets) that isn't already present by exact name. Unlike the
    monster roster this never deletes existing rows -- items can be sitting
    in a character's inventory or equipped slot, so removing one would
    dangle a foreign key."""
    existing_names = {row["name"] for row in conn.execute("SELECT name FROM items")}
    for i in DEFAULT_ITEMS:
        if i["name"] in existing_names:
            continue
        conn.execute(
            """INSERT INTO items (shop_type, name, price, stat, stat_bonus, country_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                i["shop_type"], i["name"], i["price"], i["stat"], i["stat_bonus"],
                country_ids_by_name.get(i["country_name"]),
            ),
        )


def _rebuild_monster_roster(conn):
    """One-time full replace of the monsters table with the level-bracketed
    roster (2 named monsters per 5-level bracket + 1 守衛怪 + 1 魔王 per tier).
    Runs again if the table predates is_guardian (old flat roster) OR predates
    exp_reward (every generated row has a positive exp_reward, so a stray 0
    means the column was just added and never backfilled). No admin UI ever
    edits monsters directly, so a full wipe+reseed is safe here (unlike the
    legacy-value-check pattern used for country bonuses)."""
    has_guardian = conn.execute(
        "SELECT COUNT(*) AS c FROM monsters WHERE is_guardian = 1"
    ).fetchone()["c"]
    has_unset_exp = conn.execute(
        "SELECT COUNT(*) AS c FROM monsters WHERE exp_reward = 0"
    ).fetchone()["c"]
    if has_guardian and not has_unset_exp:
        return
    conn.execute("DELETE FROM monsters")
    ground_ids = {
        row["tier"]: row["id"] for row in conn.execute("SELECT id, tier FROM hunting_grounds")
    }
    for m in DEFAULT_MONSTERS:
        conn.execute(
            """INSERT INTO monsters
               (hunting_ground_id, name, is_boss, is_guardian, level_min, level_max,
                hp, atk, def, agi, currency_reward, exp_reward, element)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ground_ids[m["tier"]], m["name"], m["is_boss"], m["is_guardian"],
                m["level_min"], m["level_max"],
                m["hp"], m["atk"], m["def"], m["agi"], m["currency_reward"], m["exp_reward"], m["element"],
            ),
        )


def _upgrade_hunting_ground_bounds(conn):
    """One-time bump of the ultimate tier's max_level from the old LEVEL_CAP
    (1000) to the new one (200, once the job/rebirth tier system replaced the
    flat exp curve) -- skipped if an admin already customized it."""
    row = conn.execute(
        "SELECT id, max_level FROM hunting_grounds WHERE tier = 'ultimate'"
    ).fetchone()
    if row and row["max_level"] == LEGACY_ULTIMATE_MAX_LEVEL:
        conn.execute("UPDATE hunting_grounds SET max_level = ? WHERE id = ?", (LEVEL_CAP, row["id"]))


def _ensure_game_settings_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(game_settings)")]
    if "sell_back_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN sell_back_percent REAL NOT NULL DEFAULT 75")
    if "boss_encounter_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_encounter_percent REAL NOT NULL DEFAULT 15")
    if "boss_exp_multiplier" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_exp_multiplier REAL NOT NULL DEFAULT 5")
    if "shop_tax_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN shop_tax_percent REAL NOT NULL DEFAULT 5")
    if "heal_cost_per_point" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN heal_cost_per_point REAL NOT NULL DEFAULT 1")
    if "town_defense_level" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN town_defense_level INTEGER NOT NULL DEFAULT 500")
    if "fortress_defense_level" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN fortress_defense_level INTEGER NOT NULL DEFAULT 1000")
    if "exp_growth_novice_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_novice_percent REAL NOT NULL DEFAULT 6.6")
    if "exp_growth_tier2_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_tier2_percent REAL NOT NULL DEFAULT 6.0")
    if "exp_growth_tier3_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_tier3_percent REAL NOT NULL DEFAULT 0.8")
    if "exp_growth_tier4_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_tier4_percent REAL NOT NULL DEFAULT 0.8")
    if "rebirth_stat_bonus_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN rebirth_stat_bonus_percent REAL NOT NULL DEFAULT 15")
    if "guardian_encounter_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN guardian_encounter_percent REAL NOT NULL DEFAULT 2")
    if "boss_room_trigger_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_room_trigger_percent REAL NOT NULL DEFAULT 50")
    if "guardian_exp_multiplier" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN guardian_exp_multiplier REAL NOT NULL DEFAULT 2")


def init_db():
    conn = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    _ensure_is_admin_column(conn)
    _ensure_session_columns(conn)
    _ensure_character_columns(conn)
    _ensure_country_columns(conn)
    _ensure_monster_columns(conn)
    _ensure_item_columns(conn)
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
    else:
        _upgrade_country_bonuses(conn)

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
    else:
        _upgrade_hunting_ground_bounds(conn)

    conn.execute(
        "UPDATE characters SET level = ?, exp = 0 WHERE level > ?", (LEVEL_CAP, LEVEL_CAP)
    )

    country_ids_by_name = {
        row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM countries")
    }
    if conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0:
        for i in DEFAULT_ITEMS:
            conn.execute(
                """INSERT INTO items (shop_type, name, price, stat, stat_bonus, country_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    i["shop_type"], i["name"], i["price"], i["stat"], i["stat_bonus"],
                    country_ids_by_name.get(i["country_name"]),
                ),
            )
    else:
        _upgrade_items(conn, country_ids_by_name)

    if conn.execute("SELECT COUNT(*) AS c FROM monsters").fetchone()["c"] == 0:
        ground_ids = {
            row["tier"]: row["id"]
            for row in conn.execute("SELECT id, tier FROM hunting_grounds")
        }
        for m in DEFAULT_MONSTERS:
            conn.execute(
                """INSERT INTO monsters
                   (hunting_ground_id, name, is_boss, is_guardian, level_min, level_max,
                    hp, atk, def, agi, currency_reward, exp_reward, element)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ground_ids[m["tier"]], m["name"], m["is_boss"], m["is_guardian"],
                    m["level_min"], m["level_max"],
                    m["hp"], m["atk"], m["def"], m["agi"], m["currency_reward"], m["exp_reward"], m["element"],
                ),
            )
    else:
        _upgrade_monster_elements(conn)
        _rebuild_monster_roster(conn)

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
