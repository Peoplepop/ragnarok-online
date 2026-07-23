import os
import sqlite3
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-secret-change-me")

COUNTRIES = [
    {"name": "百鍊流金國", "element": "金", "desc": "初始幸運值較高，閃避與命中俱佳"},
    {"name": "翡翠靈木國", "element": "木", "desc": "生生不息之地"},
    {"name": "蔚藍千泉國", "element": "水", "desc": "以柔克剛之邦"},
    {"name": "紅蓮業火國", "element": "火", "desc": "烈焰焚天之國"},
    {"name": "萬物母育國", "element": "土", "desc": "厚德載物之土"},
]

MIN_USERNAME_LEN = 3
MIN_PASSWORD_LEN = 6

if not os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "game.db")):
    init_db()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("請先登入")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    return render_template("index.html", countries=COUNTRIES)


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
        db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
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
    db.close()

    if user is None or not check_password_hash(user["password_hash"], password):
        flash("帳號或密碼錯誤")
        return render_template("login.html")

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    flash(f"歡迎回來，{user['username']}")
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
