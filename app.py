import os
import random
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, init_db, seed_defaults, log_activity, LEVEL_CAP, ELEMENT_OVERCOMES
from map_layout import axial_to_pixel, hex_corners, axial_distance

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")

from game_data.constants import (
    MIN_USERNAME_LEN, MIN_PASSWORD_LEN, MAX_PASSWORD_LEN, MIN_CHARACTER_NAME_LEN, MAX_CHARACTER_NAME_LEN,
    STAT_FIELDS, IDLE_THRESHOLD_MINUTES, ACTION_LABELS, SHOP_TYPE_LABELS, SLOT_LABELS,
    EQUIP_SLOT_COLUMNS, GOVERNMENT_ROLES, tile_display_name,
    HEX_SIZE, ELEMENT_COLORS, NEUTRAL_TILE_COLOR, MOUNTAIN_TILE_COLOR, BASE_STATS,
    LEVEL_STAT_GROWTH, STAT_LABELS,
)
from game_data.jobs import (
    TIER1_JOBS, TIER2_JOBS, TIER2_CHILDREN_BY_FAMILY, TIER3_JOBS, TIER3_CHILDREN_BY_PARENT,
    TIER4_JOB_BY_STAT, TIER4_TIE_JOB, JOB_TIER_LABELS, job_stat_bonus_pct, _resolve_tier4_job,
    _process_job_progression,
)
from game_data.skills import (
    NOVICE_SKILL_STAT_BY_ELEMENT, NOVICE_SKILL_NAMES, TIER2_SKILL_NAMES, TIER2_SKILL_NAMES_SLOT2,
    TIER3_SKILL_NAMES, TIER3_SKILL_NAMES_SLOT2, TIER3_SKILL_NAMES_SLOT3, TIER4_SKILL_NAMES,
    TIER4_SKILL_NAMES_SLOT2, TIER4_SLOT2_SKILL_KEYS,
    TIER_SLOT_TUNING, _skill_key, _novice_skill_key, _build_skill_catalog, SKILL_CATALOG,
    _skill_damage_stat_value, _current_lineage_job_classes, _learnable_skills, _usable_skill_keys,
    _ordered_usable_skills, _learned_skill_keys, _character_usable_skills, _roll_job_skill,
)
from game_data.equipment import (
    SET_SIGNATURE_STAT, SET_BONUS_TIERS, EARTH_SET_BONUS_TIERS, _equipment_set_bonus,
    _active_set_summaries, _own_element_bonus_summary, _fetch_equipped_items,
)
from game_data.stats import (
    compute_final_stats, STAT_FLOOR_COLUMNS, character_final_stats, defense_tower_stats,
    _current_hp_mp, _bandit_lord_stats,
)
from game_data.progression import (
    EXP_TIER_BANDS, exp_required_for_level, LEVEL_UP_TOTAL_POINTS, LEVEL_UP_STAT_POINT_CAP,
    LEVEL_UP_POINT_VALUE, LEVEL_UP_PRIMARY_WEIGHT, LEVEL_UP_SECONDARY_WEIGHT, LEVEL_UP_BASE_WEIGHT,
    _job_primary_secondary, _roll_level_up_stat_points, apply_exp,
)
from game_data.combat import (
    STR_DAMAGE_RANGE, DEF_REDUCTION_K, DEF_REDUCTION_JITTER, DEF_REDUCTION_HARD_CAP,
    SPEED_PER_AGI, EXTRA_ATTACK_SPEED_STEP, CRIT_CHANCE_K, CRIT_CHANCE_HARD_CAP, CRIT_DAMAGE_RANGE,
    HIT_CHANCE_BASE, HIT_CHANCE_MAX_BONUS, HIT_CHANCE_K, DODGE_CHANCE_BASE, DODGE_CHANCE_MAX_BONUS,
    DODGE_CHANCE_K, GOLD_LUK_BONUS_PER_POINT, GOLD_LUK_BONUS_CAP, ELEMENT_OVERCOME_BONUS,
    ELEMENT_OVERCOME_PENALTY, elemental_multiplier, _hit_chance_pct, _dodge_chance_pct,
    _crit_chance_pct, _def_reduction_fraction, gold_luk_bonus_pct, derived_combat_stats,
    _combat_hit, BATTLE_ROUND_CAP, run_battle,
)

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


def _parse_dt(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


ACTION_DT_FORMAT = "%Y-%m-%d %H:%M:%S.%f"


def _next_action_at(wait_seconds):
    """Precise (sub-second) cooldown timestamp, computed in Python so it isn't
    truncated the way SQLite's datetime('now', '+N seconds') rounds to whole
    seconds -- that truncation could silently shave up to ~1s off every wait."""
    return (datetime.utcnow() + timedelta(seconds=wait_seconds)).strftime(ACTION_DT_FORMAT)


def _cooldown_remaining_seconds(next_action_at):
    if not next_action_at:
        return 0
    until = datetime.strptime(next_action_at, ACTION_DT_FORMAT)
    return max(0, round((until - datetime.utcnow()).total_seconds()))


def _format_duration(seconds):
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} 小時 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {sec} 秒"
    return f"{sec} 秒"


def _add_to_inventory(db, character_id, item_id, quantity=1):
    db.execute(
        """INSERT INTO inventory (character_id, item_id, quantity) VALUES (?, ?, ?)
           ON CONFLICT(character_id, item_id) DO UPDATE SET quantity = quantity + excluded.quantity""",
        (character_id, item_id, quantity),
    )


def _remove_from_inventory(db, character_id, item_id, quantity=1):
    """Returns True if the item had enough quantity and was removed."""
    row = db.execute(
        "SELECT quantity FROM inventory WHERE character_id = ? AND item_id = ?",
        (character_id, item_id),
    ).fetchone()
    if row is None or row["quantity"] < quantity:
        return False
    remaining = row["quantity"] - quantity
    if remaining <= 0:
        db.execute(
            "DELETE FROM inventory WHERE character_id = ? AND item_id = ?",
            (character_id, item_id),
        )
    else:
        db.execute(
            "UPDATE inventory SET quantity = ? WHERE character_id = ? AND item_id = ?",
            (remaining, character_id, item_id),
        )
    return True


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("請先登入")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("請先登入")
            return redirect(url_for("login"))
        if not session.get("is_admin"):
            flash("沒有權限")
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def character_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("請先登入")
            return redirect(url_for("login"))
        db = get_db()
        character = db.execute(
            "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        db.close()
        if character is None:
            return redirect(url_for("character_create"))
        return view(*args, **kwargs)
    return wrapped


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
        return redirect(url_for("login"))

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


@app.route("/")
def index():
    if session.get("user_id"):
        db = get_db()
        character = db.execute(
            "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        db.close()
        if character is None:
            return redirect(url_for("character_create"))
        return redirect(url_for("game"))

    db = get_db()
    countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
    db.close()
    return render_template("index.html", countries=countries)


PASSWORD_COMPLEXITY_PATTERN = re.compile(r"^(?=.*[0-9])(?=.*[a-z])(?=.*[A-Z]).+$")


def _validate_password(password):
    if len(password) < MIN_PASSWORD_LEN or len(password) > MAX_PASSWORD_LEN:
        return f"密碼需要 {MIN_PASSWORD_LEN}～{MAX_PASSWORD_LEN} 個字元"
    if not PASSWORD_COMPLEXITY_PATTERN.match(password):
        return "密碼需同時包含數字、大寫英文字母與小寫英文字母"
    return None


def _validate_character_name(db, name, username):
    if len(name) < MIN_CHARACTER_NAME_LEN or len(name) > MAX_CHARACTER_NAME_LEN:
        return f"角色名稱需要 {MIN_CHARACTER_NAME_LEN}～{MAX_CHARACTER_NAME_LEN} 個字元"
    if name.lower() == username.lower():
        return "角色名稱不能跟帳號相同"
    taken = db.execute(
        "SELECT id FROM characters WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if taken:
        return "這個角色名稱已經被使用了"
    return None


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    character_name = request.form.get("character_name", "").strip()

    if len(username) < MIN_USERNAME_LEN:
        flash(f"帳號至少需要 {MIN_USERNAME_LEN} 個字元")
        return render_template("register.html")
    password_error = _validate_password(password)
    if password_error:
        flash(password_error)
        return render_template("register.html")
    if password != confirm:
        flash("兩次輸入的密碼不一致")
        return render_template("register.html")

    db = get_db()
    name_error = _validate_character_name(db, character_name, username)
    if name_error:
        db.close()
        flash(name_error)
        return render_template("register.html")

    try:
        cur = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
        log_activity(db, cur.lastrowid, username, "register", ip_address=request.remote_addr)
        db.commit()
    except sqlite3.IntegrityError:
        flash("這個帳號已經被註冊了")
        return render_template("register.html")
    finally:
        db.close()

    session["pending_character_name"] = character_name
    flash("註冊成功，請登入")
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if user is None or user["is_npc"] or not check_password_hash(user["password_hash"], password):
        log_activity(
            db, user["id"] if user else None, username, "login_failed",
            ip_address=request.remote_addr,
        )
        db.commit()
        db.close()
        flash("帳號或密碼錯誤")
        return render_template("login.html")

    db.execute(
        "UPDATE users SET last_login_at = datetime('now'), last_seen_at = datetime('now'), is_online = 1 WHERE id = ?",
        (user["id"],),
    )
    log_activity(db, user["id"], user["username"], "login", ip_address=request.remote_addr)
    db.commit()
    character = db.execute(
        "SELECT name FROM characters WHERE user_id = ?", (user["id"],)
    ).fetchone()
    db.close()

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["is_admin"] = bool(user["is_admin"])
    session["character_name"] = character["name"] if character else None
    flash(f"歡迎回來，{character['name'] if character else user['username']}")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    user_id = session.get("user_id")
    username = session.get("username")
    if user_id:
        db = get_db()
        db.execute("UPDATE users SET is_online = 0 WHERE id = ?", (user_id,))
        log_activity(db, user_id, username, "logout", ip_address=request.remote_addr)
        db.commit()
        db.close()
    session.clear()
    return redirect(url_for("index"))


@app.route("/character/create", methods=["GET", "POST"])
@login_required
def character_create():
    db = get_db()
    existing = db.execute(
        "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    if existing:
        db.close()
        return redirect(url_for("game"))

    character_name = session.get("pending_character_name")
    if not character_name:
        character_name = f"{session['username']}的角色"
        suffix = 2
        while db.execute(
            "SELECT id FROM characters WHERE lower(name) = lower(?)", (character_name,)
        ).fetchone():
            character_name = f"{session['username']}的角色{suffix}"
            suffix += 1

    if request.method == "GET":
        countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
        db.close()
        return render_template("character_create.html", countries=countries, character_name=character_name)

    country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (request.form.get("country_id", ""),)
    ).fetchone()
    if country is None:
        db.close()
        flash("請選擇一個有效的國家")
        return redirect(url_for("character_create"))

    fortress = db.execute(
        "SELECT id FROM map_tiles WHERE country_id = ? AND tile_type = 'fortress'",
        (country["id"],),
    ).fetchone()
    if fortress is None:
        # Fortress may have been conquered away -- fall back to any tile the
        # country still owns (a town). If it owns nothing at all, it has no
        # territory left to spawn characters on.
        fortress = db.execute(
            "SELECT id FROM map_tiles WHERE country_id = ? LIMIT 1", (country["id"],)
        ).fetchone()
    if fortress is None:
        db.close()
        flash(f"{country['name']}目前沒有任何據點，暫時無法在此建立角色")
        return redirect(url_for("character_create"))

    try:
        db.execute(
            "INSERT INTO characters (user_id, country_id, current_tile_id, name) VALUES (?, ?, ?, ?)",
            (session["user_id"], country["id"], fortress["id"], character_name),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        flash("這個角色名稱剛好被用掉了，請重新整理再試一次")
        return redirect(url_for("character_create"))

    log_activity(
        db, session["user_id"], session["username"], "character_create",
        detail=f"{character_name}（{country['name']}）", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    session.pop("pending_character_name", None)
    session["character_name"] = character_name
    flash(f"歡迎來到{country['name']}，{character_name}！")
    return redirect(url_for("game"))


def _relocate_or_clear_garrison(db, character_id, new_tile):
    """If character_id currently has a garrison row, relocate it to new_tile
    (refreshing stationed_at so a genuine relocation counts as a fresh "most
    recent" stationing) when new_tile is a valid own-country fortress/town;
    otherwise the destination isn't a legal garrison location, so the garrison
    row is deleted entirely."""
    garrison = db.execute(
        "SELECT id FROM garrisons WHERE character_id = ?", (character_id,)
    ).fetchone()
    if garrison is None:
        return
    character = db.execute(
        "SELECT country_id FROM characters WHERE id = ?", (character_id,)
    ).fetchone()
    valid = (
        new_tile["tile_type"] in ("fortress", "town")
        and new_tile["country_id"] == character["country_id"]
    )
    if valid:
        db.execute(
            "UPDATE garrisons SET tile_id = ?, stationed_at = datetime('now') WHERE character_id = ?",
            (new_tile["id"], character_id),
        )
    else:
        db.execute("DELETE FROM garrisons WHERE character_id = ?", (character_id,))


def _render_game(**extra):
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.current_tile_id,
                  characters.currency, characters.bank_balance, characters.level, characters.exp,
                  characters.next_action_at, characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.name AS character_name,
                  characters.current_hp, characters.current_mp, characters.job_class, characters.job_tier,
                  characters.rebirth_count, characters.stat_floor_hp, characters.stat_floor_mp,
                  characters.stat_floor_str, characters.stat_floor_def, characters.stat_floor_agi,
                  characters.stat_floor_luk, characters.level_bonus_hp, characters.level_bonus_mp,
                  characters.level_bonus_str, characters.level_bonus_def, characters.level_bonus_agi,
                  characters.level_bonus_luk, characters.contribution,
                  characters.donated_today, characters.donated_today_date, countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    tiles = [
        dict(row) for row in db.execute(
            """SELECT map_tiles.id AS tile_id, map_tiles.q, map_tiles.r, map_tiles.tile_type,
                      map_tiles.name, map_tiles.country_id, map_tiles.bandit_hp,
                      countries.element, countries.name AS country_name
               FROM map_tiles LEFT JOIN countries ON countries.id = map_tiles.country_id"""
        ).fetchall()
    ]
    for t in tiles:
        t["display_name"] = tile_display_name(t["name"], t["tile_type"])
    current_tile = next(t for t in tiles if t["tile_id"] == character["current_tile_id"])
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    hunting_grounds = db.execute(
        "SELECT * FROM hunting_grounds ORDER BY min_level"
    ).fetchall()
    admin_monsters = []
    if session.get("is_admin"):
        rows = db.execute(
            """SELECT monsters.*, hunting_grounds.name AS ground_name
               FROM monsters JOIN hunting_grounds ON hunting_grounds.id = monsters.hunting_ground_id
               ORDER BY hunting_grounds.min_level, monsters.is_boss, monsters.is_guardian, monsters.level_min"""
        ).fetchall()
        for m in rows:
            if m["is_boss"]:
                level_label = "首領"
            elif m["is_guardian"]:
                level_label = "守衛怪"
            else:
                level_label = f"Lv{m['level_min']}-{m['level_max']}"
            admin_monsters.append({
                "id": m["id"], "name": m["name"], "ground_name": m["ground_name"],
                "level_label": level_label,
            })
    equipped_items = _fetch_equipped_items(db, character)

    # Garrison status: fetched off the garrisons table itself (not inferred
    # from current_tile_id) -- see game.html/point 12 for why this is
    # defensive rather than assumed.
    garrison = db.execute(
        "SELECT tile_id, stationed_at FROM garrisons WHERE character_id = ?",
        (character["character_id"],),
    ).fetchone()
    garrison_tile = None
    if garrison is not None:
        garrison_tile = next((t for t in tiles if t["tile_id"] == garrison["tile_id"]), None)
    can_station_here = (
        current_tile["tile_type"] in ("fortress", "town")
        and current_tile["country_id"] == character["id"]
    )
    own_tile_count = db.execute(
        "SELECT COUNT(*) AS c FROM map_tiles WHERE country_id = ?", (character["id"],)
    ).fetchone()["c"]
    country_destroyed = own_tile_count == 0
    pending_trade_invite_count = db.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE target_character_id = ? AND status = 'pending'",
        (character["character_id"],),
    ).fetchone()["c"]
    db.close()

    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    exp_needed = (
        exp_required_for_level(character["level"], settings, force_one=session.get("is_admin", False))
        if character["level"] < LEVEL_CAP else None
    )

    cooldown_seconds = _cooldown_remaining_seconds(character["next_action_at"])

    missing = (stats["hp"] - current_hp) + (stats["mp"] - current_mp)
    recover_cost = round(missing * settings["heal_cost_per_point"])

    can_attack_tile = (
        current_tile["tile_type"] == "neutral"
        or (
            current_tile["tile_type"] in ("fortress", "town")
            and current_tile["country_id"] is not None
            and current_tile["country_id"] != character["id"]
        )
    )
    bandit_hp_max = None
    bandit_hp = None
    if current_tile["tile_type"] == "neutral":
        bandit_hp_max = _bandit_lord_stats(settings)["hp"]
        # Read-only view: falls back to the max when NULL rather than writing
        # the lazy-init back to the DB -- only game_conquer() ever persists it.
        bandit_hp = current_tile["bandit_hp"] if current_tile["bandit_hp"] is not None else bandit_hp_max
    job_action_available = (
        (character["job_tier"] == 0 and character["level"] >= 10)
        or (character["job_tier"] == 1 and character["level"] >= 30)
        or (character["job_tier"] == 2 and character["level"] >= 70)
        or (character["job_tier"] == 3 and character["level"] >= 120)
    )
    defense_level = (
        settings["fortress_defense_level"] if current_tile["tile_type"] == "fortress"
        else settings["town_defense_level"]
    )

    here = (current_tile["q"], current_tile["r"])
    move_targets = [
        t for t in tiles
        if t["tile_type"] != "mountain"
        and axial_distance(here, (t["q"], t["r"])) == 1
    ]

    hexes = []
    xs, ys = [], []
    for t in tiles:
        cx, cy = axial_to_pixel(t["q"], t["r"], HEX_SIZE)
        color = (
            MOUNTAIN_TILE_COLOR if t["tile_type"] == "mountain"
            else ELEMENT_COLORS.get(t["element"], NEUTRAL_TILE_COLOR)
        )
        points = " ".join(f"{px:.1f},{py:.1f}" for px, py in hex_corners(cx, cy, HEX_SIZE))
        xs += [cx - HEX_SIZE, cx + HEX_SIZE]
        ys += [cy - HEX_SIZE, cy + HEX_SIZE]
        hexes.append({
            "points": points,
            "cx": cx,
            "cy": cy,
            "color": color,
            "tile_type": t["tile_type"],
            "name": t["name"],
            "country_name": t["country_name"],
            "is_player_here": t["tile_id"] == character["current_tile_id"],
        })

    padding = HEX_SIZE
    min_x, max_x = min(xs) - padding, max(xs) + padding
    min_y, max_y = min(ys) - padding, max(ys) + padding

    # 貢獻值 donation cap display: NOTE this SELECT ends with a bare
    # countries.*, so character["id"] resolves to the COUNTRY's id (last
    # column wins in sqlite3.Row) -- the character's own id is
    # character["character_id"]. Comparing against king/advisor/general
    # character id MUST use character["character_id"], not character["id"].
    if character["character_id"] == character["king_character_id"]:
        donate_cap = DONATE_DAILY_CAP_KING
    elif character["character_id"] in (
        character["advisor_character_id"], character["general_character_id"],
    ):
        donate_cap = DONATE_DAILY_CAP_OFFICER
    else:
        donate_cap = DONATE_DAILY_CAP_DEFAULT
    today = datetime.utcnow().strftime("%Y-%m-%d")
    donated_today_display = (
        character["donated_today"] if character["donated_today_date"] == today else 0
    )

    context = dict(
        character=character,
        stats=stats,
        current_hp=current_hp,
        current_mp=current_mp,
        level_cap=LEVEL_CAP,
        exp_needed=exp_needed,
        current_tile=current_tile,
        move_targets=move_targets,
        hunting_grounds=hunting_grounds,
        admin_monsters=admin_monsters,
        cooldown_seconds=cooldown_seconds,
        recover_cost=recover_cost,
        can_attack_tile=can_attack_tile,
        defense_level=defense_level,
        bandit_hp=bandit_hp,
        bandit_hp_max=bandit_hp_max,
        job_action_available=job_action_available,
        country_destroyed=country_destroyed,
        own_treasury=character["treasury"],
        hexes=hexes,
        view_box=f"{min_x:.1f} {min_y:.1f} {max_x - min_x:.1f} {max_y - min_y:.1f}",
        garrison=garrison,
        garrison_tile=garrison_tile,
        can_station_here=can_station_here,
        donate_cap=donate_cap,
        donated_today_display=donated_today_display,
        pending_trade_invite_count=pending_trade_invite_count,
    )
    context.update(extra)
    return render_template("game.html", **context)


@app.route("/game")
@character_required
def game():
    return _render_game()


@app.route("/game/move", methods=["POST"])
@character_required
def game_move():
    db = get_db()
    character = db.execute(
        "SELECT id, current_tile_id, next_action_at FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game"))

    current_tile = db.execute(
        "SELECT q, r FROM map_tiles WHERE id = ?", (character["current_tile_id"],)
    ).fetchone()
    target_tile = db.execute(
        "SELECT id, q, r, tile_type, name, country_id FROM map_tiles WHERE id = ?",
        (request.form.get("tile_id", ""),),
    ).fetchone()

    valid_target = (
        target_tile is not None
        and target_tile["tile_type"] != "mountain"
        and axial_distance((current_tile["q"], current_tile["r"]), (target_tile["q"], target_tile["r"])) == 1
    )
    if not valid_target:
        db.close()
        flash("無法移動到那個地點")
        return redirect(url_for("game"))

    target_name = tile_display_name(target_tile["name"], target_tile["tile_type"])

    garrison = db.execute(
        "SELECT id FROM garrisons WHERE character_id = ?", (character["id"],)
    ).fetchone()
    if garrison is not None and request.form.get("confirm_garrison_move") != "1":
        db.close()
        flash(f"你目前正在駐防中，移動到「{target_name}」將會變更或解除駐防狀態，請確認是否繼續移動")
        return _render_game(
            pending_move_tile_id=target_tile["id"],
            pending_move_tile_name=target_name,
        )

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        "UPDATE characters SET current_tile_id = ?, next_action_at = ?, pending_boss_monster_id = NULL WHERE id = ?",
        (target_tile["id"], _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    if garrison is not None:
        _relocate_or_clear_garrison(db, character["id"], target_tile)
    log_activity(
        db, session["user_id"], session["username"], "move",
        detail=target_name, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"移動到了「{target_name}」")
    return redirect(url_for("game"))


@app.route("/game/hunt", methods=["POST"])
@character_required
def game_hunt():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp, characters.next_action_at,
                  characters.current_hp, characters.current_mp, characters.currency, characters.name AS character_name,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  map_tiles.tile_type, countries.*
           FROM characters
           JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game"))

    if character["tile_type"] == "fortress":
        db.close()
        flash("要塞內沒有打怪地點，請先移動到要塞外")
        return redirect(url_for("game"))

    forced_monster = None
    if session.get("is_admin") and request.form.get("monster_id", ""):
        forced_monster = db.execute(
            "SELECT * FROM monsters WHERE id = ?", (request.form.get("monster_id", ""),)
        ).fetchone()

    if forced_monster is not None:
        ground = db.execute(
            "SELECT * FROM hunting_grounds WHERE id = ?", (forced_monster["hunting_ground_id"],)
        ).fetchone()
    else:
        ground = db.execute(
            "SELECT * FROM hunting_grounds WHERE id = ?", (request.form.get("ground_id", ""),)
        ).fetchone()
    if ground is None:
        db.close()
        flash("請選擇一個有效的打怪場")
        return redirect(url_for("game"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game"))

    monsters = db.execute(
        "SELECT * FROM monsters WHERE hunting_ground_id = ?", (ground["id"],)
    ).fetchall()
    boss = next((m for m in monsters if m["is_boss"]), None)

    if forced_monster is not None:
        monster = forced_monster
        is_guardian_fight = bool(monster["is_guardian"])
    else:
        regulars_in_bracket = [
            m for m in monsters
            if not m["is_boss"] and not m["is_guardian"]
            and m["level_min"] is not None and m["level_min"] <= character["level"] <= m["level_max"]
        ]
        regulars_any = [m for m in monsters if not m["is_boss"] and not m["is_guardian"]]
        guardian = next((m for m in monsters if m["is_guardian"]), None)
        regular_pool = regulars_in_bracket or regulars_any
        if not regular_pool and not guardian:
            db.close()
            flash("這個打怪場目前還沒有設定怪物")
            return redirect(url_for("game"))
        is_guardian_fight = bool(guardian) and random.random() * 100 < settings["guardian_encounter_percent"]
        monster = guardian if is_guardian_fight else random.choice(regular_pool)

    usable_skills = _character_usable_skills(db, character)
    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, monster,
        player_mp=current_mp, usable_skills=usable_skills,
    )

    exp_gain = 0
    currency_gain = 0
    currency_lost = 0
    new_level, new_exp = character["level"], character["exp"]
    stat_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    pending_boss_id = None
    boss_room_available = False
    skill_book_dropped = None
    if result["won"]:
        if is_guardian_fight:
            exp_multiplier = settings["guardian_exp_multiplier"]
        elif monster["is_boss"]:
            exp_multiplier = settings["boss_exp_multiplier"]
        else:
            exp_multiplier = 1.0
        exp_gain = round(monster["exp_reward"] * exp_multiplier)
        currency_gain = round(monster["currency_reward"] * (1 + gold_luk_bonus_pct(stats["luk"]) / 100))
        new_level, new_exp, stat_gain = apply_exp(
            character["level"], character["exp"], exp_gain, settings,
            force_one=session.get("is_admin", False),
            job_class=character["job_class"], job_tier=character["job_tier"],
        )
        new_currency = character["currency"] + currency_gain
        if is_guardian_fight and boss is not None and result["player_hp"] > 0:
            boss_room_available = True
            pending_boss_id = boss["id"]
        # Rare monster-drop skill book: only rolls in the top-tier ultimate
        # hunting ground, independent of the hunter's own job (a "wrong
        # job's" book still isn't wasted -- once actually learned at 四轉,
        # every learned skill becomes usable regardless of lineage).
        if ground["tier"] == "ultimate" and random.random() < 1 / 20000:
            dropped_key = random.choice(TIER4_SLOT2_SKILL_KEYS)
            dropped_skill = SKILL_CATALOG[dropped_key]
            skill_book_dropped = dropped_skill["name"]
            db.execute(
                """INSERT INTO character_skill_books (character_id, skill_key, quantity)
                   VALUES (?, ?, 1)
                   ON CONFLICT(character_id, skill_key) DO UPDATE SET quantity = quantity + 1""",
                (character["character_id"], dropped_key),
            )
            log_activity(
                db, session["user_id"], session["username"], "skill_book_drop",
                detail=dropped_skill["name"], ip_address=request.remote_addr,
            )
    elif not result["timed_out"]:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
    else:
        new_currency = character["currency"]

    _process_job_progression(db, character, character["level"], new_level)

    db.execute(
        """UPDATE characters
           SET level = ?, exp = ?, currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               pending_boss_monster_id = ?,
               level_bonus_hp = level_bonus_hp + ?, level_bonus_mp = level_bonus_mp + ?,
               level_bonus_str = level_bonus_str + ?, level_bonus_def = level_bonus_def + ?,
               level_bonus_agi = level_bonus_agi + ?, level_bonus_luk = level_bonus_luk + ?
           WHERE id = ?""",
        (
            new_level, new_exp, new_currency, result["player_hp"], result["player_mp"],
            _next_action_at(settings["turn_wait_seconds"]), 1 if result["won"] else 0,
            pending_boss_id,
            stat_gain["hp"], stat_gain["mp"], stat_gain["str"],
            stat_gain["def"], stat_gain["agi"], stat_gain["luk"],
            character["character_id"],
        ),
    )
    if result["won"]:
        outcome_detail = f"擊敗{monster['name']}，+{exp_gain} EXP +{currency_gain} 諸神幣"
    elif result["timed_out"]:
        outcome_detail = f"與{monster['name']}戰鬥回合已滿，未分勝負，沒有任何諸神幣損失"
    else:
        outcome_detail = f"敗給{monster['name']}，身上 {currency_lost} 諸神幣化為烏有"
    if is_guardian_fight:
        outcome_detail = f"[守衛怪] {outcome_detail}"
    log_activity(
        db, session["user_id"], session["username"], "hunt",
        detail=f"{ground['name']} {outcome_detail}", ip_address=request.remote_addr,
    )

    if new_level > character["level"]:
        updated = dict(character)
        updated["level"] = new_level
        for stat in ("hp", "mp", "str", "def", "agi", "luk"):
            updated[f"level_bonus_{stat}"] = character[f"level_bonus_{stat}"] + stat_gain[stat]
        stats_after = character_final_stats(updated, equipped_items, settings)
    else:
        stats_after = None

    db.commit()
    db.close()

    return render_template(
        "battle.html",
        ground=ground,
        monster=monster,
        guardian_encounter=is_guardian_fight,
        boss_room_available=boss_room_available,
        skill_book_dropped=skill_book_dropped,
        log=result["log"],
        won=result["won"],
        timed_out=result["timed_out"],
        leveled_up=new_level > character["level"],
        new_level=new_level,
        exp_gain=exp_gain,
        currency_gain=currency_gain,
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@app.route("/game/hunt/boss_room", methods=["POST"])
@character_required
def game_hunt_boss_room():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp, characters.currency,
                  characters.name AS character_name, characters.pending_boss_monster_id,
                  characters.current_hp, characters.current_mp,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  countries.*
           FROM characters
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    boss = None
    ground = None
    if character["pending_boss_monster_id"] is not None:
        boss = db.execute(
            "SELECT * FROM monsters WHERE id = ? AND is_boss = 1", (character["pending_boss_monster_id"],)
        ).fetchone()
        if boss is not None:
            ground = db.execute(
                "SELECT * FROM hunting_grounds WHERE id = ?", (boss["hunting_ground_id"],)
            ).fetchone()
    if boss is None or ground is None:
        db.close()
        flash("魔王房間的挑戰機會已經沒有了")
        return redirect(url_for("game"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.execute(
            "UPDATE characters SET pending_boss_monster_id = NULL WHERE id = ?", (character["character_id"],)
        )
        db.commit()
        db.close()
        flash("HP 已耗盡，無法挑戰魔王，請先回到要塞回復")
        return redirect(url_for("game"))

    usable_skills = _character_usable_skills(db, character)
    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, boss,
        player_mp=current_mp, usable_skills=usable_skills,
    )

    exp_gain = 0
    currency_gain = 0
    currency_lost = 0
    new_level, new_exp = character["level"], character["exp"]
    stat_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    if result["won"]:
        exp_gain = round(boss["exp_reward"] * settings["boss_exp_multiplier"])
        currency_gain = round(boss["currency_reward"] * (1 + gold_luk_bonus_pct(stats["luk"]) / 100))
        new_level, new_exp, stat_gain = apply_exp(
            character["level"], character["exp"], exp_gain, settings,
            force_one=session.get("is_admin", False),
            job_class=character["job_class"], job_tier=character["job_tier"],
        )
        new_currency = character["currency"] + currency_gain
    elif not result["timed_out"]:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
    else:
        new_currency = character["currency"]

    _process_job_progression(db, character, character["level"], new_level)

    db.execute(
        """UPDATE characters
           SET level = ?, exp = ?, currency = ?, current_hp = ?, current_mp = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               pending_boss_monster_id = NULL,
               level_bonus_hp = level_bonus_hp + ?, level_bonus_mp = level_bonus_mp + ?,
               level_bonus_str = level_bonus_str + ?, level_bonus_def = level_bonus_def + ?,
               level_bonus_agi = level_bonus_agi + ?, level_bonus_luk = level_bonus_luk + ?
           WHERE id = ?""",
        (
            new_level, new_exp, new_currency, result["player_hp"], result["player_mp"],
            1 if result["won"] else 0,
            stat_gain["hp"], stat_gain["mp"], stat_gain["str"],
            stat_gain["def"], stat_gain["agi"], stat_gain["luk"],
            character["character_id"],
        ),
    )
    if result["won"]:
        outcome_detail = f"擊敗{boss['name']}，+{exp_gain} EXP +{currency_gain} 諸神幣"
    elif result["timed_out"]:
        outcome_detail = f"與{boss['name']}戰鬥回合已滿，未分勝負，沒有任何諸神幣損失"
    else:
        outcome_detail = f"敗給{boss['name']}，身上 {currency_lost} 諸神幣化為烏有"
    log_activity(
        db, session["user_id"], session["username"], "hunt",
        detail=f"[魔王房間] {ground['name']} {outcome_detail}", ip_address=request.remote_addr,
    )

    if new_level > character["level"]:
        updated = dict(character)
        updated["level"] = new_level
        for stat in ("hp", "mp", "str", "def", "agi", "luk"):
            updated[f"level_bonus_{stat}"] = character[f"level_bonus_{stat}"] + stat_gain[stat]
        stats_after = character_final_stats(updated, equipped_items, settings)
    else:
        stats_after = None

    db.commit()
    db.close()

    return render_template(
        "battle.html",
        ground=ground,
        monster=boss,
        boss_room_challenge=True,
        log=result["log"],
        won=result["won"],
        timed_out=result["timed_out"],
        leveled_up=new_level > character["level"],
        new_level=new_level,
        exp_gain=exp_gain,
        currency_gain=currency_gain,
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


def _resolve_bandit_conquest(db, character, settings, stats, current_hp, current_mp):
    """Neutral-tile fight against the persistent-HP 山賊領主 (bandit lord) --
    the one deliberate exception to this game's usual single-action instant
    win/loss battle model (see the module note above _bandit_lord_stats in
    game_data/stats.py). bandit_hp survives across separate /game/conquer
    actions and never regenerates; only a killing blow flips the tile to an
    ordinary country-owned town, with the finishing attacker installed as
    mayor -- matching the existing garrison system's town-capture rule."""
    tile_name = tile_display_name(character["tile_name"], character["tile_type"])
    bandit_profile = _bandit_lord_stats(settings)
    bandit_hp_max = bandit_profile["hp"]

    tile_row = db.execute(
        "SELECT bandit_hp FROM map_tiles WHERE id = ?", (character["current_tile_id"],)
    ).fetchone()
    bandit_hp_before = tile_row["bandit_hp"] if tile_row["bandit_hp"] is not None else bandit_hp_max

    bandit_monster = dict(bandit_profile)
    bandit_monster["hp"] = bandit_hp_before

    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, bandit_monster,
        player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
    )

    bandit_hp_after = result["monster_hp"]
    # monster_hp <= 0 always coincides with the player still being alive at
    # that instant (run_battle's loop breaks the moment either side hits 0,
    # so a simultaneous double-KO can't happen) -- i.e. this is equivalent to
    # result["won"], just phrased the way the spec's mechanics are: "if the
    # resulting monster_hp <= 0, the bandit is dead."
    tile_captured = bandit_hp_after <= 0

    # Modeled like a normal PvE loss (game_hunt), NOT the country-vs-country
    # conquer loss rule -- there's no owning country's treasury for a
    # neutral-tile fight to pay into, so a forfeited half-currency simply
    # vanishes. bandit_hp is deliberately tuned to take several actions to
    # deplete, so BATTLE_ROUND_CAP is routinely hit with both sides still
    # standing -- run_battle reports that inconclusive case as
    # timed_out=True, which is neither a capture nor a defeat and must not
    # cost the attacker anything; only an actual player death (not won, not
    # timed_out) forfeits currency.
    currency_lost = 0
    new_currency = character["currency"]
    if not result["won"] and not result["timed_out"]:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost

    if tile_captured:
        db.execute(
            """UPDATE map_tiles
               SET country_id = ?, tile_type = 'town', mayor_character_id = ?, bandit_hp = NULL
               WHERE id = ?""",
            (character["id"], character["character_id"], character["current_tile_id"]),
        )
        outcome_detail = f"擊敗盤據於{tile_name}的山賊領主，將無主之地收歸領土並自動成為城主"
    else:
        db.execute(
            "UPDATE map_tiles SET bandit_hp = ? WHERE id = ?",
            (bandit_hp_after, character["current_tile_id"]),
        )
        if currency_lost:
            outcome_detail = (
                f"攻打{tile_name}的山賊領主時力竭倒下，身上{currency_lost}諸神幣化為烏有"
                f"（山賊領主剩餘 HP {bandit_hp_after}/{bandit_hp_max}）"
            )
        else:
            outcome_detail = f"削弱了{tile_name}的山賊領主（剩餘 HP {bandit_hp_after}/{bandit_hp_max}）"

    db.execute(
        """UPDATE characters
           SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               pending_boss_monster_id = NULL
           WHERE id = ?""",
        (
            new_currency, result["player_hp"], result["player_mp"], _next_action_at(settings["turn_wait_seconds"]),
            1 if tile_captured else 0, character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "conquer_win" if tile_captured else "conquer_loss",
        detail=outcome_detail, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    return render_template(
        "battle.html",
        conquest=True,
        bandit_fight=True,
        captured_tile_name=tile_name,
        defending_country_name=character["name"],  # attacker's own country, once captured
        monster=bandit_monster,
        log=result["log"],
        won=tile_captured,
        timed_out=result["timed_out"],
        tile_captured=tile_captured,
        attacker_defeated=not result["won"] and not result["timed_out"],
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
        bandit_hp_remaining=max(0, bandit_hp_after),
        bandit_hp_max=bandit_hp_max,
    )


@app.route("/game/conquer", methods=["POST"])
@character_required
def game_conquer():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.current_tile_id, characters.level,
                  characters.exp, characters.next_action_at, characters.current_hp, characters.current_mp,
                  characters.currency, characters.name AS character_name,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  map_tiles.tile_type, map_tiles.country_id AS tile_country_id, map_tiles.name AS tile_name,
                  countries.*
           FROM characters
           JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game"))

    is_neutral_target = character["tile_type"] == "neutral"
    is_enemy_town_target = (
        character["tile_type"] in ("fortress", "town")
        and character["tile_country_id"] is not None
        and character["tile_country_id"] != character["id"]
    )
    if not is_neutral_target and not is_enemy_town_target:
        db.close()
        flash("這裡沒有可以攻打的敵方據點")
        return redirect(url_for("game"))

    # Garrisoning anywhere (not necessarily at this tile) blocks attacking --
    # you can't defend your own country's tiles and attack an enemy tile in
    # the same breath. Withdrawal-and-attack combine into this one action
    # once the player confirms, rather than wasting a separate turn.
    own_garrison = db.execute(
        "SELECT id FROM garrisons WHERE character_id = ?", (character["character_id"],)
    ).fetchone()
    if own_garrison is not None and request.form.get("confirm_withdraw_garrison") != "1":
        db.close()
        flash("你目前正在駐防中，攻打前必須先撤離駐防，請確認是否撤離並攻打")
        return _render_game(pending_conquer_confirm=True)

    settings = db.execute(
        """SELECT turn_wait_seconds, town_defense_level, fortress_defense_level, rebirth_stat_bonus_percent
           FROM game_settings WHERE id = 1"""
    ).fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game"))

    if own_garrison is not None:
        db.execute("DELETE FROM garrisons WHERE id = ?", (own_garrison["id"],))

    if is_neutral_target:
        return _resolve_bandit_conquest(db, character, settings, stats, current_hp, current_mp)

    defending_country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (character["tile_country_id"],)
    ).fetchone()
    tile_name = tile_display_name(character["tile_name"], character["tile_type"])

    # LIFO defender queue: the most recently-stationed garrison at this tile
    # is fought first. Only once every garrisoned defender is cleared does an
    # attack action reach the tile's NPC defense tower.
    defender_row = db.execute(
        """SELECT garrisons.id AS garrison_id, characters.id AS defender_id,
                  characters.name AS defender_name, characters.level, characters.job_class,
                  characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.pvp_battles_count, characters.pvp_wins_count,
                  countries.*
           FROM garrisons
           JOIN characters ON characters.id = garrisons.character_id
           JOIN countries ON countries.id = characters.country_id
           WHERE garrisons.tile_id = ?
           ORDER BY garrisons.stationed_at DESC
           LIMIT 1""",
        (character["current_tile_id"],),
    ).fetchone()

    if defender_row is not None:
        # A PvP defender is on top of the stack -- fight them instead of the
        # tower this action. Their stats are computed fresh at full HP/MP
        # every time (character_final_stats), never their own possibly-
        # damaged current_hp/current_mp from unrelated activity elsewhere --
        # same approach defense_tower_stats already uses for the NPC tower.
        # Their own learned skills do NOT trigger (same simplification
        # defense_tower_stats already has: only the "player" side of
        # run_battle ever gets usable_skills).
        defender_equipped_items = _fetch_equipped_items(db, {
            "equipped_weapon_id": defender_row["equipped_weapon_id"],
            "equipped_armor_id": defender_row["equipped_armor_id"],
            "equipped_accessory_id": defender_row["equipped_accessory_id"],
        })
        defender_stats = character_final_stats(defender_row, defender_equipped_items, settings)
        defender_monster = {
            "name": defender_row["defender_name"],
            "hp": defender_stats["hp"], "atk": defender_stats["str"],
            "def": defender_stats["def"], "agi": defender_stats["agi"],
            "element": defender_row["element"],
        }

        result = run_battle(
            character["character_name"], stats, character["element"], current_hp, defender_monster,
            player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
        )

        # No currency/EXP for either side on a defender-vs-attacker duel --
        # the only reward remains actually flipping the tile, which only
        # happens once the tower itself falls. An attacker loss still
        # forfeits half currency to the defending country's treasury exactly
        # as an ordinary tower loss does.
        currency_lost = 0
        new_currency = character["currency"]
        if result["won"]:
            db.execute("DELETE FROM garrisons WHERE id = ?", (defender_row["garrison_id"],))
            outcome_detail = f"擊敗了駐防於{tile_name}的{defender_row['defender_name']}"
        elif result["timed_out"]:
            outcome_detail = (
                f"攻打{tile_name}時與駐防的{defender_row['defender_name']}戰鬥回合已滿，"
                f"未分勝負，沒有任何諸神幣損失"
            )
        else:
            currency_lost = character["currency"] // 2
            new_currency = character["currency"] - currency_lost
            db.execute(
                "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
                (currency_lost, defending_country["id"]),
            )
            outcome_detail = (
                f"攻打{tile_name}時輸給了駐防的{defender_row['defender_name']}，"
                f"身上{currency_lost}諸神幣被{defending_country['name']}沒收"
            )

        # 貢獻值: only national PvP conquest combat (this branch and the tower
        # branch below) earns contribution -- timed_out is a true no-op, same
        # as it already is for currency.
        if result["won"]:
            attacker_contribution_delta = CONTRIBUTION_ATTACK_WIN
            defender_contribution_delta = CONTRIBUTION_DEFENSE_LOSS
        elif result["timed_out"]:
            attacker_contribution_delta = 0
            defender_contribution_delta = 0
        else:
            attacker_contribution_delta = CONTRIBUTION_ATTACK_LOSS
            defender_contribution_delta = CONTRIBUTION_DEFENSE_WIN

        db.execute(
            """UPDATE characters
               SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
                   pvp_battles_count = pvp_battles_count + 1,
                   pvp_wins_count = pvp_wins_count + ?,
                   contribution = contribution + ?,
                   pending_boss_monster_id = NULL
               WHERE id = ?""",
            (
                new_currency, result["player_hp"], result["player_mp"],
                _next_action_at(settings["turn_wait_seconds"]),
                1 if result["won"] else 0, attacker_contribution_delta, character["character_id"],
            ),
        )
        # Defender's own current_hp/current_mp are NOT touched (point 6 --
        # always a fresh full-stats fight, no persisted damage carryover);
        # only their PvP counters change, and their garrison row is removed
        # above if they lost. A defender loss (attacker won) also starts a
        # 10-minute garrison_cooldown_until so they can't immediately
        # re-station; a defender win sets no cooldown.
        if result["won"]:
            db.execute(
                """UPDATE characters
                   SET pvp_battles_count = pvp_battles_count + 1,
                       pvp_wins_count = pvp_wins_count + 0,
                       contribution = contribution + ?,
                       garrison_cooldown_until = ?
                   WHERE id = ?""",
                (
                    defender_contribution_delta,
                    _next_action_at(GARRISON_DEFENSE_LOSS_COOLDOWN_SECONDS),
                    defender_row["defender_id"],
                ),
            )
        else:
            db.execute(
                """UPDATE characters
                   SET pvp_battles_count = pvp_battles_count + 1,
                       pvp_wins_count = pvp_wins_count + ?,
                       contribution = contribution + ?
                   WHERE id = ?""",
                (
                    1,
                    defender_contribution_delta,
                    defender_row["defender_id"],
                ),
            )
        log_activity(
            db, session["user_id"], session["username"],
            "conquer_win" if result["won"] else "conquer_loss",
            detail=outcome_detail, ip_address=request.remote_addr,
        )
        db.commit()
        db.close()

        return render_template(
            "battle.html",
            conquest=True,
            captured_tile_name=tile_name,
            defending_country_name=defending_country["name"],
            monster=defender_monster,
            log=result["log"],
            won=result["won"],
            timed_out=result["timed_out"],
            currency_lost=currency_lost,
            player_hp=result["player_hp"],
            max_hp=stats["hp"],
            player_mp=result["player_mp"],
            max_mp=stats["mp"],
            player_stats=stats,
        )

    # No PvP defender remains at this tile -- proceed exactly as the
    # existing tower-fight logic, plus mayor assignment on a town capture.
    tower = defense_tower_stats(defending_country, character["tile_type"], settings)

    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, tower,
        player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
    )

    currency_lost = 0
    if result["won"]:
        new_currency = character["currency"]
        db.execute(
            "UPDATE map_tiles SET country_id = ?, mayor_character_id = ? WHERE id = ?",
            (
                character["id"],
                character["character_id"] if character["tile_type"] == "town" else None,
                character["current_tile_id"],
            ),
        )
        outcome_detail = f"攻下{tile_name}（原屬{defending_country['name']}）"
    elif result["timed_out"]:
        new_currency = character["currency"]
        outcome_detail = f"攻打{tile_name}戰鬥回合已滿，未分勝負，沒有任何諸神幣損失"
    else:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
            (currency_lost, defending_country["id"]),
        )
        outcome_detail = (
            f"攻打{tile_name}失敗，身上{currency_lost}諸神幣被{defending_country['name']}沒收"
        )

    # 貢獻值: NPC tower fight -- attacker-only, no defender to award (there is
    # no defender_row in this branch). timed_out stays a no-op.
    if result["won"]:
        tower_attacker_contribution_delta = CONTRIBUTION_ATTACK_WIN
    elif result["timed_out"]:
        tower_attacker_contribution_delta = 0
    else:
        tower_attacker_contribution_delta = CONTRIBUTION_ATTACK_LOSS

    db.execute(
        """UPDATE characters
           SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               contribution = contribution + ?,
               pending_boss_monster_id = NULL
           WHERE id = ?""",
        (
            new_currency, result["player_hp"], result["player_mp"], _next_action_at(settings["turn_wait_seconds"]),
            1 if result["won"] else 0, tower_attacker_contribution_delta, character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "conquer_win" if result["won"] else "conquer_loss",
        detail=outcome_detail, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    return render_template(
        "battle.html",
        conquest=True,
        captured_tile_name=tile_name,
        defending_country_name=defending_country["name"],
        monster=tower,
        log=result["log"],
        won=result["won"],
        timed_out=result["timed_out"],
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
    )


@app.route("/game/garrison/station", methods=["POST"])
@character_required
def game_garrison_station():
    db = get_db()
    character = db.execute(
        "SELECT id, current_tile_id, country_id, garrison_cooldown_until FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()

    remaining_cooldown = _cooldown_remaining_seconds(character["garrison_cooldown_until"])
    if remaining_cooldown > 0:
        db.close()
        flash(f"防守失敗後需要等待才能重新駐防，還需 {_format_duration(remaining_cooldown)}")
        return redirect(url_for("game"))

    existing = db.execute(
        "SELECT id, tile_id FROM garrisons WHERE character_id = ?", (character["id"],)
    ).fetchone()
    if existing is not None:
        db.close()
        if existing["tile_id"] == character["current_tile_id"]:
            flash("你已經駐防在這裡了")
        else:
            flash("你已經在別處駐防中，請先撤離駐防")
        return redirect(url_for("game"))

    tile = db.execute(
        "SELECT id, tile_type, country_id, name FROM map_tiles WHERE id = ?",
        (character["current_tile_id"],),
    ).fetchone()
    if (
        tile is None
        or tile["tile_type"] not in ("fortress", "town")
        or tile["country_id"] != character["country_id"]
    ):
        db.close()
        flash("只能在自己國家的要塞或城鎮駐防")
        return redirect(url_for("game"))

    db.execute(
        "INSERT INTO garrisons (character_id, tile_id) VALUES (?, ?)",
        (character["id"], tile["id"]),
    )
    tile_name = tile_display_name(tile["name"], tile["tile_type"])
    log_activity(
        db, session["user_id"], session["username"], "garrison_station",
        detail=tile_name, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已駐防於「{tile_name}」")
    return redirect(url_for("game"))


@app.route("/game/garrison/withdraw", methods=["POST"])
@character_required
def game_garrison_withdraw():
    db = get_db()
    character = db.execute(
        "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    deleted = db.execute(
        "DELETE FROM garrisons WHERE character_id = ?", (character["id"],)
    ).rowcount
    if deleted:
        log_activity(
            db, session["user_id"], session["username"], "garrison_withdraw",
            ip_address=request.remote_addr,
        )
    db.commit()
    db.close()
    flash("已撤離駐防" if deleted else "你目前沒有在駐防")
    return redirect(url_for("game"))


@app.route("/game/recover", methods=["POST"])
@character_required
def game_recover():
    db = get_db()
    character = db.execute(
        """SELECT characters.id, characters.level, characters.next_action_at, characters.currency,
                  characters.current_hp, characters.current_mp, map_tiles.tile_type,
                  map_tiles.country_id AS tile_country_id,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.job_class, characters.job_tier,
                  characters.rebirth_count, characters.stat_floor_hp, characters.stat_floor_mp,
                  characters.stat_floor_str, characters.stat_floor_def, characters.stat_floor_agi,
                  characters.stat_floor_luk, characters.level_bonus_hp, characters.level_bonus_mp,
                  characters.level_bonus_str, characters.level_bonus_def, characters.level_bonus_agi,
                  characters.level_bonus_luk, countries.*
           FROM characters
           JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內回復 HP／MP")
        return redirect(url_for("game"))

    settings = db.execute(
        "SELECT turn_wait_seconds, heal_cost_per_point, rebirth_stat_bonus_percent FROM game_settings WHERE id = 1"
    ).fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    missing = (stats["hp"] - current_hp) + (stats["mp"] - current_mp)
    cost = round(missing * settings["heal_cost_per_point"])
    if cost > character["currency"]:
        if current_hp <= 0:
            # Stuck-safety valve: HP is fully gone and can't afford a full
            # heal -- still heal, just take every last coin instead of
            # blocking the player from ever recovering.
            cost = character["currency"]
        else:
            db.close()
            flash(f"諸神幣不足，完全回復需要 {cost} 諸神幣")
            return redirect(url_for("game"))

    db.execute(
        """UPDATE characters SET current_hp = ?, current_mp = ?, currency = currency - ?,
               next_action_at = ?, pending_boss_monster_id = NULL WHERE id = ?""",
        (stats["hp"], stats["mp"], cost, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    if cost and character["tile_country_id"] is not None:
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
            (cost, character["tile_country_id"]),
        )
    log_activity(
        db, session["user_id"], session["username"], "recover",
        detail=f"花費 {cost} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"HP／MP 已完全回復，花費 {cost} 諸神幣")
    return redirect(url_for("game"))


def _character_for_shop(db):
    return db.execute(
        """SELECT characters.id, characters.currency, characters.bank_balance,
                  characters.next_action_at, characters.country_id, map_tiles.tile_type,
                  map_tiles.country_id AS tile_country_id,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.contribution, characters.donated_today, characters.donated_today_date
           FROM characters JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


@app.route("/game/shop")
@character_required
def game_shop():
    db = get_db()
    character = _character_for_shop(db)

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內使用商店")
        return redirect(url_for("game"))

    settings = db.execute(
        "SELECT turn_wait_seconds, sell_back_percent FROM game_settings WHERE id = 1"
    ).fetchone()

    all_items = db.execute(
        """SELECT items.*, countries.name AS set_country_name
           FROM items LEFT JOIN countries ON countries.id = items.country_id
           WHERE items.country_id IS NULL OR items.country_id = ?
           ORDER BY items.shop_type, items.price""",
        (character["tile_country_id"],),
    ).fetchall()
    shop_items = {shop_type: [] for shop_type in SHOP_TYPE_LABELS}
    for item in all_items:
        shop_items[item["shop_type"]].append(item)

    inventory_rows = db.execute(
        """SELECT items.*, inventory.quantity AS quantity
           FROM inventory JOIN items ON items.id = inventory.item_id
           WHERE inventory.character_id = ?
           ORDER BY items.shop_type, items.price""",
        (character["id"],),
    ).fetchall()
    inventory_items = {shop_type: [] for shop_type in SHOP_TYPE_LABELS}
    for row in inventory_rows:
        inventory_items[row["shop_type"]].append(row)

    equipped_slots = []
    suggestions = []
    for shop_type, label in SLOT_LABELS.items():
        slot_column = EQUIP_SLOT_COLUMNS[shop_type]
        equipped_item_id = character[slot_column]
        equipped_item = (
            db.execute("SELECT * FROM items WHERE id = ?", (equipped_item_id,)).fetchone()
            if equipped_item_id else None
        )
        equipped_slots.append({"slot": shop_type, "label": label, "item": equipped_item})

        candidates = inventory_items[shop_type]
        if candidates:
            best = max(candidates, key=lambda i: i["stat_bonus"])
            if equipped_item is None or best["stat_bonus"] > equipped_item["stat_bonus"]:
                suggestions.append({
                    "slot": shop_type,
                    "label": label,
                    "item": best,
                    "current": equipped_item,
                })

    db.close()

    return render_template(
        "shop.html",
        character=character,
        cooldown_seconds=_cooldown_remaining_seconds(character["next_action_at"]),
        shop_items=shop_items,
        shop_type_labels=SHOP_TYPE_LABELS,
        inventory_items=inventory_items,
        sell_back_percent=settings["sell_back_percent"],
        equipped_slots=equipped_slots,
        suggestions=suggestions,
    )


@app.route("/game/shop/buy", methods=["POST"])
@character_required
def game_shop_buy():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game_shop"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內的商店購買裝備")
        return redirect(url_for("game"))

    item_ids = [i for i in request.form.getlist("item_ids") if i]
    if not item_ids:
        db.close()
        flash("請至少選擇一件要購買的裝備")
        return redirect(url_for("game_shop"))

    placeholders = ",".join("?" for _ in item_ids)
    items = db.execute(f"SELECT * FROM items WHERE id IN ({placeholders})", item_ids).fetchall()
    if not items:
        db.close()
        flash("請選擇有效的商品")
        return redirect(url_for("game_shop"))

    total_price = sum(item["price"] for item in items)
    if character["currency"] < total_price:
        db.close()
        flash(f"諸神幣不足，這次購買需要 {total_price} 諸神幣")
        return redirect(url_for("game_shop"))

    settings = db.execute(
        "SELECT turn_wait_seconds, shop_tax_percent FROM game_settings WHERE id = 1"
    ).fetchone()
    for item in items:
        _add_to_inventory(db, character["id"], item["id"], 1)
    db.execute(
        "UPDATE characters SET currency = currency - ?, next_action_at = ? WHERE id = ?",
        (total_price, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    tax = round(total_price * settings["shop_tax_percent"] / 100)
    if tax:
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (tax, character["country_id"])
        )
    names = "、".join(item["name"] for item in items)
    log_activity(
        db, session["user_id"], session["username"], "shop_buy",
        detail=f"{names} ({total_price} 諸神幣)", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已購買「{names}」，放入背包")
    return redirect(url_for("game_shop"))


@app.route("/game/shop/sell", methods=["POST"])
@character_required
def game_shop_sell():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game_shop"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內的商店出售裝備")
        return redirect(url_for("game"))

    item_ids = [i for i in request.form.getlist("item_ids") if i]
    if not item_ids:
        db.close()
        flash("請至少選擇一件要出售的裝備")
        return redirect(url_for("game_shop"))

    settings = db.execute(
        "SELECT turn_wait_seconds, sell_back_percent, shop_tax_percent FROM game_settings WHERE id = 1"
    ).fetchone()

    sold_names = []
    total_refund = 0
    for item_id in item_ids:
        try:
            qty = int(request.form.get(f"qty_{item_id}", "1"))
        except ValueError:
            qty = 1
        row = db.execute(
            """SELECT items.*, inventory.quantity AS owned
               FROM inventory JOIN items ON items.id = inventory.item_id
               WHERE inventory.character_id = ? AND inventory.item_id = ?""",
            (character["id"], item_id),
        ).fetchone()
        if row is None:
            continue
        qty = max(1, min(qty, row["owned"]))
        if not _remove_from_inventory(db, character["id"], row["id"], qty):
            continue
        refund = round(row["price"] * settings["sell_back_percent"] / 100) * qty
        total_refund += refund
        sold_names.append(f"{row['name']} x{qty}" if qty > 1 else row["name"])

    if not sold_names:
        db.close()
        flash("背包裡沒有可以出售的裝備")
        return redirect(url_for("game_shop"))

    db.execute(
        "UPDATE characters SET currency = currency + ?, next_action_at = ? WHERE id = ?",
        (total_refund, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    tax = round(total_refund * settings["shop_tax_percent"] / 100)
    if tax:
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (tax, character["country_id"])
        )
    log_activity(
        db, session["user_id"], session["username"], "shop_sell",
        detail=f"{'、'.join(sold_names)} (+{total_refund} 諸神幣)", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已出售「{'、'.join(sold_names)}」，獲得 {total_refund} 諸神幣")
    return redirect(url_for("game_shop"))


BANK_AMOUNT_UNIT = 1000

# 貢獻值 (contribution) constants -- earned via national PvP conquest combat
# (game_conquer, enemy-country tiles only) and via treasury donations
# (game_treasury_donate). See CLAUDE.md-adjacent design notes in those
# functions for the exact rules.
CONTRIBUTION_ATTACK_WIN = 10
CONTRIBUTION_ATTACK_LOSS = 5
CONTRIBUTION_DEFENSE_WIN = 10
CONTRIBUTION_DEFENSE_LOSS = 5
GARRISON_DEFENSE_LOSS_COOLDOWN_SECONDS = 600
CONTRIBUTION_PER_DONATION_UNIT = 1000
DONATE_DAILY_CAP_DEFAULT = 10000
DONATE_DAILY_CAP_KING = 20000
DONATE_DAILY_CAP_OFFICER = 15000


def _parse_bank_amount(raw):
    """Bank deposits/withdrawals and treasury donations must be a positive
    multiple of BANK_AMOUNT_UNIT -- no other amount is accepted."""
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or amount % BANK_AMOUNT_UNIT != 0:
        return None
    return amount


@app.route("/game/bank/deposit", methods=["POST"])
@character_required
def game_bank_deposit():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內使用銀行")
        return redirect(url_for("game"))

    amount = _parse_bank_amount(request.form.get("amount", ""))
    if amount is None:
        db.close()
        flash(f"存入金額必須是 {BANK_AMOUNT_UNIT} 的倍數")
        return redirect(url_for("game"))
    if amount > character["currency"]:
        db.close()
        flash("存入金額不可超過身上諸神幣數量")
        return redirect(url_for("game"))

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        """UPDATE characters SET currency = currency - ?, bank_balance = bank_balance + ?,
               next_action_at = ? WHERE id = ?""",
        (amount, amount, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "bank_deposit",
        detail=f"存入 {amount} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已存入 {amount} 諸神幣")
    return redirect(url_for("game"))


@app.route("/game/bank/withdraw", methods=["POST"])
@character_required
def game_bank_withdraw():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內使用銀行")
        return redirect(url_for("game"))

    amount = _parse_bank_amount(request.form.get("amount", ""))
    if amount is None:
        db.close()
        flash(f"提領金額必須是 {BANK_AMOUNT_UNIT} 的倍數")
        return redirect(url_for("game"))
    if amount > character["bank_balance"]:
        db.close()
        flash("提領金額不可超過銀行存款數量")
        return redirect(url_for("game"))

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        """UPDATE characters SET currency = currency + ?, bank_balance = bank_balance - ?,
               next_action_at = ? WHERE id = ?""",
        (amount, amount, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "bank_withdraw",
        detail=f"提領 {amount} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已提領 {amount} 諸神幣")
    return redirect(url_for("game"))


@app.route("/game/treasury/donate", methods=["POST"])
@character_required
def game_treasury_donate():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內捐獻給國庫")
        return redirect(url_for("game"))

    amount = _parse_bank_amount(request.form.get("amount", ""))
    if amount is None:
        db.close()
        flash(f"捐獻金額必須是 {BANK_AMOUNT_UNIT} 的倍數")
        return redirect(url_for("game"))
    if amount > character["currency"]:
        db.close()
        flash("捐獻金額不可超過身上諸神幣數量")
        return redirect(url_for("game"))

    # 貢獻值 daily donation cap: depends on the character's CURRENT government
    # role in their own country (國王 > 參謀/大將軍 > 一般人), resets when the
    # UTC calendar date changes.
    country_roles = db.execute(
        "SELECT king_character_id, advisor_character_id, general_character_id FROM countries WHERE id = ?",
        (character["country_id"],),
    ).fetchone()
    if country_roles is not None and character["id"] == country_roles["king_character_id"]:
        donate_cap = DONATE_DAILY_CAP_KING
    elif country_roles is not None and character["id"] in (
        country_roles["advisor_character_id"], country_roles["general_character_id"],
    ):
        donate_cap = DONATE_DAILY_CAP_OFFICER
    else:
        donate_cap = DONATE_DAILY_CAP_DEFAULT

    today = datetime.utcnow().strftime("%Y-%m-%d")
    donated_so_far = character["donated_today"] if character["donated_today_date"] == today else 0
    if donated_so_far + amount > donate_cap:
        db.close()
        flash(
            f"今日捐獻已達上限（{donate_cap} 諸神幣），"
            f"今天還可以捐獻 {max(0, donate_cap - donated_so_far)} 諸神幣"
        )
        return redirect(url_for("game"))

    contribution_gained = amount // CONTRIBUTION_PER_DONATION_UNIT
    new_donated_today = donated_so_far + amount

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        """UPDATE characters
           SET currency = currency - ?, next_action_at = ?,
               contribution = contribution + ?,
               donated_today = ?, donated_today_date = ?
           WHERE id = ?""",
        (
            amount, _next_action_at(settings["turn_wait_seconds"]),
            contribution_gained, new_donated_today, today, character["id"],
        ),
    )
    db.execute(
        "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (amount, character["country_id"])
    )
    log_activity(
        db, session["user_id"], session["username"], "treasury_donate",
        detail=f"捐獻 {amount} 諸神幣給國庫，獲得 {contribution_gained} 貢獻值",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已捐獻 {amount} 諸神幣給國庫，獲得 {contribution_gained} 貢獻值")
    return redirect(url_for("game"))


EQUIPMENT_RETURN_ENDPOINTS = {
    "shop": "game_shop",
    "character": "character_page",
}


def _equipment_return_redirect(request):
    endpoint = EQUIPMENT_RETURN_ENDPOINTS.get(request.form.get("next", "shop"), "game_shop")
    return redirect(url_for(endpoint))


@app.route("/game/equip", methods=["POST"])
@character_required
def game_equip():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return _equipment_return_redirect(request)

    item = db.execute(
        "SELECT * FROM items WHERE id = ?", (request.form.get("item_id", ""),)
    ).fetchone()
    if item is None:
        db.close()
        flash("請選擇一個有效的裝備")
        return _equipment_return_redirect(request)

    if not _remove_from_inventory(db, character["id"], item["id"], 1):
        db.close()
        flash("背包裡沒有這件裝備")
        return _equipment_return_redirect(request)

    slot_column = EQUIP_SLOT_COLUMNS[item["shop_type"]]
    old_item_id = character[slot_column]
    if old_item_id:
        _add_to_inventory(db, character["id"], old_item_id, 1)

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        f"UPDATE characters SET {slot_column} = ?, next_action_at = ? WHERE id = ?",
        (item["id"], _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "equip",
        detail=item["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已裝備「{item['name']}」")
    return _equipment_return_redirect(request)


@app.route("/game/unequip", methods=["POST"])
@character_required
def game_unequip():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return _equipment_return_redirect(request)

    slot = request.form.get("slot", "")
    slot_column = EQUIP_SLOT_COLUMNS.get(slot)
    if slot_column is None:
        db.close()
        flash("請選擇一個有效的裝備部位")
        return _equipment_return_redirect(request)

    item_id = character[slot_column]
    if not item_id:
        db.close()
        flash("該部位目前沒有裝備任何東西")
        return _equipment_return_redirect(request)

    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    _add_to_inventory(db, character["id"], item_id, 1)

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        f"UPDATE characters SET {slot_column} = NULL, next_action_at = ? WHERE id = ?",
        (_next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "unequip",
        detail=item["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已卸下「{item['name']}」，放回背包")
    return _equipment_return_redirect(request)


@app.route("/countries")
@character_required
def countries_page():
    db = get_db()
    countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
    tile_counts = {
        row["country_id"]: row["c"]
        for row in db.execute(
            "SELECT country_id, COUNT(*) AS c FROM map_tiles WHERE country_id IS NOT NULL GROUP BY country_id"
        ).fetchall()
    }
    character_names = {
        row["id"]: row["name"] for row in db.execute("SELECT id, name FROM characters").fetchall()
    }

    # Garrison headcounts are only ever computed (and only ever shown) for
    # the viewing character's OWN country, and only when that character
    # holds one of its government seats -- never exposed for other countries.
    own_character = db.execute(
        "SELECT id, country_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    own_country = next((c for c in countries if c["id"] == own_character["country_id"]), None)
    is_officer = own_country is not None and own_character["id"] in (
        own_country["king_character_id"],
        own_country["advisor_character_id"],
        own_country["general_character_id"],
    )
    garrison_tiles_by_country_id = {}
    if is_officer:
        tile_rows = db.execute(
            """SELECT map_tiles.id AS tile_id, map_tiles.name, map_tiles.tile_type,
                      COUNT(garrisons.id) AS garrison_count
               FROM map_tiles LEFT JOIN garrisons ON garrisons.tile_id = map_tiles.id
               WHERE map_tiles.country_id = ? AND map_tiles.tile_type IN ('fortress', 'town')
               GROUP BY map_tiles.id
               ORDER BY map_tiles.tile_type DESC, map_tiles.name""",
            (own_country["id"],),
        ).fetchall()
        garrison_tiles_by_country_id[own_country["id"]] = [
            {
                "name": tile_display_name(r["name"], r["tile_type"]),
                "garrison_count": r["garrison_count"],
            }
            for r in tile_rows
        ]
    db.close()

    rows = []
    for c in countries:
        rows.append({
            "name": c["name"],
            "element": c["element"],
            "description": c["description"],
            "treasury": c["treasury"],
            "tile_count": tile_counts.get(c["id"], 0),
            "roles": [
                {"label": role["label"], "holder": character_names.get(c[role["column"]])}
                for role in GOVERNMENT_ROLES
            ],
            "garrison_tiles": garrison_tiles_by_country_id.get(c["id"]),
        })

    return render_template("countries.html", countries=rows, roles=GOVERNMENT_ROLES)


@app.route("/character")
@character_required
def character_page():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp,
                  characters.next_action_at, characters.name AS character_name,
                  characters.current_hp, characters.current_mp, characters.job_class,
                  characters.job_tier, characters.rebirth_count, characters.currency,
                  characters.stat_floor_hp,
                  characters.stat_floor_mp, characters.stat_floor_str, characters.stat_floor_def,
                  characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.battles_count,
                  characters.wins_count, characters.pvp_battles_count, characters.pvp_wins_count,
                  characters.equipped_skill_1, characters.equipped_skill_2,
                  characters.rename_count, characters.contribution,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    mastery_names = [
        row["job_name"] for row in db.execute(
            "SELECT job_name FROM job_masteries WHERE character_id = ?", (character["character_id"],)
        )
    ]
    learned_keys = _learned_skill_keys(db, character["character_id"])
    equipped_items = _fetch_equipped_items(db, character)

    equipped_slots = []
    for shop_type, label in SLOT_LABELS.items():
        item_id = character[EQUIP_SLOT_COLUMNS[shop_type]]
        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone() if item_id else None
        equipped_slots.append({"slot": shop_type, "label": label, "item": item})

    inventory_rows = db.execute(
        """SELECT items.*, inventory.quantity AS quantity
           FROM inventory JOIN items ON items.id = inventory.item_id
           WHERE inventory.character_id = ?
           ORDER BY items.shop_type, items.price""",
        (character["character_id"],),
    ).fetchall()
    inventory_items = {shop_type: [] for shop_type in SHOP_TYPE_LABELS}
    for row in inventory_rows:
        inventory_items[row["shop_type"]].append(row)

    skill_book_rows = db.execute(
        "SELECT skill_key, quantity FROM character_skill_books WHERE character_id = ? AND quantity > 0",
        (character["character_id"],),
    ).fetchall()

    db.close()

    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)
    exp_needed = (
        exp_required_for_level(character["level"], settings, force_one=session.get("is_admin", False))
        if character["level"] < LEVEL_CAP else None
    )
    can_promote_tier1 = character["job_tier"] == 0 and character["level"] >= 10
    can_promote_tier2 = character["job_tier"] == 1 and character["level"] >= 30
    can_promote_tier3 = character["job_tier"] == 2 and character["level"] >= 70
    mastery_count = len(mastery_names)
    can_promote_tier4 = (
        character["job_tier"] == 3 and character["level"] >= 120
        and character["rebirth_count"] >= 3 and mastery_count >= 3
    )
    can_rebirth = character["job_tier"] == 3 and character["level"] >= 120 and not can_promote_tier4
    mastered = set(mastery_names)
    # A tier2 job whose both tier3 children are already mastered is excluded,
    # so a future rebirth into this family never dead-ends at tier3 with zero
    # choices left; a tier3 job that's already mastered is excluded outright
    # (rebirthing into it again would waste the life re-mastering nothing new).
    tier2_choices = [
        name for name in TIER2_CHILDREN_BY_FAMILY.get(character["job_class"], [])
        if not set(TIER3_CHILDREN_BY_PARENT.get(name, [])) <= mastered
    ]
    tier3_choices = [
        name for name in TIER3_CHILDREN_BY_PARENT.get(character["job_class"], [])
        if name not in mastered
    ]
    win_rate = (
        round(character["wins_count"] / character["battles_count"] * 100, 1)
        if character["battles_count"] else None
    )
    pvp_win_rate = (
        round(character["pvp_wins_count"] / character["pvp_battles_count"] * 100, 1)
        if character["pvp_battles_count"] else None
    )
    overcomes = ELEMENT_OVERCOMES.get(character["element"])
    overcome_by = next(
        (k for k, v in ELEMENT_OVERCOMES.items() if v == character["element"]), None
    )
    active_sets = _active_set_summaries(equipped_items)
    own_element_bonus = _own_element_bonus_summary(equipped_items, character["element"])
    learnable_skills = _learnable_skills(character, learned_keys)
    usable_keys = _usable_skill_keys(character, learned_keys)
    learned_locked_skills = sorted(
        (SKILL_CATALOG[k] for k in learned_keys - usable_keys if k in SKILL_CATALOG),
        key=lambda s: (s["job_tier"], s["slot"]),
    )

    # 4 distinct skill groups shown on this page: (1) equipped (<=2, actually
    # fires in combat -- see _equipped_combat_skills), (2) the "skill library"
    # (learned + currently lineage-usable but not equipped), (3) learnable
    # (existing currency path), (4) learned_locked_skills (existing, above).
    equipped_key_slots = [("1", character["equipped_skill_1"]), ("2", character["equipped_skill_2"])]
    equipped_skills = [
        {"slot": slot, "skill": SKILL_CATALOG[key]}
        for slot, key in equipped_key_slots
        if key and key in SKILL_CATALOG
    ]
    equipped_keys = {key for _, key in equipped_key_slots if key}
    skill_library = [
        s for s in _ordered_usable_skills(usable_keys) if s["key"] not in equipped_keys
    ]
    held_skill_books = sorted(
        (
            {"skill": SKILL_CATALOG[row["skill_key"]], "quantity": row["quantity"]}
            for row in skill_book_rows if row["skill_key"] in SKILL_CATALOG
        ),
        key=lambda entry: entry["skill"]["name"],
    )

    return render_template(
        "character.html",
        character=character,
        stats=stats,
        combat_stats=derived_combat_stats(stats),
        win_rate=win_rate,
        pvp_win_rate=pvp_win_rate,
        overcomes=overcomes,
        overcome_by=overcome_by,
        element_overcome_bonus=round((ELEMENT_OVERCOME_BONUS - 1) * 100),
        element_overcome_penalty=round((1 - ELEMENT_OVERCOME_PENALTY) * 100),
        current_hp=current_hp,
        current_mp=current_mp,
        level_cap=LEVEL_CAP,
        exp_needed=exp_needed,
        equipped_items=equipped_items,
        equipped_slots=equipped_slots,
        active_sets=active_sets,
        own_element_bonus=own_element_bonus,
        inventory_items=inventory_items,
        shop_type_labels=SHOP_TYPE_LABELS,
        cooldown_seconds=_cooldown_remaining_seconds(character["next_action_at"]),
        job_tier_label=JOB_TIER_LABELS.get(character["job_tier"], ""),
        mastery_names=mastery_names,
        can_promote_tier1=can_promote_tier1,
        can_promote_tier2=can_promote_tier2,
        can_promote_tier3=can_promote_tier3,
        can_promote_tier4=can_promote_tier4,
        can_rebirth=can_rebirth,
        tier1_jobs=TIER1_JOBS,
        tier2_choices=tier2_choices,
        tier2_job_info=TIER2_JOBS,
        tier3_choices=tier3_choices,
        tier3_job_info=TIER3_JOBS,
        learnable_skills=learnable_skills,
        equipped_skills=equipped_skills,
        skill_library=skill_library,
        held_skill_books=held_skill_books,
        learned_locked_skills=learned_locked_skills,
        stat_labels=STAT_LABELS,
        stat_reroll_cost=settings["stat_reroll_cost"],
        next_rename_cost=(character["rename_count"] + 1) * 1000,
    )


def _mastered_job_names(db, character_id):
    return {
        row["job_name"] for row in db.execute(
            "SELECT job_name FROM job_masteries WHERE character_id = ?", (character_id,)
        )
    }


def _character_for_promotion(db):
    return db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.job_tier,
                  characters.job_class, characters.rebirth_count, characters.currency,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.country_id AS char_country_id, characters.name AS character_name,
                  characters.rename_count,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


def _snapshot_stat_floor(character, equipped_items, settings):
    """Pre-promotion stats (already folded against any existing floor via
    character_final_stats' own max()) become the new floor -- so a promotion
    can never make any stat go down, chained across multiple promotions."""
    stats = character_final_stats(character, equipped_items, settings)
    return {STAT_FLOOR_COLUMNS[key]: value for key, value in stats.items()}


def _tile_counts(db):
    return {
        row["country_id"]: row["c"]
        for row in db.execute(
            "SELECT country_id, COUNT(*) AS c FROM map_tiles WHERE country_id IS NOT NULL GROUP BY country_id"
        ).fetchall()
    }


@app.route("/character/rejoin_country", methods=["GET", "POST"])
@character_required
def character_rejoin_country():
    db = get_db()
    character = _character_for_promotion(db)
    tile_counts = _tile_counts(db)

    if tile_counts.get(character["char_country_id"], 0) >= 1:
        db.close()
        flash("你的國家還沒有滅國，無法加入其他國家")
        return redirect(url_for("character_page"))

    if request.method == "GET":
        countries = db.execute(
            "SELECT * FROM countries WHERE id != ? ORDER BY id", (character["char_country_id"],)
        ).fetchall()
        db.close()
        surviving_countries = [c for c in countries if tile_counts.get(c["id"], 0) >= 1]
        return render_template("rejoin_country.html", countries=surviving_countries)

    new_country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (request.form.get("country_id", ""),)
    ).fetchone()
    if (
        new_country is None
        or new_country["id"] == character["char_country_id"]
        or tile_counts.get(new_country["id"], 0) < 1
    ):
        db.close()
        flash("請選擇一個有效的國家")
        return redirect(url_for("character_page"))

    fortress = db.execute(
        "SELECT id FROM map_tiles WHERE country_id = ? AND tile_type = 'fortress'",
        (new_country["id"],),
    ).fetchone()
    if fortress is None:
        fortress = db.execute(
            "SELECT id FROM map_tiles WHERE country_id = ? LIMIT 1", (new_country["id"],)
        ).fetchone()

    old_country_name = character["name"]
    db.execute(
        "UPDATE characters SET country_id = ?, current_tile_id = ?, contribution = 0 WHERE id = ?",
        (new_country["id"], fortress["id"], character["character_id"]),
    )
    db.execute("DELETE FROM garrisons WHERE character_id = ?", (character["character_id"],))
    log_activity(
        db, session["user_id"], session["username"], "rejoin_country",
        detail=f"{old_country_name} → {new_country['name']}", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"你的國家已滅亡，成功加入「{new_country['name']}」！")
    return redirect(url_for("game"))


@app.route("/character/reroll_stats", methods=["POST"])
@character_required
def character_reroll_stats():
    db = get_db()
    character = _character_for_promotion(db)
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    if character["level"] <= 1:
        db.close()
        flash("尚未升過級，沒有可重洗的屬性點")
        return redirect(url_for("character_page"))

    cost = settings["stat_reroll_cost"]
    if character["currency"] < cost:
        db.close()
        flash(f"諸神幣不足，重洗屬性需要 {cost} 諸神幣")
        return redirect(url_for("character_page"))

    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)

    new_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    for _ in range(character["level"] - 1):
        for stat, points in _roll_level_up_stat_points(character["job_class"], character["job_tier"]).items():
            new_gain[stat] += points * LEVEL_UP_POINT_VALUE[stat]

    db.execute(
        """UPDATE characters SET currency = currency - ?,
               level_bonus_hp = ?, level_bonus_mp = ?, level_bonus_str = ?,
               level_bonus_def = ?, level_bonus_agi = ?, level_bonus_luk = ?
           WHERE id = ?""",
        (
            cost, new_gain["hp"], new_gain["mp"], new_gain["str"],
            new_gain["def"], new_gain["agi"], new_gain["luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "reroll_stats",
        detail=f"花費{cost}諸神幣重洗屬性點", ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["currency"] -= cost
    updated["level_bonus_hp"] = new_gain["hp"]
    updated["level_bonus_mp"] = new_gain["mp"]
    updated["level_bonus_str"] = new_gain["str"]
    updated["level_bonus_def"] = new_gain["def"]
    updated["level_bonus_agi"] = new_gain["agi"]
    updated["level_bonus_luk"] = new_gain["luk"]
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title="屬性重洗完成！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@app.route("/character/rename", methods=["POST"])
@character_required
def character_rename():
    db = get_db()
    character = _character_for_promotion(db)

    new_name = request.form.get("new_name", "").strip()
    old_name = character["character_name"]

    if new_name.lower() == old_name.lower():
        db.close()
        flash("名稱沒有變更")
        return redirect(url_for("character_page"))

    name_error = _validate_character_name(db, new_name, session["username"])
    if name_error:
        db.close()
        flash(name_error)
        return redirect(url_for("character_page"))

    cost = (character["rename_count"] + 1) * 1000
    if character["currency"] < cost:
        db.close()
        flash(f"諸神幣不足，改名需要 {cost} 諸神幣")
        return redirect(url_for("character_page"))

    try:
        db.execute(
            "UPDATE characters SET name = ?, currency = currency - ?, rename_count = rename_count + 1 WHERE id = ?",
            (new_name, cost, character["character_id"]),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        db.close()
        flash("這個名稱剛好被用掉了，請重新整理再試一次")
        return redirect(url_for("character_page"))

    log_activity(
        db, session["user_id"], session["username"], "rename_character",
        detail=f"{old_name} → {new_name}", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    session["character_name"] = new_name
    flash(f"角色名稱已改為「{new_name}」")
    return redirect(url_for("character_page"))


@app.route("/character/promote/tier1", methods=["POST"])
@character_required
def character_promote_tier1():
    db = get_db()
    character = _character_for_promotion(db)

    job_name = request.form.get("job_name", "")
    if character["job_tier"] != 0 or character["level"] < 10:
        db.close()
        flash("目前還不能一轉")
        return redirect(url_for("character_page"))
    if job_name not in TIER1_JOBS:
        db.close()
        flash("請選擇一個有效的職業")
        return redirect(url_for("character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 1,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier1",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 1
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"一轉成功，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@app.route("/character/promote/tier2", methods=["POST"])
@character_required
def character_promote_tier2():
    db = get_db()
    character = _character_for_promotion(db)

    job_name = request.form.get("job_name", "")
    mastered = _mastered_job_names(db, character["character_id"])
    valid_choices = [
        name for name in TIER2_CHILDREN_BY_FAMILY.get(character["job_class"], [])
        if not set(TIER3_CHILDREN_BY_PARENT.get(name, [])) <= mastered
    ]
    if character["job_tier"] != 1 or character["level"] < 30:
        db.close()
        flash("目前還不能二轉")
        return redirect(url_for("character_page"))
    if job_name not in valid_choices:
        db.close()
        flash("請選擇一個有效的職業")
        return redirect(url_for("character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 2,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier2",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 2
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"二轉成功，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@app.route("/character/promote/tier3", methods=["POST"])
@character_required
def character_promote_tier3():
    db = get_db()
    character = _character_for_promotion(db)

    job_name = request.form.get("job_name", "")
    mastered = _mastered_job_names(db, character["character_id"])
    valid_choices = [
        name for name in TIER3_CHILDREN_BY_PARENT.get(character["job_class"], [])
        if name not in mastered
    ]
    if character["job_tier"] != 2 or character["level"] < 70:
        db.close()
        flash("目前還不能三轉")
        return redirect(url_for("character_page"))
    if job_name not in valid_choices:
        db.close()
        flash("請選擇一個有效的職業")
        return redirect(url_for("character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 3,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier3",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 3
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"三轉成功，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@app.route("/character/promote/tier4", methods=["POST"])
@character_required
def character_promote_tier4():
    db = get_db()
    character = _character_for_promotion(db)

    mastery_count = db.execute(
        "SELECT COUNT(*) AS c FROM job_masteries WHERE character_id = ?",
        (character["character_id"],),
    ).fetchone()["c"]
    if not (
        character["job_tier"] == 3 and character["level"] >= 120
        and character["rebirth_count"] >= 3 and mastery_count >= 3
    ):
        db.close()
        flash("目前還不能四轉")
        return redirect(url_for("character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)
    job_name = _resolve_tier4_job(db, character["character_id"])

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 4,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier4",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 4
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"四轉！你已臻至巔峰，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@app.route("/character/rebirth", methods=["POST"])
@character_required
def character_rebirth():
    db = get_db()
    character = _character_for_promotion(db)

    if character["job_tier"] != 3 or character["level"] < 120:
        db.close()
        flash("目前還不能轉生")
        return redirect(url_for("character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)

    db.execute(
        """UPDATE characters
           SET rebirth_count = rebirth_count + 1, level = 10, exp = 0,
               job_class = '初心者', job_tier = 0,
               stat_floor_hp = NULL, stat_floor_mp = NULL, stat_floor_str = NULL,
               stat_floor_def = NULL, stat_floor_agi = NULL, stat_floor_luk = NULL
           WHERE id = ?""",
        (character["character_id"],),
    )
    log_activity(
        db, session["user_id"], session["username"], "rebirth",
        detail=f"第 {character['rebirth_count'] + 1} 次轉生", ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["level"] = 10
    updated["job_class"] = "初心者"
    updated["job_tier"] = 0
    for col in STAT_FLOOR_COLUMNS.values():
        updated[col] = None
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title="轉生完成！等級重置為 10 級，職業回到初心者，準備踏上新的旅程",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@app.route("/character/learn_skill", methods=["POST"])
@character_required
def character_learn_skill():
    db = get_db()
    character = _character_for_promotion(db)

    skill_key = request.form.get("skill_key", "")
    skill = SKILL_CATALOG.get(skill_key)
    learned_keys = _learned_skill_keys(db, character["character_id"])
    learnable_keys = {s["key"] for s in _learnable_skills(character, learned_keys)}

    if skill is None or skill_key not in learnable_keys:
        db.close()
        flash("目前無法學習這個技能")
        return redirect(url_for("character_page"))
    if character["currency"] < skill["learn_cost"]:
        db.close()
        flash(f"諸神幣不足，學習「{skill['name']}」需要 {skill['learn_cost']} 諸神幣")
        return redirect(url_for("character_page"))

    db.execute(
        "UPDATE characters SET currency = currency - ? WHERE id = ?",
        (skill["learn_cost"], character["character_id"]),
    )
    db.execute(
        "INSERT INTO character_skills (character_id, skill_key) VALUES (?, ?)",
        (character["character_id"], skill_key),
    )
    log_activity(
        db, session["user_id"], session["username"], "learn_skill",
        detail=skill["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"學會了「{skill['name']}」！")
    return redirect(url_for("character_page"))


@app.route("/character/skill_book/use", methods=["POST"])
@character_required
def character_skill_book_use():
    db = get_db()
    character = _character_for_promotion(db)

    skill_key = request.form.get("skill_key", "")
    skill = SKILL_CATALOG.get(skill_key)
    book_row = db.execute(
        "SELECT id, quantity FROM character_skill_books WHERE character_id = ? AND skill_key = ?",
        (character["character_id"], skill_key),
    ).fetchone()
    learned_keys = _learned_skill_keys(db, character["character_id"])

    if skill is None or book_row is None or book_row["quantity"] < 1:
        db.close()
        flash("你目前沒有這本技能書")
        return redirect(url_for("character_page"))
    if character["job_tier"] != 4:
        db.close()
        flash("必須先四轉才能使用這本技能書")
        return redirect(url_for("character_page"))
    if skill_key in learned_keys:
        db.close()
        flash("你已經學會這個技能了")
        return redirect(url_for("character_page"))

    if book_row["quantity"] > 1:
        db.execute(
            "UPDATE character_skill_books SET quantity = quantity - 1 WHERE id = ?", (book_row["id"],)
        )
    else:
        db.execute("DELETE FROM character_skill_books WHERE id = ?", (book_row["id"],))
    db.execute(
        "INSERT INTO character_skills (character_id, skill_key) VALUES (?, ?)",
        (character["character_id"], skill_key),
    )
    log_activity(
        db, session["user_id"], session["username"], "learn_skill",
        detail=skill["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"使用技能書學會了「{skill['name']}」！")
    return redirect(url_for("character_page"))


@app.route("/character/equip_skill", methods=["POST"])
@character_required
def character_equip_skill():
    db = get_db()
    character = _character_for_promotion(db)

    skill_key = request.form.get("skill_key", "")
    slot = request.form.get("slot", "")
    if slot not in ("1", "2"):
        db.close()
        flash("請選擇一個有效的技能欄位")
        return redirect(url_for("character_page"))

    learned_keys = _learned_skill_keys(db, character["character_id"])
    usable_keys = _usable_skill_keys(character, learned_keys)
    if skill_key not in usable_keys:
        db.close()
        flash("這個技能目前無法配置")
        return redirect(url_for("character_page"))

    current = db.execute(
        "SELECT equipped_skill_1, equipped_skill_2 FROM characters WHERE id = ?",
        (character["character_id"],),
    ).fetchone()

    # Never let both slots hold the same skill -- clear the other slot first
    # if it already has this skill_key, then (over)write the target slot.
    if slot == "1":
        if current["equipped_skill_2"] == skill_key:
            db.execute(
                "UPDATE characters SET equipped_skill_2 = NULL WHERE id = ?", (character["character_id"],)
            )
        db.execute(
            "UPDATE characters SET equipped_skill_1 = ? WHERE id = ?", (skill_key, character["character_id"])
        )
    else:
        if current["equipped_skill_1"] == skill_key:
            db.execute(
                "UPDATE characters SET equipped_skill_1 = NULL WHERE id = ?", (character["character_id"],)
            )
        db.execute(
            "UPDATE characters SET equipped_skill_2 = ? WHERE id = ?", (skill_key, character["character_id"])
        )
    db.commit()
    db.close()

    skill = SKILL_CATALOG.get(skill_key)
    flash(f"已將「{skill['name'] if skill else skill_key}」配置為技能{slot}")
    return redirect(url_for("character_page"))


@app.route("/character/unequip_skill", methods=["POST"])
@character_required
def character_unequip_skill():
    db = get_db()
    character = _character_for_promotion(db)

    slot = request.form.get("slot", "")
    if slot not in ("1", "2"):
        db.close()
        flash("請選擇一個有效的技能欄位")
        return redirect(url_for("character_page"))

    if slot == "1":
        db.execute(
            "UPDATE characters SET equipped_skill_1 = NULL WHERE id = ?", (character["character_id"],)
        )
    else:
        db.execute(
            "UPDATE characters SET equipped_skill_2 = NULL WHERE id = ?", (character["character_id"],)
        )
    db.commit()
    db.close()

    flash("已卸下技能")
    return redirect(url_for("character_page"))


@app.route("/character/debug/set_level", methods=["POST"])
@admin_required
@character_required
def character_debug_set_level():
    """Admin-only shortcut so the developer's own account can jump straight
    to any level to eyeball stat growth, without grinding real EXP."""
    try:
        level = int(request.form.get("level", ""))
    except ValueError:
        flash("等級格式不正確")
        return redirect(url_for("character_page"))

    level = max(1, min(level, LEVEL_CAP))
    # Debug-jumping a level can't fairly replay the random per-level roll for
    # every level skipped, so it approximates with the old flat formula --
    # good enough for eyeballing roughly how strong that level should feel.
    level_growth = max(0, level - 1)
    db = get_db()
    character = db.execute(
        "SELECT id AS character_id, level, job_class, job_tier, rebirth_count FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()

    # Route through the same mastery-recording check real hunts use, so this
    # shortcut can actually be used to test that flow. 四轉 is no longer an
    # automatic side effect here -- it's now an explicit player action.
    _process_job_progression(db, character, character["level"], level)

    db.execute(
        """UPDATE characters SET level = ?, exp = 0,
               level_bonus_hp = ?, level_bonus_mp = ?, level_bonus_str = ?,
               level_bonus_def = ?, level_bonus_agi = ?, level_bonus_luk = ?
           WHERE id = ?""",
        (
            level,
            LEVEL_STAT_GROWTH["hp"] * level_growth, LEVEL_STAT_GROWTH["mp"] * level_growth,
            LEVEL_STAT_GROWTH["str"] * level_growth, LEVEL_STAT_GROWTH["def"] * level_growth,
            LEVEL_STAT_GROWTH["agi"] * level_growth, LEVEL_STAT_GROWTH["luk"] * level_growth,
            character["character_id"],
        ),
    )
    db.commit()
    db.close()

    flash(f"（除錯）等級已設為 {level}")
    return redirect(url_for("character_page"))


@app.route("/character/debug/set_rebirth", methods=["POST"])
@admin_required
@character_required
def character_debug_set_rebirth():
    """Admin-only shortcut to directly set the rebirth count, so the stacking
    stat bonus can be checked without actually grinding out 3 full lifetimes."""
    try:
        rebirth_count = int(request.form.get("rebirth_count", ""))
    except ValueError:
        flash("轉生次數格式不正確")
        return redirect(url_for("character_page"))

    rebirth_count = max(0, rebirth_count)
    db = get_db()
    db.execute(
        "UPDATE characters SET rebirth_count = ? WHERE user_id = ?",
        (rebirth_count, session["user_id"]),
    )
    db.commit()
    db.close()

    flash(f"（除錯）轉生次數已設為 {rebirth_count}")
    return redirect(url_for("character_page"))


@app.route("/character/debug/reset", methods=["POST"])
@admin_required
@character_required
def character_debug_reset():
    """Admin-only one-click reset back to a brand new 初心者, so the whole
    job/rebirth/four-zhuan flow can be tested again from scratch. Currency,
    country, equipment and inventory are left untouched (same policy as a
    normal rebirth) -- this only wipes progression state."""
    db = get_db()
    character = db.execute(
        "SELECT id AS character_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()

    db.execute("DELETE FROM job_masteries WHERE character_id = ?", (character["character_id"],))
    db.execute(
        """UPDATE characters
           SET level = 1, exp = 0, rebirth_count = 0,
               job_class = '初心者', job_tier = 0,
               current_hp = NULL, current_mp = NULL, pending_boss_monster_id = NULL,
               stat_floor_hp = NULL, stat_floor_mp = NULL, stat_floor_str = NULL,
               stat_floor_def = NULL, stat_floor_agi = NULL, stat_floor_luk = NULL,
               level_bonus_hp = 0, level_bonus_mp = 0, level_bonus_str = 0,
               level_bonus_def = 0, level_bonus_agi = 0, level_bonus_luk = 0
           WHERE id = ?""",
        (character["character_id"],),
    )
    log_activity(
        db, session["user_id"], session["username"], "debug_reset",
        detail="重置為初心者 1 級", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash("（除錯）角色已重置為 1 級初心者，精通紀錄已清空")
    return redirect(url_for("character_page"))


# ---------------------------------------------------------------------------
# Character-to-character trading. No country restriction anywhere. Both
# participants must stay online (users.is_online) for the entire negotiation
# -- re-checked on every room load and on every state-changing POST, not just
# once at invite time. Only inventory rows ever move (equipped items are
# never tradeable, since equipping already removes an item from inventory).
# ---------------------------------------------------------------------------

def _character_for_trade(db):
    return db.execute(
        """SELECT characters.id AS character_id, characters.name AS character_name,
                  characters.currency AS character_currency
           FROM characters WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


def _open_trade_for_character(db, character_id):
    """Broad "do you have any open trade at all" check (pending or active, as
    either initiator or target) -- used to enforce the "at most one open
    trade at a time" rule when a new invite is being created."""
    return db.execute(
        """SELECT id FROM trades
           WHERE status IN ('pending', 'active')
             AND (initiator_character_id = ? OR target_character_id = ?)""",
        (character_id, character_id),
    ).fetchone()


def _trade_for_home_redirect(db, character_id):
    """Narrower than _open_trade_for_character: only trades where this
    character is either (a) the initiator of a still-open (pending or
    active) invite they're waiting on / negotiating, or (b) a participant of
    an already-active (both sides accepted) negotiation. A brand new pending
    invite where this character is the TARGET does NOT match here on
    purpose, so /trade can still show it in the "pending invites" list with
    accept/decline buttons rather than immediately yanking the player into
    the trade room."""
    return db.execute(
        """SELECT id FROM trades
           WHERE (initiator_character_id = ? AND status IN ('pending', 'active'))
              OR (target_character_id = ? AND status = 'active')""",
        (character_id, character_id),
    ).fetchone()


def _load_trade(db, trade_id):
    """Explicit column list + explicit AS aliases throughout (never
    characters.*/users.* wildcards) -- this app's sqlite3.Row resolves
    dict-style access to the LAST matching column name in the SELECT list
    when two joined tables share a column name (e.g. both characters rows
    have an "id" and "name" and "currency" here), so wildcards would silently
    collide between the initiator and target sides."""
    return db.execute(
        """SELECT trades.id AS trade_id, trades.status AS trade_status,
                  trades.initiator_character_id AS initiator_character_id,
                  trades.target_character_id AS target_character_id,
                  trades.initiator_currency AS initiator_currency,
                  trades.target_currency AS target_currency,
                  trades.initiator_confirmed AS initiator_confirmed,
                  trades.target_confirmed AS target_confirmed,
                  trades.created_at AS trade_created_at,
                  trades.updated_at AS trade_updated_at,
                  ic.name AS initiator_name, ic.currency AS initiator_character_currency,
                  ic.user_id AS initiator_user_id, iu.username AS initiator_username,
                  iu.is_online AS initiator_is_online,
                  tc.name AS target_name, tc.currency AS target_character_currency,
                  tc.user_id AS target_user_id, tu.username AS target_username,
                  tu.is_online AS target_is_online
           FROM trades
           JOIN characters AS ic ON ic.id = trades.initiator_character_id
           JOIN users AS iu ON iu.id = ic.user_id
           JOIN characters AS tc ON tc.id = trades.target_character_id
           JOIN users AS tu ON tu.id = tc.user_id
           WHERE trades.id = ?""",
        (trade_id,),
    ).fetchone()


def _cancel_trade(db, trade_id):
    db.execute(
        "UPDATE trades SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
        (trade_id,),
    )


def _auto_cancel_trade_if_offline(db, trade):
    """If the trade is still open (pending/active) and either participant's
    user is no longer online, flips it to cancelled and returns an
    explanatory flash message; returns None if no cancellation was needed.
    Called at the top of every trade view/action route so a stale open tab
    can't be used to keep negotiating after one side has logged out."""
    if trade["trade_status"] not in ("pending", "active"):
        return None
    if trade["initiator_is_online"] and trade["target_is_online"]:
        return None
    _cancel_trade(db, trade["trade_id"])
    return "交易對象已離線，交易已自動取消"


def _trade_opponent_name(trade, character_id):
    return trade["target_name"] if character_id == trade["initiator_character_id"] else trade["initiator_name"]


@app.route("/trade")
@character_required
def trade_home():
    db = get_db()
    character = _character_for_trade(db)

    room_trade = _trade_for_home_redirect(db, character["character_id"])
    if room_trade:
        db.close()
        return redirect(url_for("trade_room", trade_id=room_trade["id"]))

    invites = db.execute(
        """SELECT trades.id AS trade_id, trades.created_at AS trade_created_at,
                  characters.name AS initiator_name
           FROM trades JOIN characters ON characters.id = trades.initiator_character_id
           WHERE trades.target_character_id = ? AND trades.status = 'pending'
           ORDER BY trades.created_at DESC""",
        (character["character_id"],),
    ).fetchall()
    db.close()
    return render_template("trade_home.html", invites=invites)


@app.route("/trade/invite", methods=["POST"])
@character_required
def trade_invite():
    db = get_db()
    character = _character_for_trade(db)
    target_name = request.form.get("target_name", "").strip()

    if not target_name:
        db.close()
        flash("請輸入要邀請交易的角色名稱")
        return redirect(url_for("trade_home"))

    if _open_trade_for_character(db, character["character_id"]):
        db.close()
        flash("你目前已經有進行中的交易")
        return redirect(url_for("trade_home"))

    target = db.execute(
        """SELECT characters.id AS character_id, characters.name AS character_name,
                  users.is_online AS user_is_online
           FROM characters JOIN users ON users.id = characters.user_id
           WHERE lower(characters.name) = lower(?)""",
        (target_name,),
    ).fetchone()

    if target is None:
        db.close()
        flash("找不到這個角色")
        return redirect(url_for("trade_home"))

    if target["character_id"] == character["character_id"]:
        db.close()
        flash("不能邀請自己交易")
        return redirect(url_for("trade_home"))

    if not target["user_is_online"]:
        db.close()
        flash(f"{target['character_name']} 目前不在線上，無法邀請交易")
        return redirect(url_for("trade_home"))

    if _open_trade_for_character(db, target["character_id"]):
        db.close()
        flash(f"{target['character_name']} 目前已經有進行中的交易")
        return redirect(url_for("trade_home"))

    cur = db.execute(
        """INSERT INTO trades (initiator_character_id, target_character_id, status)
           VALUES (?, ?, 'pending')""",
        (character["character_id"], target["character_id"]),
    )
    trade_id = cur.lastrowid
    log_activity(
        db, session["user_id"], session["username"], "trade_invite",
        detail=f"邀請 {target['character_name']} 進行交易", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已邀請 {target['character_name']} 進行交易")
    return redirect(url_for("trade_home"))


@app.route("/trade/<int:trade_id>/accept", methods=["POST"])
@character_required
def trade_accept(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade_home"))

    if character["character_id"] != trade["target_character_id"]:
        db.close()
        flash("只有受邀請的一方可以接受交易")
        return redirect(url_for("trade_home"))

    if trade["trade_status"] != "pending":
        db.close()
        flash("這筆交易目前無法接受")
        return redirect(url_for("trade_home"))

    if not trade["initiator_is_online"]:
        _cancel_trade(db, trade_id)
        db.commit()
        db.close()
        flash(f"{trade['initiator_name']} 已離線，交易已自動取消")
        return redirect(url_for("trade_home"))

    db.execute(
        "UPDATE trades SET status = 'active', updated_at = datetime('now') WHERE id = ?", (trade_id,)
    )
    log_activity(
        db, session["user_id"], session["username"], "trade_accept",
        detail=f"接受與 {trade['initiator_name']} 的交易邀請", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("已接受交易邀請")
    return redirect(url_for("trade_room", trade_id=trade_id))


@app.route("/trade/<int:trade_id>/decline", methods=["POST"])
@character_required
def trade_decline(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade_home"))

    if trade["trade_status"] != "pending":
        db.close()
        flash("這筆交易目前無法拒絕")
        return redirect(url_for("trade_home"))

    _cancel_trade(db, trade_id)
    log_activity(
        db, session["user_id"], session["username"], "trade_decline",
        detail=f"拒絕與 {_trade_opponent_name(trade, character['character_id'])} 的交易邀請",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("已拒絕交易邀請")
    return redirect(url_for("trade_home"))


@app.route("/trade/<int:trade_id>/cancel", methods=["POST"])
@character_required
def trade_cancel(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade_home"))

    if trade["trade_status"] not in ("pending", "active"):
        db.close()
        flash("這筆交易已經結束，無法取消")
        return redirect(url_for("trade_home"))

    _cancel_trade(db, trade_id)
    log_activity(
        db, session["user_id"], session["username"], "trade_cancel",
        detail=f"取消與 {_trade_opponent_name(trade, character['character_id'])} 的交易",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("已取消交易")
    return redirect(url_for("trade_home"))


@app.route("/trade/<int:trade_id>")
@character_required
def trade_room(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade_home"))

    cancel_reason = _auto_cancel_trade_if_offline(db, trade)
    if cancel_reason:
        db.commit()
        db.close()
        flash(cancel_reason)
        return redirect(url_for("trade_home"))

    is_initiator = character["character_id"] == trade["initiator_character_id"]
    my_currency_offer = trade["initiator_currency"] if is_initiator else trade["target_currency"]
    opp_currency_offer = trade["target_currency"] if is_initiator else trade["initiator_currency"]
    my_confirmed = bool(trade["initiator_confirmed"] if is_initiator else trade["target_confirmed"])
    opp_confirmed = bool(trade["target_confirmed"] if is_initiator else trade["initiator_confirmed"])
    opp_character_id = trade["target_character_id"] if is_initiator else trade["initiator_character_id"]
    opp_name = _trade_opponent_name(trade, character["character_id"])

    my_offered_qty_by_item = {
        row["item_id"]: row["quantity"]
        for row in db.execute(
            "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND character_id = ?",
            (trade_id, character["character_id"]),
        ).fetchall()
    }
    my_inventory = db.execute(
        """SELECT items.id AS item_id, items.name AS item_name, items.shop_type AS item_shop_type,
                  inventory.quantity AS inventory_quantity
           FROM inventory JOIN items ON items.id = inventory.item_id
           WHERE inventory.character_id = ?
           ORDER BY items.shop_type, items.name""",
        (character["character_id"],),
    ).fetchall()
    my_offer_rows = [
        {
            "item_id": row["item_id"],
            "name": row["item_name"],
            "shop_type": row["item_shop_type"],
            "max_quantity": row["inventory_quantity"],
            "offered_quantity": my_offered_qty_by_item.get(row["item_id"], 0),
        }
        for row in my_inventory
    ]

    opp_items = db.execute(
        """SELECT items.name AS item_name, trade_items.quantity AS quantity
           FROM trade_items JOIN items ON items.id = trade_items.item_id
           WHERE trade_items.trade_id = ? AND trade_items.character_id = ?
           ORDER BY items.shop_type, items.name""",
        (trade_id, opp_character_id),
    ).fetchall()

    db.close()
    return render_template(
        "trade_room.html",
        trade=trade,
        is_initiator=is_initiator,
        opp_name=opp_name,
        my_currency_offer=my_currency_offer,
        opp_currency_offer=opp_currency_offer,
        my_confirmed=my_confirmed,
        opp_confirmed=opp_confirmed,
        my_offer_rows=my_offer_rows,
        opp_items=opp_items,
        my_currency_available=character["character_currency"],
    )


@app.route("/trade/<int:trade_id>/offer", methods=["POST"])
@character_required
def trade_offer(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade_home"))

    cancel_reason = _auto_cancel_trade_if_offline(db, trade)
    if cancel_reason:
        db.commit()
        db.close()
        flash(cancel_reason)
        return redirect(url_for("trade_home"))

    if trade["trade_status"] != "active":
        db.close()
        flash("這筆交易目前無法調整報價")
        return redirect(url_for("trade_home"))

    raw_amount = request.form.get("currency", "0").strip()
    try:
        amount = int(raw_amount)
    except ValueError:
        amount = 0
    amount = max(0, min(amount, character["character_currency"]))

    inventory_rows = db.execute(
        "SELECT item_id, quantity FROM inventory WHERE character_id = ?",
        (character["character_id"],),
    ).fetchall()

    db.execute(
        "DELETE FROM trade_items WHERE trade_id = ? AND character_id = ?",
        (trade_id, character["character_id"]),
    )
    for row in inventory_rows:
        raw_qty = request.form.get(f"item_qty_{row['item_id']}", "0").strip()
        try:
            qty = int(raw_qty)
        except ValueError:
            qty = 0
        qty = max(0, min(qty, row["quantity"]))
        if qty > 0:
            db.execute(
                "INSERT INTO trade_items (trade_id, character_id, item_id, quantity) VALUES (?, ?, ?, ?)",
                (trade_id, character["character_id"], row["item_id"], qty),
            )

    is_initiator = character["character_id"] == trade["initiator_character_id"]
    currency_column = "initiator_currency" if is_initiator else "target_currency"
    # Any successful offer update resets BOTH confirmation flags -- standard
    # anti-scam trade-window behavior so a stale confirmation never silently
    # carries over onto a changed offer.
    db.execute(
        f"""UPDATE trades SET {currency_column} = ?, initiator_confirmed = 0, target_confirmed = 0,
               updated_at = datetime('now') WHERE id = ?""",
        (amount, trade_id),
    )
    db.commit()
    db.close()
    flash("已更新你的交易報價")
    return redirect(url_for("trade_room", trade_id=trade_id))


@app.route("/trade/<int:trade_id>/confirm", methods=["POST"])
@character_required
def trade_confirm(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade_home"))

    cancel_reason = _auto_cancel_trade_if_offline(db, trade)
    if cancel_reason:
        db.commit()
        db.close()
        flash(cancel_reason)
        return redirect(url_for("trade_home"))

    if trade["trade_status"] != "active":
        db.close()
        flash("這筆交易目前無法確認")
        return redirect(url_for("trade_home"))

    is_initiator = character["character_id"] == trade["initiator_character_id"]
    confirm_column = "initiator_confirmed" if is_initiator else "target_confirmed"
    db.execute(
        f"UPDATE trades SET {confirm_column} = 1, updated_at = datetime('now') WHERE id = ?",
        (trade_id,),
    )

    trade = _load_trade(db, trade_id)
    if not (trade["initiator_confirmed"] and trade["target_confirmed"]):
        db.commit()
        db.close()
        flash("已確認你的交易報價，等待對方確認")
        return redirect(url_for("trade_room", trade_id=trade_id))

    # Both sides just confirmed -- attempt to finalize. Re-check online
    # status first (cheap), then re-validate both sides still actually have
    # what they offered (defensive: something may have changed since the
    # offer was set, e.g. spent currency or sold/equipped an item elsewhere).
    if not (trade["initiator_is_online"] and trade["target_is_online"]):
        _cancel_trade(db, trade_id)
        db.commit()
        db.close()
        flash("交易對象已離線，交易已自動取消")
        return redirect(url_for("trade_home"))

    initiator_items = db.execute(
        "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND character_id = ?",
        (trade_id, trade["initiator_character_id"]),
    ).fetchall()
    target_items = db.execute(
        "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND character_id = ?",
        (trade_id, trade["target_character_id"]),
    ).fetchall()

    valid = (
        trade["initiator_currency"] <= trade["initiator_character_currency"]
        and trade["target_currency"] <= trade["target_character_currency"]
    )
    if valid:
        for row in initiator_items:
            have = db.execute(
                "SELECT quantity FROM inventory WHERE character_id = ? AND item_id = ?",
                (trade["initiator_character_id"], row["item_id"]),
            ).fetchone()
            if have is None or have["quantity"] < row["quantity"]:
                valid = False
                break
    if valid:
        for row in target_items:
            have = db.execute(
                "SELECT quantity FROM inventory WHERE character_id = ? AND item_id = ?",
                (trade["target_character_id"], row["item_id"]),
            ).fetchone()
            if have is None or have["quantity"] < row["quantity"]:
                valid = False
                break

    if not valid:
        db.execute(
            """UPDATE trades SET initiator_confirmed = 0, target_confirmed = 0,
                   updated_at = datetime('now') WHERE id = ?""",
            (trade_id,),
        )
        db.commit()
        db.close()
        flash("交易物品或諸神幣數量有變動，請重新確認雙方報價")
        return redirect(url_for("trade_room", trade_id=trade_id))

    db.execute(
        "UPDATE characters SET currency = currency + ? WHERE id = ?",
        (trade["target_currency"] - trade["initiator_currency"], trade["initiator_character_id"]),
    )
    db.execute(
        "UPDATE characters SET currency = currency + ? WHERE id = ?",
        (trade["initiator_currency"] - trade["target_currency"], trade["target_character_id"]),
    )
    for row in initiator_items:
        _remove_from_inventory(db, trade["initiator_character_id"], row["item_id"], row["quantity"])
        _add_to_inventory(db, trade["target_character_id"], row["item_id"], row["quantity"])
    for row in target_items:
        _remove_from_inventory(db, trade["target_character_id"], row["item_id"], row["quantity"])
        _add_to_inventory(db, trade["initiator_character_id"], row["item_id"], row["quantity"])

    db.execute(
        "UPDATE trades SET status = 'completed', updated_at = datetime('now') WHERE id = ?",
        (trade_id,),
    )
    log_activity(
        db, trade["initiator_user_id"], trade["initiator_username"], "trade_complete",
        detail=f"與 {trade['target_name']} 完成交易", ip_address=request.remote_addr,
    )
    log_activity(
        db, trade["target_user_id"], trade["target_username"], "trade_complete",
        detail=f"與 {trade['initiator_name']} 完成交易", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("交易完成！")
    return redirect(url_for("trade_room", trade_id=trade_id))


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
    characters = db.execute("SELECT id, name, country_id, is_npc FROM characters ORDER BY name").fetchall()
    total_views = db.execute("SELECT total_views FROM site_visits WHERE id = 1").fetchone()["total_views"]
    unique_visitors = db.execute("SELECT COUNT(*) AS c FROM site_visitors").fetchone()["c"]
    db.close()

    characters_by_country = {}
    for c in characters:
        characters_by_country.setdefault(c["country_id"], []).append(c)

    return render_template(
        "admin.html", countries=countries, characters_by_country=characters_by_country,
        roles=GOVERNMENT_ROLES, active_tab="countries",
        total_views=total_views, unique_visitors=unique_visitors,
    )


@app.route("/admin/sessions")
@admin_required
def admin_sessions():
    db = get_db()
    users = db.execute(
        "SELECT username, is_admin, is_online, last_login_at, last_seen_at "
        "FROM users WHERE is_npc = 0 ORDER BY last_seen_at IS NULL, last_seen_at DESC"
    ).fetchall()
    db.close()

    now = datetime.utcnow()
    rows = []
    for u in users:
        last_login = _parse_dt(u["last_login_at"])
        last_seen = _parse_dt(u["last_seen_at"])
        idle_seconds = (now - last_seen).total_seconds() if last_seen else None
        duration_seconds = (last_seen - last_login).total_seconds() if (last_seen and last_login) else None

        if not u["is_online"]:
            status = "已登出"
        elif idle_seconds is not None and idle_seconds > IDLE_THRESHOLD_MINUTES * 60:
            status = "閒置逾時"
        else:
            status = "在線"

        rows.append({
            "username": u["username"],
            "is_admin": u["is_admin"],
            "status": status,
            "last_login_at": u["last_login_at"],
            "last_seen_at": u["last_seen_at"],
            "duration": _format_duration(duration_seconds),
            "idle": _format_duration(idle_seconds),
        })

    return render_template(
        "admin_sessions.html", rows=rows, idle_threshold=IDLE_THRESHOLD_MINUTES, active_tab="sessions",
    )


@app.route("/admin/logs")
@admin_required
def admin_logs():
    db = get_db()
    raw_logs = db.execute(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT 200"
    ).fetchall()
    db.close()

    logs = [
        {
            "created_at": row["created_at"],
            "username": row["username"],
            "action": row["action"],
            "label": ACTION_LABELS.get(row["action"], row["action"]),
            "ip_address": row["ip_address"],
        }
        for row in raw_logs
    ]
    return render_template("admin_logs.html", logs=logs, active_tab="logs")


@app.route("/admin/logs/clear", methods=["POST"])
@admin_required
def admin_clear_logs():
    db = get_db()
    db.execute("DELETE FROM activity_log")
    db.commit()
    db.close()
    flash("系統紀錄已清空")
    return redirect(url_for("admin_logs"))


@app.route("/admin/countries/<int:country_id>", methods=["POST"])
@admin_required
def admin_update_country(country_id):
    name = request.form.get("name", "").strip()
    element = request.form.get("element", "").strip()
    description = request.form.get("description", "").strip()

    if not name or not element:
        flash("國家名稱與屬性不可以是空的")
        return redirect(url_for("admin"))

    bonuses = {}
    for field in STAT_FIELDS:
        raw = request.form.get(field, "0").strip()
        try:
            bonuses[field] = int(raw)
        except ValueError:
            flash(f"{field} 必須是整數")
            return redirect(url_for("admin"))

    db = get_db()

    role_ids = {}
    for role in GOVERNMENT_ROLES:
        raw = request.form.get(role["column"], "").strip()
        if not raw:
            role_ids[role["column"]] = None
            continue
        try:
            char_id = int(raw)
        except ValueError:
            flash(f"{role['label']}的人選不正確")
            db.close()
            return redirect(url_for("admin"))
        owner = db.execute(
            "SELECT id FROM characters WHERE id = ? AND country_id = ?", (char_id, country_id)
        ).fetchone()
        if owner is None:
            flash(f"{role['label']}必須是這個國家的角色")
            db.close()
            return redirect(url_for("admin"))
        role_ids[role["column"]] = char_id

    try:
        db.execute(
            """UPDATE countries SET
                 name = ?, element = ?, description = ?,
                 hp_bonus = ?, mp_bonus = ?, str_bonus = ?,
                 def_bonus = ?, agi_bonus = ?, luk_bonus = ?,
                 king_character_id = ?, advisor_character_id = ?, general_character_id = ?
               WHERE id = ?""",
            (
                name, element, description,
                bonuses["hp_bonus"], bonuses["mp_bonus"], bonuses["str_bonus"],
                bonuses["def_bonus"], bonuses["agi_bonus"], bonuses["luk_bonus"],
                role_ids["king_character_id"], role_ids["advisor_character_id"],
                role_ids["general_character_id"],
                country_id,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("這個國家名稱已經被使用了")
        db.close()
        return redirect(url_for("admin"))
    db.close()

    flash(f"已更新「{name}」")
    return redirect(url_for("admin"))


@app.route("/admin/settings")
@admin_required
def admin_settings():
    db = get_db()
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    hunting_grounds = db.execute("SELECT * FROM hunting_grounds ORDER BY min_level").fetchall()
    db.close()
    return render_template(
        "admin_settings.html",
        settings=settings, hunting_grounds=hunting_grounds, active_tab="settings", level_cap=LEVEL_CAP,
    )


@app.route("/admin/settings/game", methods=["POST"])
@admin_required
def admin_update_game_settings():
    try:
        turn_wait_seconds = int(request.form.get("turn_wait_seconds", ""))
        exp_base = int(request.form.get("exp_base", ""))
        exp_growth_novice_percent = float(request.form.get("exp_growth_novice_percent", ""))
        exp_growth_tier2_percent = float(request.form.get("exp_growth_tier2_percent", ""))
        exp_growth_tier3_percent = float(request.form.get("exp_growth_tier3_percent", ""))
        exp_growth_tier4_percent = float(request.form.get("exp_growth_tier4_percent", ""))
        rebirth_stat_bonus_percent = float(request.form.get("rebirth_stat_bonus_percent", ""))
        sell_back_percent = float(request.form.get("sell_back_percent", ""))
        guardian_encounter_percent = float(request.form.get("guardian_encounter_percent", ""))
        guardian_exp_multiplier = float(request.form.get("guardian_exp_multiplier", ""))
        boss_exp_multiplier = float(request.form.get("boss_exp_multiplier", ""))
        shop_tax_percent = float(request.form.get("shop_tax_percent", ""))
        heal_cost_per_point = float(request.form.get("heal_cost_per_point", ""))
        town_defense_level = int(request.form.get("town_defense_level", ""))
        fortress_defense_level = int(request.form.get("fortress_defense_level", ""))
        stat_reroll_cost = int(request.form.get("stat_reroll_cost", ""))
    except ValueError:
        flash("設定值格式不正確")
        return redirect(url_for("admin_settings"))

    if turn_wait_seconds < 0 or exp_base < 1:
        flash("設定值必須是正數")
        return redirect(url_for("admin_settings"))

    if min(exp_growth_novice_percent, exp_growth_tier2_percent,
           exp_growth_tier3_percent, exp_growth_tier4_percent) < 0:
        flash("各階段成長率不可為負數")
        return redirect(url_for("admin_settings"))

    if rebirth_stat_bonus_percent < 0:
        flash("轉生加成不可為負數")
        return redirect(url_for("admin_settings"))

    if sell_back_percent < 0 or sell_back_percent > 100:
        flash("裝備回收比例必須介於 0 到 100 之間")
        return redirect(url_for("admin_settings"))

    if (
        guardian_encounter_percent < 0 or guardian_encounter_percent > 100
        or guardian_exp_multiplier < 1 or boss_exp_multiplier < 1
    ):
        flash("守衛怪遭遇機率須介於 0 到 100，經驗倍率須大於等於 1")
        return redirect(url_for("admin_settings"))

    if shop_tax_percent < 0 or shop_tax_percent > 100 or heal_cost_per_point < 0:
        flash("商店稅率須介於 0 到 100，回復站費率不可為負數")
        return redirect(url_for("admin_settings"))

    if town_defense_level < 1 or fortress_defense_level < town_defense_level:
        flash("城鎮防衛等級須大於等於 1，且要塞防衛等級須大於等於城鎮防衛等級")
        return redirect(url_for("admin_settings"))

    if stat_reroll_cost < 0:
        flash("屬性重洗費用不可為負數")
        return redirect(url_for("admin_settings"))

    db = get_db()
    db.execute(
        """UPDATE game_settings
           SET turn_wait_seconds = ?, exp_base = ?, exp_growth_novice_percent = ?,
               exp_growth_tier2_percent = ?, exp_growth_tier3_percent = ?, exp_growth_tier4_percent = ?,
               rebirth_stat_bonus_percent = ?, sell_back_percent = ?,
               guardian_encounter_percent = ?, guardian_exp_multiplier = ?,
               boss_exp_multiplier = ?, shop_tax_percent = ?,
               heal_cost_per_point = ?, town_defense_level = ?, fortress_defense_level = ?,
               stat_reroll_cost = ?
           WHERE id = 1""",
        (
            turn_wait_seconds, exp_base, exp_growth_novice_percent,
            exp_growth_tier2_percent, exp_growth_tier3_percent, exp_growth_tier4_percent,
            rebirth_stat_bonus_percent, sell_back_percent,
            guardian_encounter_percent, guardian_exp_multiplier,
            boss_exp_multiplier, shop_tax_percent,
            heal_cost_per_point, town_defense_level, fortress_defense_level,
            stat_reroll_cost,
        ),
    )
    db.commit()
    db.close()

    flash("已更新遊戲設定")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/hunting/<int:ground_id>", methods=["POST"])
@admin_required
def admin_update_hunting_ground(ground_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("打怪場名稱不可以是空的")
        return redirect(url_for("admin_settings"))

    try:
        min_level = int(request.form.get("min_level", ""))
        max_level = int(request.form.get("max_level", ""))
    except ValueError:
        flash("打怪場數值格式不正確")
        return redirect(url_for("admin_settings"))

    if min_level < 1 or max_level < min_level:
        flash("打怪場等級區間不合理")
        return redirect(url_for("admin_settings"))

    db = get_db()
    db.execute(
        """UPDATE hunting_grounds
           SET name = ?, min_level = ?, max_level = ?
           WHERE id = ?""",
        (name, min_level, max_level, ground_id),
    )
    db.commit()
    db.close()

    flash(f"已更新「{name}」")
    return redirect(url_for("admin_settings"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
