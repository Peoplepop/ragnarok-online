import os
import random
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, init_db, seed_defaults, log_activity, LEVEL_CAP, ELEMENT_OVERCOMES
from map_layout import axial_to_pixel, hex_corners, axial_distance

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")

MIN_USERNAME_LEN = 3
MIN_PASSWORD_LEN = 6
MIN_CHARACTER_NAME_LEN = 2
MAX_CHARACTER_NAME_LEN = 20
STAT_FIELDS = ["hp_bonus", "mp_bonus", "str_bonus", "def_bonus", "agi_bonus", "luk_bonus"]
IDLE_THRESHOLD_MINUTES = 15
ACTION_LABELS = {
    "register": "註冊",
    "login": "登入",
    "login_failed": "登入失敗",
    "logout": "登出",
    "auto_logout": "閒置自動登出",
    "character_create": "建立角色",
    "hunt": "打怪",
    "move": "移動",
    "shop_buy": "購買裝備",
    "shop_sell": "出售裝備",
    "equip": "裝備",
    "unequip": "卸下裝備",
    "recover": "回復",
    "bank_deposit": "銀行存款",
    "bank_withdraw": "銀行提款",
    "treasury_donate": "捐獻國庫",
    "conquer_win": "攻城成功",
    "conquer_loss": "攻城失敗",
}

SHOP_TYPE_LABELS = {
    "weapon": "武器店",
    "armor": "防具店",
    "accessory": "飾品店",
}
SLOT_LABELS = {
    "weapon": "武器",
    "armor": "防具",
    "accessory": "飾品",
}
EQUIP_SLOT_COLUMNS = {
    "weapon": "equipped_weapon_id",
    "armor": "equipped_armor_id",
    "accessory": "equipped_accessory_id",
}

# Government seats are informational only for now -- no permissions are tied
# to holding one, just a name + a blurb of what the seat is meant to do.
GOVERNMENT_ROLES = [
    {"column": "king_character_id", "label": "國王", "description": "國家最高元首，代表國家對外決策"},
    {"column": "advisor_character_id", "label": "參謀", "description": "協助國政與外交，提供決策建議"},
    {"column": "general_character_id", "label": "大將軍", "description": "統帥軍隊，主導攻城與防衛行動"},
]

# Below this level, 升級軟糖 may still be used to skip grinding; past it,
# levelling only comes from killing monsters.
LEVEL_CANDY_MAX_LEVEL = 500

def tile_display_name(name, tile_type):
    return f"{name}要塞" if tile_type == "fortress" else name


HEX_SIZE = 42
ELEMENT_COLORS = {
    "金": "#f0c419",
    "木": "#4c8c5c",
    "水": "#3b7dc4",
    "火": "#c0453f",
    "土": "#8b5a2b",
}
NEUTRAL_TILE_COLOR = "#5a5a5a"
MOUNTAIN_TILE_COLOR = "#3e3830"

# Every character starts from the same base stats; country bonuses are a
# percentage applied on top (countries.*_bonus stores the percent, e.g. 1 = 1%).
BASE_STATS = {
    "hp": ("hp_bonus", 500),
    "mp": ("mp_bonus", 500),
    "str": ("str_bonus", 30),
    "def": ("def_bonus", 30),
    "agi": ("agi_bonus", 30),
    "luk": ("luk_bonus", 30),
}

# Flat stat growth per level above 1, so higher-level hunting grounds are
# actually harder/beatable -- gear alone was the only source of growth before.
LEVEL_STAT_GROWTH = {"hp": 5, "mp": 5, "str": 1, "def": 1, "agi": 1, "luk": 1}


def compute_final_stats(country, equipped_items=(), level=1):
    equip_bonus = {}
    for item in equipped_items:
        if item:
            equip_bonus[item["stat"]] = equip_bonus.get(item["stat"], 0) + item["stat_bonus"]
    level_bonus = max(0, level - 1)
    return {
        key: round(base * (1 + country[bonus_field] / 100))
        + equip_bonus.get(key, 0)
        + LEVEL_STAT_GROWTH[key] * level_bonus
        for key, (bonus_field, base) in BASE_STATS.items()
    }


def defense_tower_stats(country, tile_type, settings):
    """Builds a monster-shaped dict for a town/fortress's defenders, reusing
    compute_final_stats at a fixed high level (no gear) so territory combat
    goes through the exact same run_battle/_combat_hit path as hunting."""
    level = settings["fortress_defense_level"] if tile_type == "fortress" else settings["town_defense_level"]
    s = compute_final_stats(country, [], level)
    return {
        "name": f"{country['name']}守軍",
        "hp": s["hp"], "atk": s["str"], "def": s["def"], "agi": s["agi"],
        "element": country["element"],
    }


def _current_hp_mp(character, stats):
    """current_hp/current_mp of NULL means "untouched, full" -- battles and
    /game/recover are the only things that ever write a concrete number."""
    hp = character["current_hp"] if character["current_hp"] is not None else stats["hp"]
    mp = character["current_mp"] if character["current_mp"] is not None else stats["mp"]
    return max(0, min(hp, stats["hp"])), max(0, min(mp, stats["mp"]))


def exp_required_for_level(level, settings):
    """EXP needed to advance from `level` to `level + 1`, compounding
    exp_growth_percent per level on top of exp_base."""
    return round(settings["exp_base"] * (1 + settings["exp_growth_percent"] / 100) ** (level - 1))


def apply_exp(level, exp, gained, settings):
    """Add `gained` EXP, cascading through as many level-ups as it covers.
    Returns (new_level, new_exp). Capped at LEVEL_CAP; extra EXP past the cap is discarded."""
    level, exp = level, exp + gained
    while level < LEVEL_CAP:
        needed = exp_required_for_level(level, settings)
        if exp < needed:
            break
        exp -= needed
        level += 1
    if level >= LEVEL_CAP:
        level, exp = LEVEL_CAP, 0
    return level, exp


# --- Combat formula tuning ------------------------------------------------
# Every percentage-based stat (減傷/爆擊率/命中率/閃避率) uses an x/(x+K) soft-cap
# curve: it keeps climbing but only asymptotically approaches its ceiling, so no
# stat combination can ever push it to a literal 100% (guaranteed unhittable /
# undodgeable / uncritable outcome).
STR_DAMAGE_RANGE = (0.85, 1.15)      # 1 STR point -> 0.85~1.15 raw damage, rolled per hit
DEF_REDUCTION_K = 120                # DEF/(DEF+K) -> reduction fraction, asymptotic to 1
DEF_REDUCTION_JITTER = (0.9, 1.1)    # per-hit jitter on the reduction fraction itself
DEF_REDUCTION_HARD_CAP = 0.90        # extra safety net alongside the asymptote
SPEED_PER_AGI = 5                    # 1 AGI -> 5 attack-speed points
EXTRA_ATTACK_SPEED_STEP = 50         # every 50 speed points of lead = +1 attack per round
CRIT_CHANCE_K = 150                  # AGI/(AGI+K) -> crit chance, asymptotic
CRIT_CHANCE_HARD_CAP = 70            # percent
CRIT_DAMAGE_RANGE = (1.3, 1.7)       # crit multiplier rolled per crit
HIT_CHANCE_BASE = 90                 # percent, at LUK=0 (so monsters w/o LUK still swing)
HIT_CHANCE_MAX_BONUS = 10            # + up to this many percent, asymptotic w/ LUK
HIT_CHANCE_K = 100
DODGE_CHANCE_BASE = 5                # percent, baseline evasion even at LUK=0
DODGE_CHANCE_MAX_BONUS = 55          # + up to this many percent, asymptotic w/ LUK
DODGE_CHANCE_K = 150
GOLD_LUK_BONUS_PER_POINT = 0.05      # percent of currency reward per LUK point
GOLD_LUK_BONUS_CAP = 15              # percent
ELEMENT_OVERCOME_BONUS = 1.25        # attacker's element 剋 defender's -> +25% damage
ELEMENT_OVERCOME_PENALTY = 0.8       # attacker's element 被剋 by defender's -> -20% damage


def elemental_multiplier(attacker_element, defender_element):
    if not attacker_element or not defender_element or attacker_element == defender_element:
        return 1.0
    if ELEMENT_OVERCOMES.get(attacker_element) == defender_element:
        return ELEMENT_OVERCOME_BONUS
    if ELEMENT_OVERCOMES.get(defender_element) == attacker_element:
        return ELEMENT_OVERCOME_PENALTY
    return 1.0


def _hit_chance_pct(attacker_luk):
    return HIT_CHANCE_BASE + HIT_CHANCE_MAX_BONUS * attacker_luk / (attacker_luk + HIT_CHANCE_K)


def _dodge_chance_pct(defender_luk):
    return DODGE_CHANCE_BASE + DODGE_CHANCE_MAX_BONUS * defender_luk / (defender_luk + DODGE_CHANCE_K)


def _crit_chance_pct(attacker_agi):
    return min(CRIT_CHANCE_HARD_CAP, 100 * attacker_agi / (attacker_agi + CRIT_CHANCE_K))


def _def_reduction_fraction(defender_def):
    return defender_def / (defender_def + DEF_REDUCTION_K)


def gold_luk_bonus_pct(luk):
    return min(GOLD_LUK_BONUS_CAP, luk * GOLD_LUK_BONUS_PER_POINT)


def derived_combat_stats(stats):
    """Same formulas as combat, minus the per-hit dice rolls -- for display
    on the character sheet (shows the range a stat translates into)."""
    reduction = _def_reduction_fraction(stats["def"])
    return {
        "damage_min": round(stats["str"] * STR_DAMAGE_RANGE[0]),
        "damage_max": round(stats["str"] * STR_DAMAGE_RANGE[1]),
        "reduction_min": round(min(DEF_REDUCTION_HARD_CAP, reduction * DEF_REDUCTION_JITTER[0]) * 100, 1),
        "reduction_max": round(min(DEF_REDUCTION_HARD_CAP, reduction * DEF_REDUCTION_JITTER[1]) * 100, 1),
        "speed": stats["agi"] * SPEED_PER_AGI,
        "crit_chance": round(_crit_chance_pct(stats["agi"]), 1),
        "crit_damage_min": CRIT_DAMAGE_RANGE[0],
        "crit_damage_max": CRIT_DAMAGE_RANGE[1],
        "hit_chance": round(_hit_chance_pct(stats["luk"]), 1),
        "dodge_chance": round(_dodge_chance_pct(stats["luk"]), 1),
        "gold_bonus": round(gold_luk_bonus_pct(stats["luk"]), 1),
    }


def _combat_hit(
    attacker_name, attacker_str, attacker_agi, attacker_luk, attacker_element,
    defender_name, defender_def, defender_luk, defender_element,
):
    if random.random() * 100 >= _hit_chance_pct(attacker_luk):
        return 0, f"{attacker_name} 的攻擊沒有命中"

    if random.random() * 100 < _dodge_chance_pct(defender_luk):
        return 0, f"{defender_name} 閃避了 {attacker_name} 的攻擊！"

    is_crit = random.random() * 100 < _crit_chance_pct(attacker_agi)
    crit_mult = random.uniform(*CRIT_DAMAGE_RANGE) if is_crit else 1.0
    elem_mult = elemental_multiplier(attacker_element, defender_element)
    raw_damage = attacker_str * random.uniform(*STR_DAMAGE_RANGE) * crit_mult * elem_mult

    reduction = min(DEF_REDUCTION_HARD_CAP, _def_reduction_fraction(defender_def) * random.uniform(*DEF_REDUCTION_JITTER))
    damage = max(1, round(raw_damage * (1 - reduction)))
    suffix = "（會心一擊！）" if is_crit else ""
    if elem_mult > 1:
        suffix += "（屬性相剋！）"
    elif elem_mult < 1:
        suffix += "（屬性被剋）"
    return damage, f"{attacker_name} 攻擊 {defender_name}，造成 {damage} 點傷害{suffix}"


BATTLE_ROUND_CAP = 60


def run_battle(player_name, player_stats, player_element, player_hp, monster):
    """Resolves an entire fight in one shot. Turn order and extra attacks are
    driven purely by attack speed (AGI*SPEED_PER_AGI): whoever is faster always
    goes first each round, and gets +1 extra attack per EXTRA_ATTACK_SPEED_STEP
    of speed lead. Monsters have no LUK column so they use 0 (baseline hit/dodge
    only), but they do roll crits off their own AGI and carry their own
    element for the Wu Xing damage multiplier."""
    log = []
    p_hp, m_hp = player_hp, monster["hp"]

    player_speed = player_stats["agi"] * SPEED_PER_AGI
    monster_speed = monster["agi"] * SPEED_PER_AGI
    if player_speed >= monster_speed:
        faster, slower = "player", "monster"
        speed_lead = player_speed - monster_speed
    else:
        faster, slower = "monster", "player"
        speed_lead = monster_speed - player_speed
    extra_attacks = speed_lead // EXTRA_ATTACK_SPEED_STEP

    def attack_once(attacker):
        nonlocal p_hp, m_hp
        if attacker == "player":
            dmg, line = _combat_hit(
                player_name, player_stats["str"], player_stats["agi"], player_stats["luk"], player_element,
                monster["name"], monster["def"], 0, monster["element"],
            )
            m_hp = max(0, m_hp - dmg)
            log.append(f"{line}（{monster['name']} 剩餘 HP {m_hp}）")
        else:
            dmg, line = _combat_hit(
                monster["name"], monster["atk"], monster["agi"], 0, monster["element"],
                player_name, player_stats["def"], player_stats["luk"], player_element,
            )
            p_hp = max(0, p_hp - dmg)
            log.append(f"{line}（{player_name} 剩餘 HP {p_hp}）")

    for _round in range(BATTLE_ROUND_CAP):
        if p_hp <= 0 or m_hp <= 0:
            break
        for _ in range(1 + extra_attacks):
            attack_once(faster)
            if p_hp <= 0 or m_hp <= 0:
                break
        if p_hp <= 0 or m_hp <= 0:
            break
        attack_once(slower)

    return {"log": log, "won": m_hp <= 0 and p_hp > 0, "player_hp": p_hp, "monster_hp": m_hp}


init_db()
seed_defaults()


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
    if len(password) < MIN_PASSWORD_LEN:
        flash(f"密碼至少需要 {MIN_PASSWORD_LEN} 個字元")
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

    if user is None or not check_password_hash(user["password_hash"], password):
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


@app.route("/game")
@character_required
def game():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.current_tile_id,
                  characters.currency, characters.bank_balance, characters.level, characters.exp,
                  characters.next_action_at, characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.name AS character_name,
                  characters.current_hp, characters.current_mp, countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    tiles = [
        dict(row) for row in db.execute(
            """SELECT map_tiles.id AS tile_id, map_tiles.q, map_tiles.r, map_tiles.tile_type,
                      map_tiles.name, map_tiles.country_id,
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
    equipped_ids = [
        character["equipped_weapon_id"], character["equipped_armor_id"], character["equipped_accessory_id"],
    ]
    equipped_items = [
        db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        for item_id in equipped_ids if item_id
    ]
    db.close()

    stats = compute_final_stats(character, equipped_items, character["level"])
    current_hp, current_mp = _current_hp_mp(character, stats)

    exp_needed = (
        exp_required_for_level(character["level"], settings)
        if character["level"] < LEVEL_CAP else None
    )

    cooldown_seconds = _cooldown_remaining_seconds(character["next_action_at"])

    missing = (stats["hp"] - current_hp) + (stats["mp"] - current_mp)
    recover_cost = round(missing * settings["heal_cost_per_point"])

    can_attack_tile = (
        current_tile["tile_type"] in ("fortress", "town")
        and current_tile["country_id"] is not None
        and current_tile["country_id"] != character["id"]
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
            "name": t["display_name"],
            "country_name": t["country_name"],
            "is_player_here": t["tile_id"] == character["current_tile_id"],
        })

    padding = HEX_SIZE
    min_x, max_x = min(xs) - padding, max(xs) + padding
    min_y, max_y = min(ys) - padding, max(ys) + padding

    return render_template(
        "game.html",
        character=character,
        stats=stats,
        current_hp=current_hp,
        current_mp=current_mp,
        level_cap=LEVEL_CAP,
        exp_needed=exp_needed,
        current_tile=current_tile,
        move_targets=move_targets,
        hunting_grounds=hunting_grounds,
        cooldown_seconds=cooldown_seconds,
        recover_cost=recover_cost,
        can_attack_tile=can_attack_tile,
        defense_level=defense_level,
        own_treasury=character["treasury"],
        hexes=hexes,
        view_box=f"{min_x:.1f} {min_y:.1f} {max_x - min_x:.1f} {max_y - min_y:.1f}",
    )


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
        "SELECT id, q, r, tile_type, name FROM map_tiles WHERE id = ?",
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

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        "UPDATE characters SET current_tile_id = ?, next_action_at = ? WHERE id = ?",
        (target_tile["id"], _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
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

    ground = db.execute(
        "SELECT * FROM hunting_grounds WHERE id = ?", (request.form.get("ground_id", ""),)
    ).fetchone()
    if ground is None:
        db.close()
        flash("請選擇一個有效的打怪場")
        return redirect(url_for("game"))

    equipped_ids = [
        character["equipped_weapon_id"], character["equipped_armor_id"], character["equipped_accessory_id"],
    ]
    equipped_items = [
        db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        for item_id in equipped_ids if item_id
    ]
    stats = compute_final_stats(character, equipped_items, character["level"])
    current_hp, _current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    monsters = db.execute(
        "SELECT * FROM monsters WHERE hunting_ground_id = ?", (ground["id"],)
    ).fetchall()
    regulars = [m for m in monsters if not m["is_boss"]]
    bosses = [m for m in monsters if m["is_boss"]]
    is_boss_fight = bool(bosses) and random.random() * 100 < settings["boss_encounter_percent"]
    pool = bosses if is_boss_fight else (regulars or bosses)
    if not pool:
        db.close()
        flash("這個打怪場目前還沒有設定怪物")
        return redirect(url_for("game"))
    monster = random.choice(pool)

    result = run_battle(character["character_name"], stats, character["element"], current_hp, monster)

    exp_gain = 0
    currency_gain = 0
    currency_lost = 0
    new_level, new_exp = character["level"], character["exp"]
    if result["won"]:
        exp_gain = ground["monster_exp"]
        if monster["is_boss"]:
            exp_gain = round(exp_gain * settings["boss_exp_multiplier"])
        currency_gain = monster["currency_reward"]
        currency_gain = round(currency_gain * (1 + gold_luk_bonus_pct(stats["luk"]) / 100))
        new_level, new_exp = apply_exp(character["level"], character["exp"], exp_gain, settings)
        new_currency = character["currency"] + currency_gain
    else:
        currency_lost = character["currency"]
        new_currency = 0

    db.execute(
        """UPDATE characters
           SET level = ?, exp = ?, currency = ?, current_hp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?
           WHERE id = ?""",
        (
            new_level, new_exp, new_currency, result["player_hp"],
            _next_action_at(settings["turn_wait_seconds"]), 1 if result["won"] else 0,
            character["character_id"],
        ),
    )
    outcome_detail = (
        f"擊敗{monster['name']}，+{exp_gain} EXP +{currency_gain} 諸神幣"
        if result["won"] else f"敗給{monster['name']}，身上 {currency_lost} 諸神幣化為烏有"
    )
    log_activity(
        db, session["user_id"], session["username"], "hunt",
        detail=f"{ground['name']} {outcome_detail}", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    return render_template(
        "battle.html",
        ground=ground,
        monster=monster,
        log=result["log"],
        won=result["won"],
        leveled_up=new_level > character["level"],
        new_level=new_level,
        exp_gain=exp_gain,
        currency_gain=currency_gain,
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
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

    if (
        character["tile_type"] not in ("fortress", "town")
        or character["tile_country_id"] is None
        or character["tile_country_id"] == character["id"]
    ):
        db.close()
        flash("這裡沒有可以攻打的敵方據點")
        return redirect(url_for("game"))

    equipped_ids = [
        character["equipped_weapon_id"], character["equipped_armor_id"], character["equipped_accessory_id"],
    ]
    equipped_items = [
        db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        for item_id in equipped_ids if item_id
    ]
    stats = compute_final_stats(character, equipped_items, character["level"])
    current_hp, _current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game"))

    settings = db.execute(
        "SELECT turn_wait_seconds, town_defense_level, fortress_defense_level FROM game_settings WHERE id = 1"
    ).fetchone()
    defending_country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (character["tile_country_id"],)
    ).fetchone()
    tower = defense_tower_stats(defending_country, character["tile_type"], settings)
    tile_name = tile_display_name(character["tile_name"], character["tile_type"])

    result = run_battle(character["character_name"], stats, character["element"], current_hp, tower)

    currency_lost = 0
    if result["won"]:
        new_currency = character["currency"]
        db.execute(
            "UPDATE map_tiles SET country_id = ? WHERE id = ?",
            (character["id"], character["current_tile_id"]),
        )
        outcome_detail = f"攻下{tile_name}（原屬{defending_country['name']}）"
    else:
        currency_lost = character["currency"]
        new_currency = 0
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
            (currency_lost, defending_country["id"]),
        )
        outcome_detail = (
            f"攻打{tile_name}失敗，身上{currency_lost}諸神幣被{defending_country['name']}沒收"
        )

    db.execute(
        """UPDATE characters
           SET currency = ?, current_hp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?
           WHERE id = ?""",
        (
            new_currency, result["player_hp"], _next_action_at(settings["turn_wait_seconds"]),
            1 if result["won"] else 0, character["character_id"],
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
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
    )


@app.route("/game/recover", methods=["POST"])
@character_required
def game_recover():
    db = get_db()
    character = db.execute(
        """SELECT characters.id, characters.level, characters.next_action_at, characters.currency,
                  characters.current_hp, characters.current_mp, map_tiles.tile_type,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, countries.*
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

    equipped_ids = [
        character["equipped_weapon_id"], character["equipped_armor_id"], character["equipped_accessory_id"],
    ]
    equipped_items = [
        db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        for item_id in equipped_ids if item_id
    ]
    stats = compute_final_stats(character, equipped_items, character["level"])
    current_hp, current_mp = _current_hp_mp(character, stats)

    settings = db.execute(
        "SELECT turn_wait_seconds, heal_cost_per_point FROM game_settings WHERE id = 1"
    ).fetchone()
    missing = (stats["hp"] - current_hp) + (stats["mp"] - current_mp)
    cost = round(missing * settings["heal_cost_per_point"])
    if cost > character["currency"]:
        db.close()
        flash(f"諸神幣不足，完全回復需要 {cost} 諸神幣")
        return redirect(url_for("game"))

    db.execute(
        """UPDATE characters SET current_hp = ?, current_mp = ?, currency = currency - ?,
               next_action_at = ? WHERE id = ?""",
        (stats["hp"], stats["mp"], cost, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
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
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id
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

    all_items = db.execute("SELECT * FROM items ORDER BY shop_type, price").fetchall()
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

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        "UPDATE characters SET currency = currency - ?, next_action_at = ? WHERE id = ?",
        (amount, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    db.execute(
        "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (amount, character["country_id"])
    )
    log_activity(
        db, session["user_id"], session["username"], "treasury_donate",
        detail=f"捐獻 {amount} 諸神幣給國庫", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已捐獻 {amount} 諸神幣給國庫")
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
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.battles_count,
                  characters.wins_count, countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_ids = [
        character["equipped_weapon_id"], character["equipped_armor_id"], character["equipped_accessory_id"],
    ]
    equipped_items = [
        db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        for item_id in equipped_ids if item_id
    ]

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

    db.close()

    stats = compute_final_stats(character, equipped_items, character["level"])
    current_hp, current_mp = _current_hp_mp(character, stats)
    exp_needed = (
        exp_required_for_level(character["level"], settings)
        if character["level"] < LEVEL_CAP else None
    )
    win_rate = (
        round(character["wins_count"] / character["battles_count"] * 100, 1)
        if character["battles_count"] else None
    )
    overcomes = ELEMENT_OVERCOMES.get(character["element"])
    overcome_by = next(
        (k for k, v in ELEMENT_OVERCOMES.items() if v == character["element"]), None
    )

    return render_template(
        "character.html",
        character=character,
        stats=stats,
        combat_stats=derived_combat_stats(stats),
        win_rate=win_rate,
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
        inventory_items=inventory_items,
        shop_type_labels=SHOP_TYPE_LABELS,
        cooldown_seconds=_cooldown_remaining_seconds(character["next_action_at"]),
    )


@app.route("/admin")
@admin_required
def admin():
    db = get_db()
    countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
    characters = db.execute("SELECT id, name, country_id FROM characters ORDER BY name").fetchall()
    db.close()

    characters_by_country = {}
    for c in characters:
        characters_by_country.setdefault(c["country_id"], []).append(c)

    return render_template(
        "admin.html", countries=countries, characters_by_country=characters_by_country,
        roles=GOVERNMENT_ROLES, active_tab="countries",
    )


@app.route("/admin/sessions")
@admin_required
def admin_sessions():
    db = get_db()
    users = db.execute(
        "SELECT username, is_admin, is_online, last_login_at, last_seen_at "
        "FROM users ORDER BY last_seen_at IS NULL, last_seen_at DESC"
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
        settings=settings, hunting_grounds=hunting_grounds, active_tab="settings",
    )


@app.route("/admin/settings/game", methods=["POST"])
@admin_required
def admin_update_game_settings():
    try:
        turn_wait_seconds = int(request.form.get("turn_wait_seconds", ""))
        exp_base = int(request.form.get("exp_base", ""))
        exp_growth_percent = float(request.form.get("exp_growth_percent", ""))
        sell_back_percent = float(request.form.get("sell_back_percent", ""))
        boss_encounter_percent = float(request.form.get("boss_encounter_percent", ""))
        boss_exp_multiplier = float(request.form.get("boss_exp_multiplier", ""))
        shop_tax_percent = float(request.form.get("shop_tax_percent", ""))
        heal_cost_per_point = float(request.form.get("heal_cost_per_point", ""))
        town_defense_level = int(request.form.get("town_defense_level", ""))
        fortress_defense_level = int(request.form.get("fortress_defense_level", ""))
    except ValueError:
        flash("設定值格式不正確")
        return redirect(url_for("admin_settings"))

    if turn_wait_seconds < 0 or exp_base < 1 or exp_growth_percent < 0:
        flash("設定值必須是正數")
        return redirect(url_for("admin_settings"))

    if sell_back_percent < 0 or sell_back_percent > 100:
        flash("裝備回收比例必須介於 0 到 100 之間")
        return redirect(url_for("admin_settings"))

    if boss_encounter_percent < 0 or boss_encounter_percent > 100 or boss_exp_multiplier < 1:
        flash("首領遭遇機率須介於 0 到 100，經驗倍率須大於等於 1")
        return redirect(url_for("admin_settings"))

    if shop_tax_percent < 0 or shop_tax_percent > 100 or heal_cost_per_point < 0:
        flash("商店稅率須介於 0 到 100，回復站費率不可為負數")
        return redirect(url_for("admin_settings"))

    if town_defense_level < 1 or fortress_defense_level < town_defense_level:
        flash("城鎮防衛等級須大於等於 1，且要塞防衛等級須大於等於城鎮防衛等級")
        return redirect(url_for("admin_settings"))

    db = get_db()
    db.execute(
        """UPDATE game_settings
           SET turn_wait_seconds = ?, exp_base = ?, exp_growth_percent = ?, sell_back_percent = ?,
               boss_encounter_percent = ?, boss_exp_multiplier = ?, shop_tax_percent = ?,
               heal_cost_per_point = ?, town_defense_level = ?, fortress_defense_level = ?
           WHERE id = 1""",
        (
            turn_wait_seconds, exp_base, exp_growth_percent, sell_back_percent,
            boss_encounter_percent, boss_exp_multiplier, shop_tax_percent,
            heal_cost_per_point, town_defense_level, fortress_defense_level,
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
        monster_exp = int(request.form.get("monster_exp", ""))
    except ValueError:
        flash("打怪場數值格式不正確")
        return redirect(url_for("admin_settings"))

    if min_level < 1 or max_level < min_level or monster_exp < 0:
        flash("打怪場等級區間或經驗值不合理")
        return redirect(url_for("admin_settings"))

    db = get_db()
    db.execute(
        """UPDATE hunting_grounds
           SET name = ?, min_level = ?, max_level = ?, monster_exp = ?
           WHERE id = ?""",
        (name, min_level, max_level, monster_exp, ground_id),
    )
    db.commit()
    db.close()

    flash(f"已更新「{name}」")
    return redirect(url_for("admin_settings"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
