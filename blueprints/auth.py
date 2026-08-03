"""Landing page, registration, login/logout."""
import sqlite3

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, log_activity
from web_helpers import _validate_username, _validate_password, _validate_character_name
from game_data.avatars import BUILT_IN_AVATARS, BUILT_IN_AVATAR_KEYS, DEFAULT_AVATAR_KEY

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/")
def index():
    if session.get("user_id"):
        db = get_db()
        character = db.execute(
            "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
        ).fetchone()
        db.close()
        if character is None:
            return redirect(url_for("character.character_create"))
        return redirect(url_for("game.game"))

    db = get_db()
    countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
    db.close()
    return render_template("index.html", countries=countries)


def _selected_country(db, country_id):
    if not country_id:
        return None
    return db.execute("SELECT * FROM countries WHERE id = ?", (country_id,)).fetchone()


def _render_register(country, avatar_key):
    return render_template(
        "register.html", country=country, built_in_avatars=BUILT_IN_AVATARS,
        selected_avatar_key=avatar_key,
    )


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        db = get_db()
        country = _selected_country(db, request.args.get("country_id", ""))
        db.close()
        return _render_register(country, DEFAULT_AVATAR_KEY)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    character_name = request.form.get("character_name", "").strip()
    country_id = request.form.get("country_id", "")
    avatar_key = request.form.get("avatar_key", "")

    db = get_db()
    country = _selected_country(db, country_id)

    username_error = _validate_username(username)
    if username_error:
        db.close()
        flash(username_error)
        return _render_register(country, avatar_key)
    password_error = _validate_password(password)
    if password_error:
        db.close()
        flash(password_error)
        return _render_register(country, avatar_key)
    if password != confirm:
        db.close()
        flash("兩次輸入的密碼不一致")
        return _render_register(country, avatar_key)
    if avatar_key not in BUILT_IN_AVATAR_KEYS:
        db.close()
        flash("請選擇一個頭像")
        return _render_register(country, avatar_key)

    name_error = _validate_character_name(db, character_name, username)
    if name_error:
        db.close()
        flash(name_error)
        return _render_register(country, avatar_key)

    try:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, avatar_key) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), avatar_key),
        )
        log_activity(db, cur.lastrowid, username, "register", ip_address=request.remote_addr)
        db.commit()
    except sqlite3.IntegrityError:
        db.close()
        flash("這個帳號已經被註冊了")
        return _render_register(country, avatar_key)
    finally:
        db.close()

    session["pending_character_name"] = character_name
    if country is not None:
        session["pending_country_id"] = country["id"]
    flash("註冊成功，請登入")
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
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

    if user["is_locked"]:
        log_activity(db, user["id"], user["username"], "login_locked", ip_address=request.remote_addr)
        db.commit()
        db.close()
        flash("此帳號已被管理員鎖定，暫時無法登入")
        return render_template("login.html")

    if user["must_reset_password"]:
        # Deliberately does NOT set session["user_id"] -- until the reset
        # flow completes this account isn't "logged in" for login_required/
        # character_required's purposes, so it can't reach anything else
        # (including /game) by guessing a URL. Only pending_reset_user_id is
        # set, which auth.reset_password itself checks for.
        log_activity(
            db, user["id"], user["username"], "login_forced_reset", ip_address=request.remote_addr,
        )
        db.commit()
        db.close()
        session["pending_reset_user_id"] = user["id"]
        flash("管理員已重設你的密碼，請先設定一組新密碼")
        return redirect(url_for("auth.reset_password"))

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
    session["avatar_key"] = user["avatar_key"]
    session["avatar_custom_filename"] = user["avatar_custom_filename"]
    flash(f"歡迎回來，{character['name'] if character else user['username']}")
    return redirect(url_for("auth.index"))


@auth_bp.route("/logout")
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
    return redirect(url_for("auth.index"))


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    """Only reachable via the session["pending_reset_user_id"] auth.login
    sets for an account with must_reset_password -- there is no normal
    session["user_id"] at this point, so login_required/character_required
    can't be used to gate this route the usual way."""
    user_id = session.get("pending_reset_user_id")
    if not user_id:
        flash("請先登入")
        return redirect(url_for("auth.login"))

    if request.method == "GET":
        return render_template("reset_password.html")

    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    password_error = _validate_password(password)
    if password_error:
        flash(password_error)
        return render_template("reset_password.html")
    if password != confirm:
        flash("兩次輸入的密碼不一致")
        return render_template("reset_password.html")

    db = get_db()
    user = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        db.close()
        session.pop("pending_reset_user_id", None)
        flash("請先登入")
        return redirect(url_for("auth.login"))

    db.execute(
        "UPDATE users SET password_hash = ?, must_reset_password = 0, password_reset_requested = 0 WHERE id = ?",
        (generate_password_hash(password), user_id),
    )
    log_activity(db, user_id, user["username"], "password_reset_complete", ip_address=request.remote_addr)
    db.commit()
    db.close()

    session.pop("pending_reset_user_id", None)
    flash("密碼已更新，請用新密碼重新登入")
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Self-service entry point for a player who can't log in at all (so
    auth.reset_password's own session["pending_reset_user_id"] gate, which
    only auth.login sets after a successful password check, is unreachable
    for them). Instead this remembers the claimed username in THIS browser's
    session (session["forgot_password_username"]) across a request/approve/
    revisit cycle -- no email, so re-checking on GET is the only way this
    browser finds out an admin has approved it."""
    username = session.get("forgot_password_username")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        db = get_db()
        user = db.execute(
            "SELECT id, username, is_npc, must_reset_password, password_reset_requested "
            "FROM users WHERE username = ?", (username,),
        ).fetchone()
        if user is None or user["is_npc"]:
            db.close()
            flash("找不到這個帳號")
            return render_template("forgot_password.html", state="form")

        session["forgot_password_username"] = username
        if user["must_reset_password"]:
            # Already approved (or an admin reset it directly without a
            # request ever being filed) -- skip straight to the same reset
            # screen auth.login's forced-reset path uses.
            db.close()
            session["pending_reset_user_id"] = user["id"]
            session.pop("forgot_password_username", None)
            return redirect(url_for("auth.reset_password"))

        if not user["password_reset_requested"]:
            db.execute(
                "UPDATE users SET password_reset_requested = 1 WHERE id = ?", (user["id"],)
            )
            log_activity(
                db, user["id"], user["username"], "password_reset_requested",
                ip_address=request.remote_addr,
            )
            db.commit()
        db.close()
        return render_template("forgot_password.html", state="pending")

    if not username:
        return render_template("forgot_password.html", state="form")

    db = get_db()
    user = db.execute(
        "SELECT id, must_reset_password, password_reset_requested FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    db.close()

    if user is None:
        session.pop("forgot_password_username", None)
        return render_template("forgot_password.html", state="form")
    if user["must_reset_password"]:
        session["pending_reset_user_id"] = user["id"]
        session.pop("forgot_password_username", None)
        return redirect(url_for("auth.reset_password"))
    if user["password_reset_requested"]:
        return render_template("forgot_password.html", state="pending")

    session.pop("forgot_password_username", None)
    return render_template("forgot_password.html", state="form")
