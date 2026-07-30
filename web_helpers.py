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
    MIN_USERNAME_LEN, MIN_PASSWORD_LEN, MAX_PASSWORD_LEN, MIN_CHARACTER_NAME_LEN, MAX_CHARACTER_NAME_LEN,
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


def _in_any_war_window(settings, now=None):
    """True if either the town/wilderness or the fortress war window is
    currently active (Taipei time). Used by the king-usurpation/office-
    challenge routes, which gate on being OUTSIDE war time entirely --
    the opposite polarity from game_conquer, which requires being INSIDE
    the single window matching whatever tile is being attacked. settings
    must carry the full set of war_town_*/war_fortress_* columns (i.e. a
    `SELECT *` from game_settings, not a narrowed column list)."""
    return (
        _in_war_window(
            settings["war_town_weekday"], settings["war_town_start_time"], settings["war_town_end_time"], now,
        )
        or _in_war_window(
            settings["war_fortress_weekday"], settings["war_fortress_start_time"], settings["war_fortress_end_time"],
            now,
        )
    )


WAR_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _valid_war_time(value):
    return bool(WAR_TIME_PATTERN.match(value))


def _war_window_kind_for_tile_type(tile_type):
    return "fortress" if tile_type == "fortress" else "town"


def _current_war_window_end(settings, now=None):
    """If a war window (town or fortress) is currently active, return its
    window_end as a Taipei-aware datetime -- town checked first, then
    fortress -- built the exact same way _in_war_window already constructs
    its own window_end internally. Returns None if neither window is active.

    Used by country_cast_morale_buff to identify "this specific war window
    instance": the caller converts the returned datetime to a UTC-naive
    ACTION_DT_FORMAT string and compares it against
    countries.morale_buff_expires_at to detect a buff already cast for the
    same window occurrence."""
    now = now if now is not None else _taipei_now()
    if _in_war_window(
        settings["war_town_weekday"], settings["war_town_start_time"], settings["war_town_end_time"], now,
    ):
        end_h, end_m = (int(x) for x in settings["war_town_end_time"].split(":"))
        return now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if _in_war_window(
        settings["war_fortress_weekday"], settings["war_fortress_start_time"], settings["war_fortress_end_time"], now,
    ):
        end_h, end_m = (int(x) for x in settings["war_fortress_end_time"].split(":"))
        return now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return None


def _next_weekly_instant_at(weekday, time_str, now=None):
    """The next upcoming occurrence of a weekday+time pair (Taipei time),
    returned as a UTC-naive ACTION_DT_FORMAT string ready to store in a
    "when does X happen" column -- the same conversion
    country_cast_morale_buff already does on _current_war_window_end's
    return value. weekday is an ISO weekday (1=Mon..7=Sun), matching the
    war_town_weekday/war_fortress_weekday convention.

    Always strictly in the future: an instant that is exactly now (or
    earlier today) rolls forward a full week. Used by 天下武道大會 to freeze
    a cycle's registration deadline and start time at the moment the cycle
    row is created."""
    now = now if now is not None else _taipei_now()
    hour, minute = (int(x) for x in time_str.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - now.isoweekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None).strftime(ACTION_DT_FORMAT)


def _taipei_time_label(stored_at):
    """Human-readable Taipei-time rendering of a stored UTC-naive
    ACTION_DT_FORMAT instant (the inverse of _next_weekly_instant_at), for
    displaying tournament deadlines/start times on the page."""
    if not stored_at:
        return "-"
    dt = datetime.strptime(stored_at, ACTION_DT_FORMAT).replace(tzinfo=timezone.utc).astimezone(TAIPEI_TZ)
    return f"{dt.strftime('%Y-%m-%d %H:%M')}（{ISO_WEEKDAY_LABELS.get(dt.isoweekday(), '?')}）"


def _activity_log_time_label(created_at):
    """Human-readable Taipei-time rendering of an activity_log.created_at
    value -- SQLite's own `datetime('now')` default (UTC, no microseconds),
    a different shape than ACTION_DT_FORMAT, so this is deliberately its own
    helper rather than reusing _taipei_time_label."""
    dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TAIPEI_TZ)
    return dt.strftime("%m/%d %H:%M")


# 重大事件 (major events) sidebar on game.html: a curated subset of
# activity_log, one message-builder per action key. Anything not listed here
# is simply not a "major" event and never shows up, regardless of how much
# activity_log otherwise fills up with routine hunt/move/shop rows.
_MAJOR_EVENT_MESSAGE_BUILDERS = {
    "character_create": lambda username, detail: f"🆕 {detail} 加入了遊戲",
    "hidden_loot_drop": lambda username, detail: f"✨ {username} 在秘境獲得寶物：{detail}",
    "tournament_champion": lambda username, detail: f"🏆 {detail} 奪得本屆天下武道大會冠軍！",
}


def _major_event_feed(rows):
    """rows: activity_log rows (need action/detail/username/created_at) ->
    [{message, time_label}], oldest-filtered-out, newest first (assumes the
    caller already queried ORDER BY created_at DESC)."""
    events = []
    for row in rows:
        builder = _MAJOR_EVENT_MESSAGE_BUILDERS.get(row["action"])
        if builder is None:
            continue
        events.append({
            "message": builder(row["username"], row["detail"]),
            "time_label": _activity_log_time_label(row["created_at"]),
        })
    return events


def _tournament_registration_open(tournament):
    """True if this 天下武道大會 cycle row is still accepting registrations --
    shared by the tournament blueprint (authoritative check on POST) and by
    game.py's _render_game (deciding whether to show the fortress signup
    panel), same shape as _morale_buff_active."""
    return (
        tournament is not None
        and tournament["status"] == "registration"
        and datetime.utcnow() < datetime.strptime(tournament["registration_deadline_at"], ACTION_DT_FORMAT)
    )


def _morale_buff_active(country):
    """True if country["morale_buff_expires_at"] (or a character row carrying
    the bare countries.* join, per this codebase's usual aliasing) has an
    unexpired 士氣激勵 (Advisor's morale buff) still in effect."""
    expires_at = country["morale_buff_expires_at"]
    return expires_at is not None and datetime.utcnow() < datetime.strptime(expires_at, ACTION_DT_FORMAT)


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


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def _validate_username(username):
    if len(username) < MIN_USERNAME_LEN:
        return f"帳號至少需要 {MIN_USERNAME_LEN} 個字元"
    if not USERNAME_PATTERN.match(username):
        return "帳號僅能使用英文字母、數字或底線"
    return None


PASSWORD_COMPLEXITY_PATTERN = re.compile(r"^(?=.*[0-9])(?=.*[a-z])(?=.*[A-Z]).+$")


def _validate_password(password):
    if len(password) < MIN_PASSWORD_LEN or len(password) > MAX_PASSWORD_LEN:
        return f"密碼需要 {MIN_PASSWORD_LEN}～{MAX_PASSWORD_LEN} 個字元"
    if not PASSWORD_COMPLEXITY_PATTERN.match(password):
        return "密碼需同時包含數字、大寫英文字母與小寫英文字母"
    return None


def _validate_character_name(db, name, username, max_len=MAX_CHARACTER_NAME_LEN):
    if len(name) < MIN_CHARACTER_NAME_LEN or len(name) > max_len:
        return f"角色名稱需要 {MIN_CHARACTER_NAME_LEN}～{max_len} 個字元"
    if name.lower() == username.lower():
        return "角色名稱不能跟帳號相同"
    taken = db.execute(
        "SELECT id FROM characters WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if taken:
        return "這個角色名稱已經被使用了"
    return None
