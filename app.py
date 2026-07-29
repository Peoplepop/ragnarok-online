import os
import random
import secrets
import uuid
from datetime import datetime

from flask import Flask, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash

from db import get_db, init_db, seed_defaults, log_activity
from web_helpers import _parse_dt
from game_data.constants import IDLE_THRESHOLD_MINUTES, LEVEL_STAT_GROWTH
from game_data.skills import SKILL_CATALOG

from blueprints.auth import auth_bp
from blueprints.character import character_bp
from blueprints.game import game_bp
from blueprints.trade import trade_bp
from blueprints.admin import admin_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")


def _backfill_level_bonus_columns():
    """One-time migration: characters predating the random level-up stat-
    point system (see apply_exp/_roll_level_up_stat_points) get their
    level_bonus_* columns seeded from the old flat LEVEL_STAT_GROWTH formula,
    so switching to random rolls doesn't retroactively strip stats they
    already earned -- only levels gained *after* this change use the new
    roll. Detected by all six columns still being exactly 0, which is
    impossible for a character who has gone through even one real roll
    (a roll always distributes a positive total across at least one stat)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, level FROM characters
           WHERE level_bonus_hp = 0 AND level_bonus_mp = 0 AND level_bonus_str = 0
                 AND level_bonus_def = 0 AND level_bonus_agi = 0 AND level_bonus_luk = 0
                 AND level > 1"""
    ).fetchall()
    for row in rows:
        level_bonus = max(0, row["level"] - 1)
        conn.execute(
            """UPDATE characters SET level_bonus_hp = ?, level_bonus_mp = ?, level_bonus_str = ?,
                   level_bonus_def = ?, level_bonus_agi = ?, level_bonus_luk = ?
               WHERE id = ?""",
            (
                LEVEL_STAT_GROWTH["hp"] * level_bonus, LEVEL_STAT_GROWTH["mp"] * level_bonus,
                LEVEL_STAT_GROWTH["str"] * level_bonus, LEVEL_STAT_GROWTH["def"] * level_bonus,
                LEVEL_STAT_GROWTH["agi"] * level_bonus, LEVEL_STAT_GROWTH["luk"] * level_bonus,
                row["id"],
            ),
        )
    conn.commit()
    conn.close()



# The 15 default NPC officeholders (國王/參謀/大將軍 x 5 countries) that
# _seed_npc_officials seeds into the previously-always-empty government
# seats. Each tuple is (country_name, root, role, name, level, job_class,
# job_tier) -- root is the name fragment shared by that country's regalia
# item names (e.g. "流金御劍"/"流金策劍"/"流金戰劍" for 百鍊流金國). Job
# classes are verified against TIER3_JOBS/TIER4_JOB_BY_STAT/TIER4_TIE_JOB in
# game_data/jobs.py; King is always job_tier=4 at level 220 (a deliberate,
# one-time exception to the normal LEVEL_CAP=200 -- see the is_npc guard
# added to the level clamp in db.py's seed_defaults()), Advisor/General are
# always job_tier=3 at level 200.
NPC_OFFICIAL_ROSTER = [
    ("百鍊流金國", "流金", "king", "金璘尊", 220, "流金尊者", 4),
    ("百鍊流金國", "流金", "advisor", "銀策", 200, "太乙真人", 3),
    ("百鍊流金國", "流金", "general", "鋼鏑", 200, "天命劍仙", 3),
    ("翡翠靈木國", "靈木", "king", "木牧尊", 220, "青木道尊", 4),
    ("翡翠靈木國", "靈木", "advisor", "綠簡", 200, "龜甲寒鐵陣", 3),
    ("翡翠靈木國", "靈木", "general", "蒼甲", 200, "回天鐵壁", 3),
    ("蔚藍千泉國", "千泉", "king", "淵瀾尊", 220, "流水劍尊", 4),
    ("蔚藍千泉國", "千泉", "advisor", "潮謀", 200, "踏虛步影", 3),
    ("蔚藍千泉國", "千泉", "general", "浪鏑", 200, "追風劍影", 3),
    ("紅蓮業火國", "業火", "king", "炎煌尊", 220, "業火尊者", 4),
    ("紅蓮業火國", "業火", "advisor", "燼策", 200, "煉體宗師", 3),
    ("紅蓮業火國", "業火", "general", "烈戈", 200, "裂地劍豪", 3),
    ("萬物母育國", "母育", "king", "厚土尊", 220, "厚土真尊", 4),
    ("萬物母育國", "母育", "advisor", "塋策", 200, "奇門遁甲師", 3),
    ("萬物母育國", "母育", "general", "磐鏑", 200, "不壞金身", 3),
]

# role -> (countries.* seat column, "{root}{suffix}" regalia weapon/armor/
# accessory name suffixes). Mirrors the 3 new 官職套裝 tiers seeded into
# DEFAULT_ITEMS by db.py (GENERAL/ADVISOR/KING_SET_ITEM_*).
_NPC_ROLE_COLUMN = {
    "king": "king_character_id", "advisor": "advisor_character_id", "general": "general_character_id",
}
_NPC_ROLE_EQUIP_SUFFIXES = {
    "king": ("御劍", "御鎧", "御冠"),
    "advisor": ("策劍", "策鎧", "策珮"),
    "general": ("戰劍", "戰甲", "戰印"),
}


def _seed_npc_officials():
    """Idempotently seeds the 15-NPC roster above into users/characters, and
    assigns each into its country's still-empty government seat. Must run
    after init_db() (needs the is_npc columns), seed_defaults() (needs the
    country rows, fortress map tiles, and the new regalia items to already
    exist), and _backfill_level_bonus_columns() (ordering only -- no direct
    dependency). Safe to call on every app startup: a username lookup guards
    each NPC so a prior run's rows are never re-inserted or re-updated."""
    conn = get_db()
    country_ids_by_name = {
        row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM countries")
    }

    for country_name, root, role, name, level, job_class, job_tier in NPC_OFFICIAL_ROSTER:
        country_id = country_ids_by_name[country_name]
        username = f"npc_{role}_country{country_id}"

        if conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            continue  # already seeded on a prior startup -- idempotent no-op

        user_cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_npc) VALUES (?, ?, 1)",
            (username, generate_password_hash(secrets.token_hex(32))),
        )
        user_id = user_cur.lastrowid

        fortress = conn.execute(
            "SELECT id FROM map_tiles WHERE country_id = ? AND tile_type = 'fortress'",
            (country_id,),
        ).fetchone()
        current_tile_id = fortress["id"] if fortress else None

        weapon_suffix, armor_suffix, accessory_suffix = _NPC_ROLE_EQUIP_SUFFIXES[role]
        weapon_id = conn.execute(
            "SELECT id FROM items WHERE name = ? AND country_id = ?",
            (f"{root}{weapon_suffix}", country_id),
        ).fetchone()["id"]
        armor_id = conn.execute(
            "SELECT id FROM items WHERE name = ? AND country_id = ?",
            (f"{root}{armor_suffix}", country_id),
        ).fetchone()["id"]
        accessory_id = conn.execute(
            "SELECT id FROM items WHERE name = ? AND country_id = ?",
            (f"{root}{accessory_suffix}", country_id),
        ).fetchone()["id"]

        # This NPC never actually leveled up through play, so its
        # level_bonus_* columns are seeded with the same flat-formula
        # convention _backfill_level_bonus_columns() uses for pre-migration
        # player characters, rather than the random per-level roll system.
        level_bonus = level - 1
        level_bonus_stats = {
            key: LEVEL_STAT_GROWTH[key] * level_bonus for key in LEVEL_STAT_GROWTH
        }

        char_cur = conn.execute(
            """INSERT INTO characters
               (user_id, name, country_id, current_tile_id, currency, level, exp,
                equipped_weapon_id, equipped_armor_id, equipped_accessory_id,
                job_class, job_tier, rebirth_count, is_npc,
                level_bonus_hp, level_bonus_mp, level_bonus_str,
                level_bonus_def, level_bonus_agi, level_bonus_luk)
               VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, name, country_id, current_tile_id, level,
                weapon_id, armor_id, accessory_id, job_class, job_tier,
                level_bonus_stats["hp"], level_bonus_stats["mp"], level_bonus_stats["str"],
                level_bonus_stats["def"], level_bonus_stats["agi"], level_bonus_stats["luk"],
            ),
        )
        character_id = char_cur.lastrowid

        # King gets the exclusive tier4-slot3 skill; Advisor/General get one
        # of their tier3 job's 3 skill slots at random (spec: "一個三轉隨機
        # 技能"; only runs once at first seed, so non-reproducibility across
        # runs doesn't matter).
        slot = 3 if role == "king" else random.choice([1, 2, 3])
        skill_key = f"{job_class}_{slot}"
        if skill_key not in SKILL_CATALOG:
            raise RuntimeError(f"NPC seeding: skill key {skill_key!r} is not in SKILL_CATALOG")
        conn.execute(
            "INSERT INTO character_skills (character_id, skill_key) VALUES (?, ?)",
            (character_id, skill_key),
        )
        conn.execute(
            "UPDATE characters SET equipped_skill_1 = ? WHERE id = ?", (skill_key, character_id)
        )

        seat_column = _NPC_ROLE_COLUMN[role]
        conn.execute(
            f"UPDATE countries SET {seat_column} = ? WHERE id = ? AND {seat_column} IS NULL",
            (character_id, country_id),
        )

    conn.commit()
    conn.close()


init_db()
seed_defaults()
_backfill_level_bonus_columns()
_seed_npc_officials()


@app.before_request
def _session_activity():
    if request.endpoint == "static":
        return
    user_id = session.get("user_id")
    if not user_id:
        return

    db = get_db()
    row = db.execute("SELECT last_seen_at FROM users WHERE id = ?", (user_id,)).fetchone()
    last_seen = _parse_dt(row["last_seen_at"]) if row else None
    idle_seconds = (datetime.utcnow() - last_seen).total_seconds() if last_seen else None

    if idle_seconds is not None and idle_seconds > IDLE_THRESHOLD_MINUTES * 60:
        username = session.get("username")
        db.execute("UPDATE users SET is_online = 0 WHERE id = ?", (user_id,))
        log_activity(db, user_id, username, "auto_logout", ip_address=request.remote_addr)
        db.commit()
        db.close()
        session.clear()
        flash(f"閒置超過 {IDLE_THRESHOLD_MINUTES} 分鐘，系統已自動將您登出")
        return redirect(url_for("auth.login"))

    db.execute("UPDATE users SET last_seen_at = datetime('now') WHERE id = ?", (user_id,))
    db.commit()
    db.close()


VISITOR_COOKIE_NAME = "visitor_id"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # 5 years


@app.after_request
def _track_site_visit(response):
    """Simple admin-only site visit counter (site_visits.total_views) plus a
    distinct-visitor count (site_visitors, keyed off a long-lived cookie) --
    counts every non-static request regardless of login state. Runs as a
    single after_request hook (rather than before_request) because it needs
    to both read the visitor_id cookie AND set it on the response the first
    time a visitor is seen; an upsert (INSERT ... ON CONFLICT DO UPDATE)
    keeps a returning visitor whose cookie already exists from ever being
    double-counted as a new unique visitor."""
    if request.endpoint == "static":
        return response

    visitor_id = request.cookies.get(VISITOR_COOKIE_NAME)
    is_new_cookie = visitor_id is None
    if is_new_cookie:
        visitor_id = uuid.uuid4().hex

    db = get_db()
    db.execute("UPDATE site_visits SET total_views = total_views + 1 WHERE id = 1")
    db.execute(
        """INSERT INTO site_visitors (visitor_id) VALUES (?)
           ON CONFLICT(visitor_id) DO UPDATE SET last_seen_at = datetime('now')""",
        (visitor_id,),
    )
    db.commit()
    db.close()

    if is_new_cookie:
        response.set_cookie(
            VISITOR_COOKIE_NAME, visitor_id, max_age=VISITOR_COOKIE_MAX_AGE,
            httponly=True, samesite="Lax",
        )
    return response


@app.context_processor
def _inject_nav_display_name():
    return {"nav_display_name": session.get("character_name") or session.get("username")}


app.register_blueprint(auth_bp)
app.register_blueprint(character_bp)
app.register_blueprint(game_bp)
app.register_blueprint(trade_bp)
app.register_blueprint(admin_bp)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
