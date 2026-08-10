"""Helpers shared across more than one blueprint (plus the auth/admin/
character access decorators used by all of them).

Import direction is strictly app.py -> blueprints/* -> web_helpers.py, so
this module must never import from app.py or from any blueprint."""
import io
import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import redirect, url_for, session, flash, current_app
from PIL import Image

from db import get_db
from game_data.constants import (
    MIN_USERNAME_LEN, MIN_PASSWORD_LEN, MAX_PASSWORD_LEN, MIN_CHARACTER_NAME_LEN, MAX_CHARACTER_NAME_LEN,
    GAME_LAYOUT_BLOCKS,
)
from game_data.avatars import (
    BUILT_IN_AVATAR_KEYS, DEFAULT_AVATAR_KEY, CUSTOM_AVATAR_ALLOWED_FORMATS, CUSTOM_AVATAR_FORMAT_HINT,
)
from game_data.backgrounds import (
    BACKGROUND_ALLOWED_FORMATS, BACKGROUND_FORMAT_HINT, BGM_DIR, BGM_EXTENSIONS,
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
    "boss_set_drop": lambda username, detail: f"👑 {username} {detail}",
    "skill_book_drop": lambda username, detail: f"📖 {username} 在究極打怪場獲得稀世技能書：{detail}",
    # detail is already the full "舊名 → 新名" phrase set at log_activity call
    # time in character_rename -- username here is the ACCOUNT username, same
    # convention as hidden_loot_drop/skill_book_drop above.
    "rename_character": lambda username, detail: f"✏️ {username} 更改了角色名稱：{detail}",
    "tournament_champion": lambda username, detail: f"🏆 {detail} 奪得本屆天下武道大會冠軍！",
    # detail is already the full "角色名 已X轉為「職業」" phrase (set at
    # log_activity call time in blueprints/character.py's promote_tier1..4),
    # same self-contained-detail convention as character_create above --
    # username here is the ACCOUNT username, less useful than the character
    # name already baked into detail, so it's ignored just like there.
    "promote_tier1": lambda username, detail: f"🎓 {detail}",
    "promote_tier2": lambda username, detail: f"🎓 {detail}",
    "promote_tier3": lambda username, detail: f"🎓 {detail}",
    "promote_tier4": lambda username, detail: f"🌟 {detail}",
    # detail already carries the full "擊敗XX，佔領/篡位/奪得..." phrase built
    # at log_activity call time in blueprints/game.py -- username here is the
    # winner's ACCOUNT username (same convention as hidden_loot_drop above;
    # these routes never had a character-name-only detail string to prefer).
    "conquer_win": lambda username, detail: f"🏰 {username} {detail}",
    "challenge_king_win": lambda username, detail: f"👑 {username} {detail}",
    "challenge_official_win": lambda username, detail: f"⚔️ {username} {detail}",
    "claim_vacant_king": lambda username, detail: f"👑 {username} {detail}",
}


def _major_event_feed(rows):
    """rows: activity_log rows (need action/detail/username/created_at/
    character_id) -> [{message, time_label, character_id}], oldest-filtered-
    out, newest first (assumes the caller already queried ORDER BY
    created_at DESC). character_id may be None (rows logged before the
    column existed, or a system-authored row with no single actor) -- the
    template only renders a link when it's present, see game.html."""
    events = []
    for row in rows:
        builder = _MAJOR_EVENT_MESSAGE_BUILDERS.get(row["action"])
        if builder is None:
            continue
        events.append({
            "message": builder(row["username"], row["detail"]),
            "time_label": _activity_log_time_label(row["created_at"]),
            "character_id": row["character_id"],
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


def _sanitized_action_block_order(raw_value):
    """Parse+sanitize a comma-separated action_block_order string (either the
    stored game_settings.action_block_order value, or a raw client-submitted
    `order` form field) into a complete, valid permutation of
    GAME_LAYOUT_BLOCKS's keys.

    Shared by game.py's _render_game (read path) and admin.py's
    admin_update_layout (write path, where the input is untrusted client
    data) so both apply the exact same rules: keep only known keys, dedupe
    preserving first occurrence, preserve the given order, then append any
    missing valid keys (in GAME_LAYOUT_BLOCKS's own order) at the end -- this
    guarantees the result always contains every key exactly once, so a block
    can never silently disappear from the page even if the DB row or a
    submitted form is incomplete/garbled."""
    seen = set()
    ordered = []
    for key in (raw_value or "").split(","):
        key = key.strip()
        if key in GAME_LAYOUT_BLOCKS and key not in seen:
            seen.add(key)
            ordered.append(key)
    for key in GAME_LAYOUT_BLOCKS:
        if key not in seen:
            ordered.append(key)
    return ordered


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


def avatar_url(avatar_key, avatar_custom_filename):
    """A custom upload always wins over the built-in key once one exists
    (see character.character_change_avatar) -- avatar_key is kept around as a
    fallback identity rather than cleared, so nothing breaks if the uploaded
    file is ever manually removed from disk.

    For the built-in-key fallback branch, an admin-uploaded replacement image
    (see blueprints/admin.py's /admin/images routes) wins over the shipped
    .svg if one exists on disk at static/avatars/built_in/custom/{key}.png --
    checked with a real filesystem os.path.exists (not just a URL guess),
    since there's no DB column tracking override existence in this app."""
    if avatar_custom_filename:
        return url_for("static", filename=f"avatars/uploads/{avatar_custom_filename}")
    key = avatar_key if avatar_key in BUILT_IN_AVATAR_KEYS else DEFAULT_AVATAR_KEY
    override_path = os.path.join(current_app.static_folder, "avatars", "built_in", "custom", f"{key}.png")
    if os.path.exists(override_path):
        return url_for("static", filename=f"avatars/built_in/custom/{key}.png")
    return url_for("static", filename=f"avatars/built_in/{key}.svg")


def monster_image_url(image_key):
    """Resolves a monster's (or monster-shaped opponent's, e.g. the defense
    tower / bandit lord -- see game_data/stats.py) art, same admin-override
    pattern as avatar_url: static/monsters/custom/{image_key}.png wins over
    the shipped static/monsters/{image_key}.svg if present on disk. Returns
    None for a falsy image_key so callers (e.g. templates/battle.html) can
    keep their existing "only render an <img> if monster.image_key" guard."""
    if not image_key:
        return None
    override_path = os.path.join(current_app.static_folder, "monsters", "custom", f"{image_key}.png")
    if os.path.exists(override_path):
        return url_for("static", filename=f"monsters/custom/{image_key}.png")
    return url_for("static", filename=f"monsters/{image_key}.svg")


def official_image_url(country_id, seat):
    """Admin-uploaded portrait for a country's King/Advisor/General NPC (see
    blueprints/admin.py's /admin/images/official routes), keyed by
    "{country_id}_{seat}". Unlike monsters/avatars there's no shipped
    default art for these -- returns None if nothing has been uploaded, so
    callers (templates/battle.html) only render an <img> when this is
    truthy, same guard pattern as monster.image_key."""
    if not country_id or not seat:
        return None
    path = os.path.join(current_app.static_folder, "officials", f"{country_id}_{seat}.png")
    if os.path.exists(path):
        return url_for("static", filename=f"officials/{country_id}_{seat}.png")
    return None


def favicon_url():
    """Resolves the site-wide browser-tab icon, static/favicon/custom/favicon.png
    if an admin has uploaded one (see blueprints/admin.py's admin_upload_favicon),
    else None so templates/base.html skips the <link rel="icon"> tag entirely and
    the browser falls back to its own default tab icon."""
    override_path = os.path.join(current_app.static_folder, "favicon", "custom", "favicon.png")
    if os.path.exists(override_path):
        return url_for("static", filename="favicon/custom/favicon.png")
    return None


def _process_square_image_upload(file_storage, max_bytes, dimension):
    """Validates and re-encodes an uploaded image into a fixed-size square
    PNG, returned as raw bytes -- the uploaded bytes/extension are NEVER
    trusted or stored as-is, only what Pillow can actually decode as a real
    image (and re-encode from scratch) is returned. Shared by the player-
    facing custom-avatar upload (blueprints/character.py's
    _process_avatar_upload) and the admin monster/avatar image-override
    upload (blueprints/admin.py) -- callers own writing the bytes to disk
    under whatever filename/path convention they use. Returns
    (png_bytes, error_message), exactly one of which is None."""
    data = file_storage.read()
    if not data:
        return None, "請選擇一個圖片檔案"
    if len(data) > max_bytes:
        return None, f"檔案大小超過 {max_bytes // (1024 * 1024)}MB 上限"

    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        img = Image.open(io.BytesIO(data))  # verify() consumes the first handle
        img.load()
    except Exception:
        # Pillow can raise many different exception types for a malformed or
        # disguised file (OSError/ValueError/SyntaxError/DecompressionBombError/
        # UnidentifiedImageError...) -- catching broadly here is deliberate,
        # since any failure to decode means "reject the upload", not a bug.
        return None, f"無法辨識這個圖片檔案。{CUSTOM_AVATAR_FORMAT_HINT}"

    if img.format not in CUSTOM_AVATAR_ALLOWED_FORMATS:
        return None, CUSTOM_AVATAR_FORMAT_HINT

    img = img.convert("RGBA")
    width, height = img.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize((dimension, dimension), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), None


def background_url(key):
    """Resolves an admin-uploaded background image's URL for the given
    BACKGROUND_KEYS key, same override-on-disk pattern as monster_image_url:
    static/backgrounds/custom/{key}.jpg wins if present, otherwise None (there
    is no shipped default background -- callers must gate rendering on this
    being non-None so a zero-upload game looks exactly as it does today)."""
    if not key:
        return None
    override_path = os.path.join(current_app.static_folder, "backgrounds", "custom", f"{key}.jpg")
    if os.path.exists(override_path):
        return url_for("static", filename=f"backgrounds/custom/{key}.jpg")
    return None


def _process_background_image_upload(file_storage, max_bytes, max_width, max_height):
    """Validates and re-encodes an uploaded image into a JPEG that fits within
    max_width x max_height (aspect ratio preserved, no forced crop -- CSS
    background-size:cover handles the rest on render), returned as raw bytes.
    Same "never trust the raw bytes/extension" rule as
    _process_square_image_upload, just without the square crop and with a
    JPEG re-encode instead of PNG (backgrounds don't need alpha transparency).
    Returns (jpeg_bytes, error_message), exactly one of which is None."""
    data = file_storage.read()
    if not data:
        return None, "請選擇一個圖片檔案"
    if len(data) > max_bytes:
        return None, f"檔案大小超過 {max_bytes // (1024 * 1024)}MB 上限"

    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        img = Image.open(io.BytesIO(data))  # verify() consumes the first handle
        img.load()
    except Exception:
        # Same broad catch as _process_square_image_upload -- any decode
        # failure means "reject the upload", not a bug.
        return None, f"無法辨識這個圖片檔案。{BACKGROUND_FORMAT_HINT}"

    if img.format not in BACKGROUND_ALLOWED_FORMATS:
        return None, BACKGROUND_FORMAT_HINT

    img = img.convert("RGB")
    width, height = img.size
    scale = min(max_width / width, max_height / height, 1.0)
    if scale < 1.0:
        img = img.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), None


def _sniff_audio_format(data):
    """Identifies data's audio container by magic number, independent of
    whatever extension the upload claims -- mp3 (ID3 tag or a raw MPEG frame
    sync), ogg (OggS), wav (RIFF....WAVE). Returns one of BGM_EXTENSIONS, or
    None if nothing matches. There is no audio-processing library in
    requirements.txt to actually decode/re-encode the file (unlike images,
    which are fully re-decoded through Pillow), so this magic-number sniff
    plus the extension check in admin_upload_bgm is the full trust boundary
    -- bytes that pass both are stored as-is."""
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xfa", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    return None


def bgm_url():
    """Resolves the single global background-music track's URL, whichever of
    static/audio/bgm.{mp3,ogg,wav} currently exists on disk (there is only
    ever one at a time -- see admin_upload_bgm). None if no track has been
    uploaded, so templates/base.html can skip rendering the <audio> tag
    entirely rather than pointing it at a 404."""
    for ext in BGM_EXTENSIONS:
        path = os.path.join(BGM_DIR, f"bgm.{ext}")
        if os.path.exists(path):
            return url_for("static", filename=f"audio/bgm.{ext}")
    return None


def active_announcement():
    """The admin-set site-wide announcement text, or None if disabled/empty
    so templates/base.html can skip rendering the marquee bar entirely."""
    db = get_db()
    row = db.execute(
        "SELECT announcement_text, announcement_enabled FROM game_settings WHERE id = 1"
    ).fetchone()
    db.close()
    if row is None or not row["announcement_enabled"]:
        return None
    text = row["announcement_text"].strip()
    return text or None


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
