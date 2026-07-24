import os
import random
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, init_db, seed_defaults, log_activity, LEVEL_CAP
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

# Below this level, 升級軟糖 may still be used to skip grinding; past it,
# levelling only comes from killing monsters.
LEVEL_CANDY_MAX_LEVEL = 500

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


def _combat_hit(attacker_name, attacker_atk, attacker_luk, defender_name, defender_def, defender_luk):
    dodge_chance = min(25, defender_luk * 0.3)
    if random.random() * 100 < dodge_chance:
        return 0, f"{defender_name} 閃避了 {attacker_name} 的攻擊！"
    raw_damage = max(1, round(attacker_atk - defender_def / 2))
    is_crit = random.random() * 100 < min(35, attacker_luk * 0.5)
    damage = round(raw_damage * 1.5) if is_crit else raw_damage
    suffix = "（會心一擊！）" if is_crit else ""
    return damage, f"{attacker_name} 攻擊 {defender_name}，造成 {damage} 點傷害{suffix}"


BATTLE_ROUND_CAP = 60


def run_battle(player_name, player_stats, player_hp, monster):
    """Resolves an entire fight in one shot (no monster LUK -- monsters
    never crit or dodge, only the player's LUK matters for those rolls)."""
    log = []
    p_hp, m_hp = player_hp, monster["hp"]
    order = ("player", "monster") if player_stats["agi"] >= monster["agi"] else ("monster", "player")

    for _round in range(BATTLE_ROUND_CAP):
        if p_hp <= 0 or m_hp <= 0:
            break
        for attacker in order:
            if attacker == "player":
                dmg, line = _combat_hit(
                    player_name, player_stats["str"], player_stats["luk"],
                    monster["name"], monster["def"], 0,
                )
                m_hp = max(0, m_hp - dmg)
                log.append(f"{line}（{monster['name']} 剩餘 HP {m_hp}）")
                if m_hp <= 0:
                    break
            else:
                dmg, line = _combat_hit(
                    monster["name"], monster["atk"], 0,
                    player_name, player_stats["def"], player_stats["luk"],
                )
                p_hp = max(0, p_hp - dmg)
                log.append(f"{line}（{player_name} 剩餘 HP {p_hp}）")
                if p_hp <= 0:
                    break

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

    try:
        db.execute(
            "INSERT INTO characters (user_id, country_id, current_tile_id, name) VALUES (?, ?, ?, ?)",
            (session["user_id"], country["id"], fortress["id"] if fortress else None, character_name),
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
                  characters.currency, characters.level, characters.exp, characters.next_action_at,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.name AS character_name,
                  characters.current_hp, characters.current_mp, countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    tiles = db.execute(
        """SELECT map_tiles.id AS tile_id, map_tiles.q, map_tiles.r, map_tiles.tile_type,
                  map_tiles.name, map_tiles.country_id,
                  countries.element, countries.name AS country_name
           FROM map_tiles LEFT JOIN countries ON countries.id = map_tiles.country_id"""
    ).fetchall()
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
            "is_own_country": t["country_id"] == character["id"] if t["country_id"] else False,
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

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        "UPDATE characters SET current_tile_id = ?, next_action_at = ? WHERE id = ?",
        (target_tile["id"], _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "move",
        detail=target_tile["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"移動到了「{target_tile['name']}」")
    return redirect(url_for("game"))


@app.route("/game/hunt", methods=["POST"])
@character_required
def game_hunt():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp, characters.next_action_at,
                  characters.current_hp, characters.current_mp, characters.name AS character_name,
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

    result = run_battle(character["character_name"], stats, current_hp, monster)

    exp_gain = 0
    currency_gain = 0
    new_level, new_exp = character["level"], character["exp"]
    if result["won"]:
        exp_gain = ground["monster_exp"]
        if monster["is_boss"]:
            exp_gain = round(exp_gain * settings["boss_exp_multiplier"])
        currency_gain = monster["currency_reward"]
        new_level, new_exp = apply_exp(character["level"], character["exp"], exp_gain, settings)

    db.execute(
        """UPDATE characters
           SET level = ?, exp = ?, currency = currency + ?, current_hp = ?, next_action_at = ?
           WHERE id = ?""",
        (
            new_level, new_exp, currency_gain, result["player_hp"],
            _next_action_at(settings["turn_wait_seconds"]), character["character_id"],
        ),
    )
    outcome_detail = (
        f"擊敗{monster['name']}，+{exp_gain} EXP +{currency_gain} 諸神幣"
        if result["won"] else f"敗給{monster['name']}"
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
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
    )


@app.route("/game/recover", methods=["POST"])
@character_required
def game_recover():
    db = get_db()
    character = db.execute(
        """SELECT characters.id, characters.level, characters.next_action_at, map_tiles.tile_type,
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

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        "UPDATE characters SET current_hp = ?, current_mp = ?, next_action_at = ? WHERE id = ?",
        (stats["hp"], stats["mp"], _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "recover",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash("HP／MP 已完全回復")
    return redirect(url_for("game"))


def _character_for_shop(db):
    return db.execute(
        """SELECT characters.id, characters.currency, characters.next_action_at, map_tiles.tile_type,
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

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    for item in items:
        _add_to_inventory(db, character["id"], item["id"], 1)
    db.execute(
        "UPDATE characters SET currency = currency - ?, next_action_at = ? WHERE id = ?",
        (total_price, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
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
        "SELECT turn_wait_seconds, sell_back_percent FROM game_settings WHERE id = 1"
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
    log_activity(
        db, session["user_id"], session["username"], "shop_sell",
        detail=f"{'、'.join(sold_names)} (+{total_refund} 諸神幣)", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已出售「{'、'.join(sold_names)}」，獲得 {total_refund} 諸神幣")
    return redirect(url_for("game_shop"))


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


@app.route("/character")
@character_required
def character_page():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp,
                  characters.next_action_at, characters.name AS character_name,
                  characters.current_hp, characters.current_mp,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, countries.*
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

    return render_template(
        "character.html",
        character=character,
        stats=stats,
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
    db.close()
    return render_template("admin.html", countries=countries, active_tab="countries")


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
    try:
        db.execute(
            """UPDATE countries SET
                 name = ?, element = ?, description = ?,
                 hp_bonus = ?, mp_bonus = ?, str_bonus = ?,
                 def_bonus = ?, agi_bonus = ?, luk_bonus = ?
               WHERE id = ?""",
            (
                name, element, description,
                bonuses["hp_bonus"], bonuses["mp_bonus"], bonuses["str_bonus"],
                bonuses["def_bonus"], bonuses["agi_bonus"], bonuses["luk_bonus"],
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

    db = get_db()
    db.execute(
        """UPDATE game_settings
           SET turn_wait_seconds = ?, exp_base = ?, exp_growth_percent = ?, sell_back_percent = ?,
               boss_encounter_percent = ?, boss_exp_multiplier = ?
           WHERE id = 1""",
        (
            turn_wait_seconds, exp_base, exp_growth_percent, sell_back_percent,
            boss_encounter_percent, boss_exp_multiplier,
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
