"""Character-to-character trading."""

from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from db import get_db, log_activity
from web_helpers import character_required, _add_to_inventory, _remove_from_inventory

trade_bp = Blueprint("trade", __name__)


# ---------------------------------------------------------------------------
# Character-to-character trading. No country restriction anywhere. Both
# participants must stay online (users.is_online) for the entire negotiation
# -- re-checked on every room load and on every state-changing POST, not just
# once at invite time. Only inventory rows ever move (equipped items are
# never tradeable, since equipping already removes an item from inventory).
# ---------------------------------------------------------------------------

def _character_for_trade(db):
    return db.execute(
        """SELECT characters.id AS character_id, characters.name AS character_name,
                  characters.currency AS character_currency
           FROM characters WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


def _open_trade_for_character(db, character_id):
    """Broad "do you have any open trade at all" check (pending or active, as
    either initiator or target) -- used to enforce the "at most one open
    trade at a time" rule when a new invite is being created."""
    return db.execute(
        """SELECT id FROM trades
           WHERE status IN ('pending', 'active')
             AND (initiator_character_id = ? OR target_character_id = ?)""",
        (character_id, character_id),
    ).fetchone()


def _trade_for_home_redirect(db, character_id):
    """Narrower than _open_trade_for_character: only trades where this
    character is either (a) the initiator of a still-open (pending or
    active) invite they're waiting on / negotiating, or (b) a participant of
    an already-active (both sides accepted) negotiation. A brand new pending
    invite where this character is the TARGET does NOT match here on
    purpose, so /trade can still show it in the "pending invites" list with
    accept/decline buttons rather than immediately yanking the player into
    the trade room."""
    return db.execute(
        """SELECT id FROM trades
           WHERE (initiator_character_id = ? AND status IN ('pending', 'active'))
              OR (target_character_id = ? AND status = 'active')""",
        (character_id, character_id),
    ).fetchone()


def _load_trade(db, trade_id):
    """Explicit column list + explicit AS aliases throughout (never
    characters.*/users.* wildcards) -- this app's sqlite3.Row resolves
    dict-style access to the LAST matching column name in the SELECT list
    when two joined tables share a column name (e.g. both characters rows
    have an "id" and "name" and "currency" here), so wildcards would silently
    collide between the initiator and target sides."""
    return db.execute(
        """SELECT trades.id AS trade_id, trades.status AS trade_status,
                  trades.initiator_character_id AS initiator_character_id,
                  trades.target_character_id AS target_character_id,
                  trades.initiator_currency AS initiator_currency,
                  trades.target_currency AS target_currency,
                  trades.initiator_confirmed AS initiator_confirmed,
                  trades.target_confirmed AS target_confirmed,
                  trades.created_at AS trade_created_at,
                  trades.updated_at AS trade_updated_at,
                  ic.name AS initiator_name, ic.currency AS initiator_character_currency,
                  ic.user_id AS initiator_user_id, iu.username AS initiator_username,
                  iu.is_online AS initiator_is_online,
                  tc.name AS target_name, tc.currency AS target_character_currency,
                  tc.user_id AS target_user_id, tu.username AS target_username,
                  tu.is_online AS target_is_online
           FROM trades
           JOIN characters AS ic ON ic.id = trades.initiator_character_id
           JOIN users AS iu ON iu.id = ic.user_id
           JOIN characters AS tc ON tc.id = trades.target_character_id
           JOIN users AS tu ON tu.id = tc.user_id
           WHERE trades.id = ?""",
        (trade_id,),
    ).fetchone()


def _cancel_trade(db, trade_id):
    db.execute(
        "UPDATE trades SET status = 'cancelled', updated_at = datetime('now') WHERE id = ?",
        (trade_id,),
    )


def _auto_cancel_trade_if_offline(db, trade):
    """If the trade is still open (pending/active) and either participant's
    user is no longer online, flips it to cancelled and returns an
    explanatory flash message; returns None if no cancellation was needed.
    Called at the top of every trade view/action route so a stale open tab
    can't be used to keep negotiating after one side has logged out."""
    if trade["trade_status"] not in ("pending", "active"):
        return None
    if trade["initiator_is_online"] and trade["target_is_online"]:
        return None
    _cancel_trade(db, trade["trade_id"])
    return "交易對象已離線，交易已自動取消"


def _trade_opponent_name(trade, character_id):
    return trade["target_name"] if character_id == trade["initiator_character_id"] else trade["initiator_name"]


@trade_bp.route("/trade")
@character_required
def trade_home():
    db = get_db()
    character = _character_for_trade(db)

    room_trade = _trade_for_home_redirect(db, character["character_id"])
    if room_trade:
        db.close()
        return redirect(url_for("trade.trade_room", trade_id=room_trade["id"]))

    invites = db.execute(
        """SELECT trades.id AS trade_id, trades.created_at AS trade_created_at,
                  characters.name AS initiator_name
           FROM trades JOIN characters ON characters.id = trades.initiator_character_id
           WHERE trades.target_character_id = ? AND trades.status = 'pending'
           ORDER BY trades.created_at DESC""",
        (character["character_id"],),
    ).fetchall()
    db.close()
    return render_template("trade_home.html", invites=invites)


@trade_bp.route("/trade/invite", methods=["POST"])
@character_required
def trade_invite():
    db = get_db()
    character = _character_for_trade(db)
    target_name = request.form.get("target_name", "").strip()

    if not target_name:
        db.close()
        flash("請輸入要邀請交易的角色名稱")
        return redirect(url_for("trade.trade_home"))

    if _open_trade_for_character(db, character["character_id"]):
        db.close()
        flash("你目前已經有進行中的交易")
        return redirect(url_for("trade.trade_home"))

    target = db.execute(
        """SELECT characters.id AS character_id, characters.name AS character_name,
                  users.is_online AS user_is_online
           FROM characters JOIN users ON users.id = characters.user_id
           WHERE lower(characters.name) = lower(?)""",
        (target_name,),
    ).fetchone()

    if target is None:
        db.close()
        flash("找不到這個角色")
        return redirect(url_for("trade.trade_home"))

    if target["character_id"] == character["character_id"]:
        db.close()
        flash("不能邀請自己交易")
        return redirect(url_for("trade.trade_home"))

    if not target["user_is_online"]:
        db.close()
        flash(f"{target['character_name']} 目前不在線上，無法邀請交易")
        return redirect(url_for("trade.trade_home"))

    if _open_trade_for_character(db, target["character_id"]):
        db.close()
        flash(f"{target['character_name']} 目前已經有進行中的交易")
        return redirect(url_for("trade.trade_home"))

    cur = db.execute(
        """INSERT INTO trades (initiator_character_id, target_character_id, status)
           VALUES (?, ?, 'pending')""",
        (character["character_id"], target["character_id"]),
    )
    trade_id = cur.lastrowid
    log_activity(
        db, session["user_id"], session["username"], "trade_invite",
        detail=f"邀請 {target['character_name']} 進行交易", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已邀請 {target['character_name']} 進行交易")
    return redirect(url_for("trade.trade_home"))


@trade_bp.route("/trade/<int:trade_id>/accept", methods=["POST"])
@character_required
def trade_accept(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade.trade_home"))

    if character["character_id"] != trade["target_character_id"]:
        db.close()
        flash("只有受邀請的一方可以接受交易")
        return redirect(url_for("trade.trade_home"))

    if trade["trade_status"] != "pending":
        db.close()
        flash("這筆交易目前無法接受")
        return redirect(url_for("trade.trade_home"))

    if not trade["initiator_is_online"]:
        _cancel_trade(db, trade_id)
        db.commit()
        db.close()
        flash(f"{trade['initiator_name']} 已離線，交易已自動取消")
        return redirect(url_for("trade.trade_home"))

    db.execute(
        "UPDATE trades SET status = 'active', updated_at = datetime('now') WHERE id = ?", (trade_id,)
    )
    log_activity(
        db, session["user_id"], session["username"], "trade_accept",
        detail=f"接受與 {trade['initiator_name']} 的交易邀請", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("已接受交易邀請")
    return redirect(url_for("trade.trade_room", trade_id=trade_id))


@trade_bp.route("/trade/<int:trade_id>/decline", methods=["POST"])
@character_required
def trade_decline(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade.trade_home"))

    if trade["trade_status"] != "pending":
        db.close()
        flash("這筆交易目前無法拒絕")
        return redirect(url_for("trade.trade_home"))

    _cancel_trade(db, trade_id)
    log_activity(
        db, session["user_id"], session["username"], "trade_decline",
        detail=f"拒絕與 {_trade_opponent_name(trade, character['character_id'])} 的交易邀請",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("已拒絕交易邀請")
    return redirect(url_for("trade.trade_home"))


@trade_bp.route("/trade/<int:trade_id>/cancel", methods=["POST"])
@character_required
def trade_cancel(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade.trade_home"))

    if trade["trade_status"] not in ("pending", "active"):
        db.close()
        flash("這筆交易已經結束，無法取消")
        return redirect(url_for("trade.trade_home"))

    _cancel_trade(db, trade_id)
    log_activity(
        db, session["user_id"], session["username"], "trade_cancel",
        detail=f"取消與 {_trade_opponent_name(trade, character['character_id'])} 的交易",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("已取消交易")
    return redirect(url_for("trade.trade_home"))


@trade_bp.route("/trade/<int:trade_id>")
@character_required
def trade_room(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade.trade_home"))

    cancel_reason = _auto_cancel_trade_if_offline(db, trade)
    if cancel_reason:
        db.commit()
        db.close()
        flash(cancel_reason)
        return redirect(url_for("trade.trade_home"))

    is_initiator = character["character_id"] == trade["initiator_character_id"]
    my_currency_offer = trade["initiator_currency"] if is_initiator else trade["target_currency"]
    opp_currency_offer = trade["target_currency"] if is_initiator else trade["initiator_currency"]
    my_confirmed = bool(trade["initiator_confirmed"] if is_initiator else trade["target_confirmed"])
    opp_confirmed = bool(trade["target_confirmed"] if is_initiator else trade["initiator_confirmed"])
    opp_character_id = trade["target_character_id"] if is_initiator else trade["initiator_character_id"]
    opp_name = _trade_opponent_name(trade, character["character_id"])

    my_offered_qty_by_item = {
        row["item_id"]: row["quantity"]
        for row in db.execute(
            "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND character_id = ?",
            (trade_id, character["character_id"]),
        ).fetchall()
    }
    my_inventory = db.execute(
        """SELECT items.id AS item_id, items.name AS item_name, items.shop_type AS item_shop_type,
                  inventory.quantity AS inventory_quantity
           FROM inventory JOIN items ON items.id = inventory.item_id
           WHERE inventory.character_id = ?
           ORDER BY items.shop_type, items.name""",
        (character["character_id"],),
    ).fetchall()
    my_offer_rows = [
        {
            "item_id": row["item_id"],
            "name": row["item_name"],
            "shop_type": row["item_shop_type"],
            "max_quantity": row["inventory_quantity"],
            "offered_quantity": my_offered_qty_by_item.get(row["item_id"], 0),
        }
        for row in my_inventory
    ]

    opp_items = db.execute(
        """SELECT items.name AS item_name, trade_items.quantity AS quantity
           FROM trade_items JOIN items ON items.id = trade_items.item_id
           WHERE trade_items.trade_id = ? AND trade_items.character_id = ?
           ORDER BY items.shop_type, items.name""",
        (trade_id, opp_character_id),
    ).fetchall()

    db.close()
    return render_template(
        "trade_room.html",
        trade=trade,
        is_initiator=is_initiator,
        opp_name=opp_name,
        my_currency_offer=my_currency_offer,
        opp_currency_offer=opp_currency_offer,
        my_confirmed=my_confirmed,
        opp_confirmed=opp_confirmed,
        my_offer_rows=my_offer_rows,
        opp_items=opp_items,
        my_currency_available=character["character_currency"],
    )


@trade_bp.route("/trade/<int:trade_id>/offer", methods=["POST"])
@character_required
def trade_offer(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade.trade_home"))

    cancel_reason = _auto_cancel_trade_if_offline(db, trade)
    if cancel_reason:
        db.commit()
        db.close()
        flash(cancel_reason)
        return redirect(url_for("trade.trade_home"))

    if trade["trade_status"] != "active":
        db.close()
        flash("這筆交易目前無法調整報價")
        return redirect(url_for("trade.trade_home"))

    raw_amount = request.form.get("currency", "0").strip()
    try:
        amount = int(raw_amount)
    except ValueError:
        amount = 0
    amount = max(0, min(amount, character["character_currency"]))

    inventory_rows = db.execute(
        "SELECT item_id, quantity FROM inventory WHERE character_id = ?",
        (character["character_id"],),
    ).fetchall()

    db.execute(
        "DELETE FROM trade_items WHERE trade_id = ? AND character_id = ?",
        (trade_id, character["character_id"]),
    )
    for row in inventory_rows:
        raw_qty = request.form.get(f"item_qty_{row['item_id']}", "0").strip()
        try:
            qty = int(raw_qty)
        except ValueError:
            qty = 0
        qty = max(0, min(qty, row["quantity"]))
        if qty > 0:
            db.execute(
                "INSERT INTO trade_items (trade_id, character_id, item_id, quantity) VALUES (?, ?, ?, ?)",
                (trade_id, character["character_id"], row["item_id"], qty),
            )

    is_initiator = character["character_id"] == trade["initiator_character_id"]
    currency_column = "initiator_currency" if is_initiator else "target_currency"
    # Any successful offer update resets BOTH confirmation flags -- standard
    # anti-scam trade-window behavior so a stale confirmation never silently
    # carries over onto a changed offer.
    db.execute(
        f"""UPDATE trades SET {currency_column} = ?, initiator_confirmed = 0, target_confirmed = 0,
               updated_at = datetime('now') WHERE id = ?""",
        (amount, trade_id),
    )
    db.commit()
    db.close()
    flash("已更新你的交易報價")
    return redirect(url_for("trade.trade_room", trade_id=trade_id))


@trade_bp.route("/trade/<int:trade_id>/confirm", methods=["POST"])
@character_required
def trade_confirm(trade_id):
    db = get_db()
    character = _character_for_trade(db)
    trade = _load_trade(db, trade_id)
    if trade is None or character["character_id"] not in (
        trade["initiator_character_id"], trade["target_character_id"],
    ):
        db.close()
        flash("找不到這筆交易")
        return redirect(url_for("trade.trade_home"))

    cancel_reason = _auto_cancel_trade_if_offline(db, trade)
    if cancel_reason:
        db.commit()
        db.close()
        flash(cancel_reason)
        return redirect(url_for("trade.trade_home"))

    if trade["trade_status"] != "active":
        db.close()
        flash("這筆交易目前無法確認")
        return redirect(url_for("trade.trade_home"))

    is_initiator = character["character_id"] == trade["initiator_character_id"]
    confirm_column = "initiator_confirmed" if is_initiator else "target_confirmed"
    db.execute(
        f"UPDATE trades SET {confirm_column} = 1, updated_at = datetime('now') WHERE id = ?",
        (trade_id,),
    )

    trade = _load_trade(db, trade_id)
    if not (trade["initiator_confirmed"] and trade["target_confirmed"]):
        db.commit()
        db.close()
        flash("已確認你的交易報價，等待對方確認")
        return redirect(url_for("trade.trade_room", trade_id=trade_id))

    # Both sides just confirmed -- attempt to finalize. Re-check online
    # status first (cheap), then re-validate both sides still actually have
    # what they offered (defensive: something may have changed since the
    # offer was set, e.g. spent currency or sold/equipped an item elsewhere).
    if not (trade["initiator_is_online"] and trade["target_is_online"]):
        _cancel_trade(db, trade_id)
        db.commit()
        db.close()
        flash("交易對象已離線，交易已自動取消")
        return redirect(url_for("trade.trade_home"))

    initiator_items = db.execute(
        "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND character_id = ?",
        (trade_id, trade["initiator_character_id"]),
    ).fetchall()
    target_items = db.execute(
        "SELECT item_id, quantity FROM trade_items WHERE trade_id = ? AND character_id = ?",
        (trade_id, trade["target_character_id"]),
    ).fetchall()

    valid = (
        trade["initiator_currency"] <= trade["initiator_character_currency"]
        and trade["target_currency"] <= trade["target_character_currency"]
    )
    if valid:
        for row in initiator_items:
            have = db.execute(
                "SELECT quantity FROM inventory WHERE character_id = ? AND item_id = ?",
                (trade["initiator_character_id"], row["item_id"]),
            ).fetchone()
            if have is None or have["quantity"] < row["quantity"]:
                valid = False
                break
    if valid:
        for row in target_items:
            have = db.execute(
                "SELECT quantity FROM inventory WHERE character_id = ? AND item_id = ?",
                (trade["target_character_id"], row["item_id"]),
            ).fetchone()
            if have is None or have["quantity"] < row["quantity"]:
                valid = False
                break

    if not valid:
        db.execute(
            """UPDATE trades SET initiator_confirmed = 0, target_confirmed = 0,
                   updated_at = datetime('now') WHERE id = ?""",
            (trade_id,),
        )
        db.commit()
        db.close()
        flash("交易物品或諸神幣數量有變動，請重新確認雙方報價")
        return redirect(url_for("trade.trade_room", trade_id=trade_id))

    db.execute(
        "UPDATE characters SET currency = currency + ? WHERE id = ?",
        (trade["target_currency"] - trade["initiator_currency"], trade["initiator_character_id"]),
    )
    db.execute(
        "UPDATE characters SET currency = currency + ? WHERE id = ?",
        (trade["initiator_currency"] - trade["target_currency"], trade["target_character_id"]),
    )
    for row in initiator_items:
        _remove_from_inventory(db, trade["initiator_character_id"], row["item_id"], row["quantity"])
        _add_to_inventory(db, trade["target_character_id"], row["item_id"], row["quantity"])
    for row in target_items:
        _remove_from_inventory(db, trade["target_character_id"], row["item_id"], row["quantity"])
        _add_to_inventory(db, trade["initiator_character_id"], row["item_id"], row["quantity"])

    db.execute(
        "UPDATE trades SET status = 'completed', updated_at = datetime('now') WHERE id = ?",
        (trade_id,),
    )
    log_activity(
        db, trade["initiator_user_id"], trade["initiator_username"], "trade_complete",
        detail=f"與 {trade['target_name']} 完成交易", ip_address=request.remote_addr,
    )
    log_activity(
        db, trade["target_user_id"], trade["target_username"], "trade_complete",
        detail=f"與 {trade['initiator_name']} 完成交易", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("交易完成！")
    return redirect(url_for("trade.trade_room", trade_id=trade_id))
