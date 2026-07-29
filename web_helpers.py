"""Helpers shared across more than one blueprint (plus the auth/admin/
character access decorators used by all of them).

Import direction is strictly app.py -> blueprints/* -> web_helpers.py, so
this module must never import from app.py or from any blueprint."""
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import redirect, url_for, session, flash

from db import get_db
from game_data.constants import (
    MIN_PASSWORD_LEN, MAX_PASSWORD_LEN, MIN_CHARACTER_NAME_LEN, MAX_CHARACTER_NAME_LEN,
)


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


TAIPEI_TZ = timezone(timedelta(hours=8))
ISO_WEEKDAY_LABELS = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}


def _taipei_now():
    return datetime.now(TAIPEI_TZ)


def _in_war_window(weekday, start_time, end_time, now=None):
    now = now if now is not None else _taipei_now()
    if now.isoweekday() != weekday:
        return False
    start_h, start_m = (int(x) for x in start_time.split(":"))
    end_h, end_m = (int(x) for x in end_time.split(":"))
    window_start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    window_end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return window_start <= now < window_end


def _war_window_label(weekday, start_time, end_time):
    return f"{ISO_WEEKDAY_LABELS.get(weekday, '?')} {start_time}-{end_time}"


WAR_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _valid_war_time(value):
    return bool(WAR_TIME_PATTERN.match(value))


def _war_window_kind_for_tile_type(tile_type):
    return "fortress" if tile_type == "fortress" else "town"


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
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("請先登入")
            return redirect(url_for("auth.login"))
        if not session.get("is_admin"):
            flash("沒有權限")
            return redirect(url_for("auth.index"))
        return view(*args, **kwargs)
    return wrapped


def character_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("請先登入")
            return redirect(url_for("auth.login"))
        db = get_db()
        character = db.execute(
            "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        db.close()
        if character is None:
            return redirect(url_for("character.character_create"))
        return view(*args, **kwargs)
    return wrapped


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
