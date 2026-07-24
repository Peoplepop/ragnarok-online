import os
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
}

SHOP_TYPE_LABELS = {
    "weapon": "武器店",
    "armor": "防具店",
    "accessory": "飾品店",
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


def compute_final_stats(country, equipped_items=()):
    equip_bonus = {}
    for item in equipped_items:
        if item:
            equip_bonus[item["stat"]] = equip_bonus.get(item["stat"], 0) + item["stat_bonus"]
    return {
        key: round(base * (1 + country[bonus_field] / 100)) + equip_bonus.get(key, 0)
        for key, (bonus_field, base) in BASE_STATS.items()
    }


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


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

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
    db.close()

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["is_admin"] = bool(user["is_admin"])
    flash(f"歡迎回來，{user['username']}")
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

    if request.method == "GET":
        countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
        db.close()
        return render_template("character_create.html", countries=countries)

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

    db.execute(
        "INSERT INTO characters (user_id, country_id, current_tile_id) VALUES (?, ?, ?)",
        (session["user_id"], country["id"], fortress["id"] if fortress else None),
    )
    log_activity(
        db, session["user_id"], session["username"], "character_create",
        detail=country["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"歡迎來到{country['name']}！")
    return redirect(url_for("game"))


@app.route("/game")
@character_required
def game():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.current_tile_id,
                  characters.currency, characters.level, characters.exp, characters.next_action_at,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, countries.*
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
    shop_items = None
    if current_tile["tile_type"] == "fortress":
        all_items = db.execute("SELECT * FROM items ORDER BY shop_type, price").fetchall()
        shop_items = {shop_type: [] for shop_type in SHOP_TYPE_LABELS}
        for item in all_items:
            shop_items[item["shop_type"]].append(item)
    db.close()

    stats = compute_final_stats(character, equipped_items)

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
        level_cap=LEVEL_CAP,
        exp_needed=exp_needed,
        current_tile=current_tile,
        move_targets=move_targets,
        hunting_grounds=hunting_grounds,
        cooldown_seconds=cooldown_seconds,
        shop_items=shop_items,
        shop_type_labels=SHOP_TYPE_LABELS,
        equipped_ids=set(equipped_ids),
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
        """SELECT characters.id, characters.level, characters.exp, characters.next_action_at,
                  map_tiles.tile_type
           FROM characters JOIN map_tiles ON map_tiles.id = characters.current_tile_id
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

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    new_level, new_exp = apply_exp(character["level"], character["exp"], ground["monster_exp"], settings)

    db.execute(
        "UPDATE characters SET level = ?, exp = ?, next_action_at = ? WHERE id = ?",
        (new_level, new_exp, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "hunt",
        detail=f"{ground['name']} +{ground['monster_exp']} EXP", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    if new_level > character["level"]:
        flash(f"在{ground['name']}擊敗了怪物，獲得 {ground['monster_exp']} 經驗值，升到 Lv.{new_level}！")
    else:
        flash(f"在{ground['name']}擊敗了怪物，獲得 {ground['monster_exp']} 經驗值")
    return redirect(url_for("game"))


@app.route("/game/shop/buy", methods=["POST"])
@character_required
def game_shop_buy():
    db = get_db()
    character = db.execute(
        """SELECT characters.id, characters.currency, map_tiles.tile_type
           FROM characters JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內的商店購買裝備")
        return redirect(url_for("game"))

    item = db.execute(
        "SELECT * FROM items WHERE id = ?", (request.form.get("item_id", ""),)
    ).fetchone()
    if item is None:
        db.close()
        flash("請選擇一個有效的商品")
        return redirect(url_for("game"))

    if character["currency"] < item["price"]:
        db.close()
        flash(f"諸神幣不足，「{item['name']}」需要 {item['price']} 諸神幣")
        return redirect(url_for("game"))

    slot_column = EQUIP_SLOT_COLUMNS[item["shop_type"]]
    db.execute(
        f"UPDATE characters SET currency = currency - ?, {slot_column} = ? WHERE id = ?",
        (item["price"], item["id"], character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "shop_buy",
        detail=f"{item['name']} ({item['price']} 諸神幣)", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已購買並裝備「{item['name']}」")
    return redirect(url_for("game"))


@app.route("/character")
@character_required
def character_page():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp,
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
    db.close()

    stats = compute_final_stats(character, equipped_items)
    exp_needed = (
        exp_required_for_level(character["level"], settings)
        if character["level"] < LEVEL_CAP else None
    )

    return render_template(
        "character.html",
        character=character,
        stats=stats,
        level_cap=LEVEL_CAP,
        exp_needed=exp_needed,
        equipped_items=equipped_items,
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
    except ValueError:
        flash("設定值格式不正確")
        return redirect(url_for("admin_settings"))

    if turn_wait_seconds < 0 or exp_base < 1 or exp_growth_percent < 0:
        flash("設定值必須是正數")
        return redirect(url_for("admin_settings"))

    db = get_db()
    db.execute(
        """UPDATE game_settings
           SET turn_wait_seconds = ?, exp_base = ?, exp_growth_percent = ?
           WHERE id = 1""",
        (turn_wait_seconds, exp_base, exp_growth_percent),
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
