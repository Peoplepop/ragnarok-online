"""玩家意見箱: a player's own suggestions to the admin. Visible only to the
submitter and to admins -- there is no public listing anywhere (not the
重大事件 feed, not chat). Admin-side status editing lives in blueprints/admin.py
alongside the other admin-only pages."""
from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from db import get_db, log_activity
from web_helpers import character_required
from game_data.constants import FEEDBACK_MESSAGE_MAX_LEN, FEEDBACK_STATUS_LABELS

feedback_bp = Blueprint("feedback", __name__)


@feedback_bp.route("/feedback")
@character_required
def feedback_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM feedback WHERE user_id = ? ORDER BY id DESC", (session["user_id"],)
    ).fetchall()
    db.close()
    return render_template(
        "feedback.html", feedback_list=rows, status_labels=FEEDBACK_STATUS_LABELS,
        message_max_len=FEEDBACK_MESSAGE_MAX_LEN,
    )


@feedback_bp.route("/feedback/submit", methods=["POST"])
@character_required
def feedback_submit():
    message = request.form.get("message", "").strip()
    if not message:
        flash("建議內容不可為空白")
        return redirect(url_for("feedback.feedback_page"))
    if len(message) > FEEDBACK_MESSAGE_MAX_LEN:
        flash(f"建議內容不可超過 {FEEDBACK_MESSAGE_MAX_LEN} 字")
        return redirect(url_for("feedback.feedback_page"))

    db = get_db()
    character = db.execute(
        "SELECT name FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    db.execute(
        "INSERT INTO feedback (user_id, character_name, message) VALUES (?, ?, ?)",
        (session["user_id"], character["name"], message),
    )
    log_activity(db, session["user_id"], session["username"], "feedback_submit", ip_address=request.remote_addr)
    db.commit()
    db.close()
    flash("已送出你的建議，感謝回饋！")
    return redirect(url_for("feedback.feedback_page"))
