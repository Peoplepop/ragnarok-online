"""Two-channel chat: 公共頻道 (everyone) and 國家頻道 (own country only).

AJAX-only by design (POST /chat/send and GET /chat/messages both return
JSON, never a redirect) -- game.html polls /chat/messages every few seconds
and posts through fetch() so a message send never reloads the page, unlike
every other action in this app which redirects back to a full-page render.
"""
from flask import Blueprint, jsonify, request, session

from db import get_db
from web_helpers import character_required, _activity_log_time_label, avatar_url
from game_data.constants import CHAT_MESSAGE_MAX_LEN

chat_bp = Blueprint("chat", __name__)

CHANNELS = ("public", "country")


def _character_for_chat(db):
    """character_id/character_name/country_id/country_name for the logged-in
    user -- country_id/name always come from the character's OWN row, never
    from client input, so a player can never post into (or read) another
    country's channel by passing a different country_id."""
    return db.execute(
        """SELECT characters.id AS character_id, characters.name AS character_name,
                  characters.country_id, countries.name AS country_name
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


@chat_bp.route("/chat/send", methods=["POST"])
@character_required
def chat_send():
    channel = request.form.get("channel", "")
    message = request.form.get("message", "").strip()

    if channel not in CHANNELS:
        return jsonify({"ok": False, "error": "無效的頻道"}), 400
    if not message:
        return jsonify({"ok": False, "error": "訊息不可為空白"}), 400
    if len(message) > CHAT_MESSAGE_MAX_LEN:
        return jsonify({"ok": False, "error": f"訊息長度不可超過 {CHAT_MESSAGE_MAX_LEN} 字"}), 400

    db = get_db()
    character = _character_for_chat(db)
    db.execute(
        """INSERT INTO chat_messages (channel, country_id, character_id, character_name, country_name, message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            channel, character["country_id"] if channel == "country" else None,
            character["character_id"], character["character_name"], character["country_name"], message,
        ),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True})


@chat_bp.route("/chat/messages")
@character_required
def chat_messages():
    channel = request.args.get("channel", "")
    if channel not in CHANNELS:
        return jsonify({"ok": False, "error": "無效的頻道"}), 400

    db = get_db()
    character = _character_for_chat(db)
    # Avatar is looked up LIVE via this join (character_id -> users), unlike
    # character_name/country_name which are denormalized snapshots taken at
    # send-time -- an old message shows whatever avatar its poster wears
    # RIGHT NOW, matching how ordinary chat apps behave, rather than freezing
    # a picture that may since have been changed (post-四轉 avatar change).
    if channel == "country":
        rows = db.execute(
            """SELECT chat_messages.character_id, chat_messages.character_name,
                      chat_messages.country_name,
                      chat_messages.message, chat_messages.created_at,
                      users.avatar_key, users.avatar_custom_filename
               FROM chat_messages
               JOIN characters ON characters.id = chat_messages.character_id
               JOIN users ON users.id = characters.user_id
               WHERE chat_messages.channel = 'country' AND chat_messages.country_id = ?
               ORDER BY chat_messages.id DESC LIMIT 50""",
            (character["country_id"],),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT chat_messages.character_id, chat_messages.character_name,
                      chat_messages.country_name,
                      chat_messages.message, chat_messages.created_at,
                      users.avatar_key, users.avatar_custom_filename
               FROM chat_messages
               JOIN characters ON characters.id = chat_messages.character_id
               JOIN users ON users.id = characters.user_id
               WHERE chat_messages.channel = 'public'
               ORDER BY chat_messages.id DESC LIMIT 50"""
        ).fetchall()
    db.close()

    messages = [
        {
            "character_id": r["character_id"],
            "character_name": r["character_name"],
            "country_name": r["country_name"],
            "message": r["message"],
            "time_label": _activity_log_time_label(r["created_at"]),
            "avatar_url": avatar_url(r["avatar_key"], r["avatar_custom_filename"]),
        }
        for r in reversed(rows)  # oldest first, newest at the bottom
    ]
    return jsonify({"ok": True, "messages": messages})
