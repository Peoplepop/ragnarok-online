"""Admin console: sessions, logs, country roles, game settings, image
overrides (monster art / built-in avatar picker, see /admin/images below),
and background image/music overrides (see /admin/backgrounds below)."""
import os
import secrets
import sqlite3
import string
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from werkzeug.security import generate_password_hash

from db import get_db, LEVEL_CAP, log_activity, DEFAULT_MONSTERS, HIDDEN_MONSTERS
from web_helpers import (
    admin_required, _parse_dt, _valid_war_time, _format_duration, _sanitized_action_block_order,
    avatar_url, monster_image_url, _process_square_image_upload,
    background_url, bgm_url, _process_background_image_upload, _sniff_audio_format,
)
from game_data.constants import (
    STAT_FIELDS, IDLE_THRESHOLD_MINUTES, ACTION_LABELS, GOVERNMENT_ROLES,
    FEEDBACK_STATUSES, FEEDBACK_STATUS_LABELS, GAME_LAYOUT_BLOCKS,
)
from game_data.avatars import (
    BUILT_IN_AVATARS, BUILT_IN_AVATAR_KEYS, CUSTOM_AVATAR_MAX_BYTES, CUSTOM_AVATAR_DIMENSION,
)
from game_data.backgrounds import (
    BACKGROUND_KEYS, BACKGROUND_CUSTOM_DIR, BACKGROUND_MAX_BYTES, BACKGROUND_MAX_WIDTH, BACKGROUND_MAX_HEIGHT,
    BGM_DIR, BGM_MAX_BYTES, BGM_EXTENSIONS, BGM_FORMAT_HINT,
)

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/admin")
@admin_required
def admin():
    db = get_db()
    countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
    characters = db.execute("SELECT id, name, country_id, is_npc FROM characters ORDER BY name").fetchall()
    total_views = db.execute("SELECT total_views FROM site_visits WHERE id = 1").fetchone()["total_views"]
    unique_visitors = db.execute("SELECT COUNT(*) AS c FROM site_visitors").fetchone()["c"]
    db.close()

    characters_by_country = {}
    for c in characters:
        characters_by_country.setdefault(c["country_id"], []).append(c)

    return render_template(
        "admin.html", countries=countries, characters_by_country=characters_by_country,
        roles=GOVERNMENT_ROLES, active_tab="countries",
        total_views=total_views, unique_visitors=unique_visitors,
    )


@admin_bp.route("/admin/sessions")
@admin_required
def admin_sessions():
    db = get_db()
    users = db.execute(
        "SELECT id, username, is_admin, is_online, is_locked, must_reset_password, "
        "password_reset_requested, last_login_at, last_seen_at "
        "FROM users WHERE is_npc = 0 ORDER BY last_seen_at IS NULL, last_seen_at DESC"
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
            "id": u["id"],
            "username": u["username"],
            "is_admin": u["is_admin"],
            "is_locked": u["is_locked"],
            "must_reset_password": u["must_reset_password"],
            "password_reset_requested": u["password_reset_requested"],
            "status": status,
            "last_login_at": u["last_login_at"],
            "last_seen_at": u["last_seen_at"],
            "duration": _format_duration(duration_seconds),
            "idle": _format_duration(idle_seconds),
        })

    return render_template(
        "admin_sessions.html", rows=rows, idle_threshold=IDLE_THRESHOLD_MINUTES, active_tab="sessions",
    )


@admin_bp.route("/admin/users/<int:user_id>/toggle_lock", methods=["POST"])
@admin_required
def admin_toggle_user_lock(user_id):
    db = get_db()
    target = db.execute(
        "SELECT id, username, is_npc, is_locked FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if target is None or target["is_npc"]:
        db.close()
        flash("找不到這個玩家帳號")
        return redirect(url_for("admin.admin_sessions"))
    if target["id"] == session["user_id"]:
        db.close()
        flash("不能鎖定自己的帳號")
        return redirect(url_for("admin.admin_sessions"))

    new_locked = 0 if target["is_locked"] else 1
    db.execute("UPDATE users SET is_locked = ? WHERE id = ?", (new_locked, target["id"]))
    log_activity(
        db, session["user_id"], session["username"],
        "admin_lock_user" if new_locked else "admin_unlock_user",
        detail=target["username"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已{'鎖定' if new_locked else '解除鎖定'}帳號「{target['username']}」")
    return redirect(url_for("admin.admin_sessions"))


def _generate_temp_password():
    """A random password that already satisfies _validate_password's own
    complexity rule (digit + lower + upper), so the admin never has to
    manually retry -- one guaranteed char from each required class, padded
    to 10 with a shared pool, then shuffled so the classes aren't positional."""
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    pool = string.ascii_letters + string.digits
    chars = required + [secrets.choice(pool) for _ in range(7)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


@admin_bp.route("/admin/users/<int:user_id>/reset_password", methods=["POST"])
@admin_required
def admin_reset_user_password(user_id):
    db = get_db()
    target = db.execute(
        "SELECT id, username, is_npc FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if target is None or target["is_npc"]:
        db.close()
        flash("找不到這個玩家帳號")
        return redirect(url_for("admin.admin_sessions"))

    temp_password = _generate_temp_password()
    db.execute(
        "UPDATE users SET password_hash = ?, must_reset_password = 1 WHERE id = ?",
        (generate_password_hash(temp_password), target["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "admin_reset_password",
        detail=target["username"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(
        f"已重設「{target['username']}」的密碼，臨時密碼為「{temp_password}」，請告知玩家；"
        f"玩家登入後會直接進入設定新密碼的畫面"
    )
    return redirect(url_for("admin.admin_sessions"))


@admin_bp.route("/admin/users/<int:user_id>/approve_password_reset", methods=["POST"])
@admin_required
def admin_approve_password_reset(user_id):
    """Approves a player's self-service "忘記密碼" request (auth.forgot_password)
    -- unlike admin_reset_user_password above, this never touches
    password_hash at all, since the player sets their own new password
    directly once they revisit /forgot-password and it notices
    must_reset_password has flipped on."""
    db = get_db()
    target = db.execute(
        "SELECT id, username, is_npc, password_reset_requested FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if target is None or target["is_npc"]:
        db.close()
        flash("找不到這個玩家帳號")
        return redirect(url_for("admin.admin_sessions"))
    if not target["password_reset_requested"]:
        db.close()
        flash("這個帳號目前沒有忘記密碼的申請")
        return redirect(url_for("admin.admin_sessions"))

    db.execute(
        "UPDATE users SET password_reset_requested = 0, must_reset_password = 1 WHERE id = ?",
        (target["id"],),
    )
    log_activity(
        db, session["user_id"], session["username"], "admin_approve_password_reset",
        detail=target["username"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已核准「{target['username']}」的忘記密碼申請，玩家回到忘記密碼頁面即可自行設定新密碼")
    return redirect(url_for("admin.admin_sessions"))


def _delete_player_and_records(db, user_id):
    """Cascade-deletes a real (non-NPC) player's login account, character,
    and every row anywhere in the schema that references that character or
    user -- including their own activity_log entries, per the explicit
    "玩家所有紀錄也都會被清空" requirement. Office seats and tile mayorship
    are cleared (set to NULL) rather than left dangling; nothing else about
    the country/tile/tournament-cycle rows themselves is touched. Caller is
    responsible for the is_npc/self-delete guards -- this function trusts
    user_id unconditionally."""
    character = db.execute(
        "SELECT id FROM characters WHERE user_id = ?", (user_id,)
    ).fetchone()

    if character is not None:
        character_id = character["id"]
        db.execute("DELETE FROM character_skills WHERE character_id = ?", (character_id,))
        db.execute("DELETE FROM character_skill_books WHERE character_id = ?", (character_id,))
        db.execute("DELETE FROM job_masteries WHERE character_id = ?", (character_id,))
        db.execute("DELETE FROM garrisons WHERE character_id = ?", (character_id,))
        db.execute("DELETE FROM inventory WHERE character_id = ?", (character_id,))
        db.execute("DELETE FROM chat_messages WHERE character_id = ?", (character_id,))

        trade_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM trades WHERE initiator_character_id = ? OR target_character_id = ?",
                (character_id, character_id),
            ).fetchall()
        ]
        for trade_id in trade_ids:
            db.execute("DELETE FROM trade_items WHERE trade_id = ?", (trade_id,))
        db.execute(
            "DELETE FROM trades WHERE initiator_character_id = ? OR target_character_id = ?",
            (character_id, character_id),
        )

        registration_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM tournament_registrations WHERE character_id = ?", (character_id,)
            ).fetchall()
        ]
        for reg_id in registration_ids:
            db.execute(
                """DELETE FROM tournament_matches
                   WHERE registration_a_id = ? OR registration_b_id = ? OR winner_registration_id = ?""",
                (reg_id, reg_id, reg_id),
            )
        db.execute("DELETE FROM tournament_registrations WHERE character_id = ?", (character_id,))

        db.execute(
            "UPDATE map_tiles SET mayor_character_id = NULL WHERE mayor_character_id = ?", (character_id,)
        )
        db.execute(
            "UPDATE countries SET king_character_id = NULL WHERE king_character_id = ?", (character_id,)
        )
        db.execute(
            "UPDATE countries SET advisor_character_id = NULL WHERE advisor_character_id = ?", (character_id,)
        )
        db.execute(
            "UPDATE countries SET general_character_id = NULL WHERE general_character_id = ?", (character_id,)
        )

        db.execute("DELETE FROM characters WHERE id = ?", (character_id,))

    db.execute("DELETE FROM feedback WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM activity_log WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))


@admin_bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    db = get_db()
    target = db.execute(
        "SELECT id, username, is_npc FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if target is None or target["is_npc"]:
        db.close()
        flash("找不到這個玩家帳號")
        return redirect(url_for("admin.admin_sessions"))
    if target["id"] == session["user_id"]:
        db.close()
        flash("不能刪除自己的帳號")
        return redirect(url_for("admin.admin_sessions"))

    _delete_player_and_records(db, target["id"])
    log_activity(
        db, session["user_id"], session["username"], "admin_delete_user",
        detail=target["username"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已刪除玩家「{target['username']}」及其所有紀錄")
    return redirect(url_for("admin.admin_sessions"))


@admin_bp.route("/admin/logs")
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


@admin_bp.route("/admin/logs/clear", methods=["POST"])
@admin_required
def admin_clear_logs():
    db = get_db()
    db.execute("DELETE FROM activity_log")
    db.commit()
    db.close()
    flash("系統紀錄已清空")
    return redirect(url_for("admin.admin_logs"))


@admin_bp.route("/admin/feedback")
@admin_required
def admin_feedback():
    db = get_db()
    rows = db.execute("SELECT * FROM feedback ORDER BY id DESC").fetchall()
    db.close()
    return render_template(
        "admin_feedback.html", feedback_list=rows, status_labels=FEEDBACK_STATUS_LABELS,
        statuses=FEEDBACK_STATUSES, active_tab="feedback",
    )


@admin_bp.route("/admin/feedback/<int:feedback_id>/status", methods=["POST"])
@admin_required
def admin_update_feedback_status(feedback_id):
    status = request.form.get("status", "")
    if status not in FEEDBACK_STATUSES:
        flash("無效的處理狀態")
        return redirect(url_for("admin.admin_feedback"))

    db = get_db()
    db.execute(
        "UPDATE feedback SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, feedback_id),
    )
    db.commit()
    db.close()
    flash("已更新處理狀態")
    return redirect(url_for("admin.admin_feedback"))


@admin_bp.route("/admin/countries/<int:country_id>", methods=["POST"])
@admin_required
def admin_update_country(country_id):
    name = request.form.get("name", "").strip()
    element = request.form.get("element", "").strip()
    description = request.form.get("description", "").strip()

    if not name or not element:
        flash("國家名稱與屬性不可以是空的")
        return redirect(url_for("admin.admin"))

    bonuses = {}
    for field in STAT_FIELDS:
        raw = request.form.get(field, "0").strip()
        try:
            bonuses[field] = int(raw)
        except ValueError:
            flash(f"{field} 必須是整數")
            return redirect(url_for("admin.admin"))

    db = get_db()

    role_ids = {}
    for role in GOVERNMENT_ROLES:
        raw = request.form.get(role["column"], "").strip()
        if not raw:
            role_ids[role["column"]] = None
            continue
        try:
            char_id = int(raw)
        except ValueError:
            flash(f"{role['label']}的人選不正確")
            db.close()
            return redirect(url_for("admin.admin"))
        owner = db.execute(
            "SELECT id FROM characters WHERE id = ? AND country_id = ?", (char_id, country_id)
        ).fetchone()
        if owner is None:
            flash(f"{role['label']}必須是這個國家的角色")
            db.close()
            return redirect(url_for("admin.admin"))
        role_ids[role["column"]] = char_id

    try:
        db.execute(
            """UPDATE countries SET
                 name = ?, element = ?, description = ?,
                 hp_bonus = ?, mp_bonus = ?, str_bonus = ?,
                 def_bonus = ?, agi_bonus = ?, luk_bonus = ?,
                 king_character_id = ?, advisor_character_id = ?, general_character_id = ?
               WHERE id = ?""",
            (
                name, element, description,
                bonuses["hp_bonus"], bonuses["mp_bonus"], bonuses["str_bonus"],
                bonuses["def_bonus"], bonuses["agi_bonus"], bonuses["luk_bonus"],
                role_ids["king_character_id"], role_ids["advisor_character_id"],
                role_ids["general_character_id"],
                country_id,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        flash("這個國家名稱已經被使用了")
        db.close()
        return redirect(url_for("admin.admin"))
    db.close()

    flash(f"已更新「{name}」")
    return redirect(url_for("admin.admin"))


@admin_bp.route("/admin/settings")
@admin_required
def admin_settings():
    db = get_db()
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    hunting_grounds = db.execute("SELECT * FROM hunting_grounds ORDER BY min_level").fetchall()
    db.close()
    return render_template(
        "admin_settings.html",
        settings=settings, hunting_grounds=hunting_grounds, active_tab="settings", level_cap=LEVEL_CAP,
        game_layout_blocks=GAME_LAYOUT_BLOCKS,
        current_action_block_order=_sanitized_action_block_order(settings["action_block_order"]),
    )


@admin_bp.route("/admin/settings/game", methods=["POST"])
@admin_required
def admin_update_game_settings():
    try:
        turn_wait_seconds = int(request.form.get("turn_wait_seconds", ""))
        exp_base = int(request.form.get("exp_base", ""))
        exp_growth_novice_percent = float(request.form.get("exp_growth_novice_percent", ""))
        exp_growth_tier2_percent = float(request.form.get("exp_growth_tier2_percent", ""))
        exp_growth_tier3_percent = float(request.form.get("exp_growth_tier3_percent", ""))
        exp_growth_tier4_percent = float(request.form.get("exp_growth_tier4_percent", ""))
        rebirth_stat_bonus_percent = float(request.form.get("rebirth_stat_bonus_percent", ""))
        sell_back_percent = float(request.form.get("sell_back_percent", ""))
        guardian_encounter_percent = float(request.form.get("guardian_encounter_percent", ""))
        same_bracket_encounter_percent = float(request.form.get("same_bracket_encounter_percent", ""))
        hidden_taiji_trigger_percent = float(request.form.get("hidden_taiji_trigger_percent", ""))
        hidden_wuji_trigger_percent = float(request.form.get("hidden_wuji_trigger_percent", ""))
        hidden_taiji_drop_percent = float(request.form.get("hidden_taiji_drop_percent", ""))
        hidden_wuji_drop_percent = float(request.form.get("hidden_wuji_drop_percent", ""))
        guardian_exp_multiplier = float(request.form.get("guardian_exp_multiplier", ""))
        boss_exp_multiplier = float(request.form.get("boss_exp_multiplier", ""))
        boss_set_drop_percent = float(request.form.get("boss_set_drop_percent", ""))
        potion_drop_percent = float(request.form.get("potion_drop_percent", ""))
        shop_tax_percent = float(request.form.get("shop_tax_percent", ""))
        heal_cost_per_point = float(request.form.get("heal_cost_per_point", ""))
        town_defense_level = int(request.form.get("town_defense_level", ""))
        fortress_defense_level = int(request.form.get("fortress_defense_level", ""))
        stat_reroll_cost = int(request.form.get("stat_reroll_cost", ""))
        war_town_weekday = int(request.form.get("war_town_weekday", ""))
        war_town_start_time = request.form.get("war_town_start_time", "").strip()
        war_town_end_time = request.form.get("war_town_end_time", "").strip()
        war_fortress_weekday = int(request.form.get("war_fortress_weekday", ""))
        war_fortress_start_time = request.form.get("war_fortress_start_time", "").strip()
        war_fortress_end_time = request.form.get("war_fortress_end_time", "").strip()
        king_weekly_income_percent = int(request.form.get("king_weekly_income_percent", ""))
        official_weekly_income_percent = int(request.form.get("official_weekly_income_percent", ""))
        king_war_defense_bonus_percent = int(request.form.get("king_war_defense_bonus_percent", ""))
        office_challenge_aura_bonus_percent = int(request.form.get("office_challenge_aura_bonus_percent", ""))
        morale_buff_cost = int(request.form.get("morale_buff_cost", ""))
        morale_buff_bonus_percent = int(request.form.get("morale_buff_bonus_percent", ""))
        siege_attack_cost = int(request.form.get("siege_attack_cost", ""))
        siege_attack_reduction_percent = int(request.form.get("siege_attack_reduction_percent", ""))
        siege_attack_reduction_floor_percent = int(request.form.get("siege_attack_reduction_floor_percent", ""))
        siege_attack_cooldown_seconds = int(request.form.get("siege_attack_cooldown_seconds", ""))
        defense_repair_cost_per_percent = int(request.form.get("defense_repair_cost_per_percent", ""))
        tournament_registration_fee = int(request.form.get("tournament_registration_fee", ""))
        tournament_treasury_cut_percent = int(request.form.get("tournament_treasury_cut_percent", ""))
        tournament_registration_deadline_weekday = int(
            request.form.get("tournament_registration_deadline_weekday", "")
        )
        tournament_registration_deadline_time = request.form.get(
            "tournament_registration_deadline_time", ""
        ).strip()
        tournament_start_weekday = int(request.form.get("tournament_start_weekday", ""))
        tournament_start_time = request.form.get("tournament_start_time", "").strip()
        avatar_change_base_cost = int(request.form.get("avatar_change_base_cost", ""))
    except ValueError:
        flash("設定值格式不正確")
        return redirect(url_for("admin.admin_settings"))

    if turn_wait_seconds < 0 or exp_base < 1:
        flash("設定值必須是正數")
        return redirect(url_for("admin.admin_settings"))

    if min(exp_growth_novice_percent, exp_growth_tier2_percent,
           exp_growth_tier3_percent, exp_growth_tier4_percent) < 0:
        flash("各階段成長率不可為負數")
        return redirect(url_for("admin.admin_settings"))

    if rebirth_stat_bonus_percent < 0:
        flash("轉生加成不可為負數")
        return redirect(url_for("admin.admin_settings"))

    if sell_back_percent < 0 or sell_back_percent > 100:
        flash("裝備回收比例必須介於 0 到 100 之間")
        return redirect(url_for("admin.admin_settings"))

    if (
        guardian_encounter_percent < 0 or guardian_encounter_percent > 100
        or guardian_exp_multiplier < 1 or boss_exp_multiplier < 1
    ):
        flash("守衛怪遭遇機率須介於 0 到 100，經驗倍率須大於等於 1")
        return redirect(url_for("admin.admin_settings"))

    if boss_set_drop_percent < 0 or boss_set_drop_percent > 100:
        flash("魔王套裝掉落機率須介於 0 到 100 之間")
        return redirect(url_for("admin.admin_settings"))

    if potion_drop_percent < 0 or potion_drop_percent > 100:
        flash("藥水掉落機率須介於 0 到 100 之間")
        return redirect(url_for("admin.admin_settings"))

    if same_bracket_encounter_percent < 0 or same_bracket_encounter_percent > 100:
        flash("同等級怪物遭遇機率須介於 0 到 100 之間")
        return redirect(url_for("admin.admin_settings"))

    if min(
        hidden_taiji_trigger_percent, hidden_wuji_trigger_percent,
        hidden_taiji_drop_percent, hidden_wuji_drop_percent,
    ) < 0 or max(
        hidden_taiji_trigger_percent, hidden_wuji_trigger_percent,
        hidden_taiji_drop_percent, hidden_wuji_drop_percent,
    ) > 100:
        flash("秘境的觸發與掉落機率必須介於 0 到 100 之間")
        return redirect(url_for("admin.admin_settings"))

    if shop_tax_percent < 0 or shop_tax_percent > 100 or heal_cost_per_point < 0:
        flash("商店稅率須介於 0 到 100，回復站費率不可為負數")
        return redirect(url_for("admin.admin_settings"))

    if town_defense_level < 1 or fortress_defense_level < town_defense_level:
        flash("城鎮防衛等級須大於等於 1，且要塞防衛等級須大於等於城鎮防衛等級")
        return redirect(url_for("admin.admin_settings"))

    if stat_reroll_cost < 0:
        flash("屬性重洗費用不可為負數")
        return redirect(url_for("admin.admin_settings"))

    if war_town_weekday < 1 or war_town_weekday > 7 or war_fortress_weekday < 1 or war_fortress_weekday > 7:
        flash("國戰時段的星期必須介於 1（週一）到 7（週日）之間")
        return redirect(url_for("admin.admin_settings"))

    if not (
        _valid_war_time(war_town_start_time) and _valid_war_time(war_town_end_time)
        and _valid_war_time(war_fortress_start_time) and _valid_war_time(war_fortress_end_time)
    ):
        flash("國戰時段的時間格式須為 HH:MM（24 小時制）")
        return redirect(url_for("admin.admin_settings"))

    if war_town_start_time >= war_town_end_time:
        flash("城鎮國戰開始時間必須早於結束時間")
        return redirect(url_for("admin.admin_settings"))

    if war_fortress_start_time >= war_fortress_end_time:
        flash("要塞國戰開始時間必須早於結束時間")
        return redirect(url_for("admin.admin_settings"))

    if min(
        king_weekly_income_percent, official_weekly_income_percent,
        king_war_defense_bonus_percent, office_challenge_aura_bonus_percent,
    ) < 0:
        flash("國王／官職相關的分潤與加成數值不可為負數")
        return redirect(url_for("admin.admin_settings"))

    if min(
        morale_buff_cost, morale_buff_bonus_percent, siege_attack_cost,
        siege_attack_reduction_percent, siege_attack_reduction_floor_percent,
        siege_attack_cooldown_seconds, defense_repair_cost_per_percent,
    ) < 0:
        flash("國庫花費相關的費用與加成數值不可為負數")
        return redirect(url_for("admin.admin_settings"))

    if tournament_registration_fee < 0 or tournament_treasury_cut_percent < 0:
        flash("天下武道大會的報名費與國庫抽成不可為負數")
        return redirect(url_for("admin.admin_settings"))

    if tournament_treasury_cut_percent > 100:
        flash("天下武道大會的國庫抽成必須介於 0 到 100 之間")
        return redirect(url_for("admin.admin_settings"))

    if not (
        1 <= tournament_registration_deadline_weekday <= 7 and 1 <= tournament_start_weekday <= 7
    ):
        flash("天下武道大會的星期必須介於 1（週一）到 7（週日）之間")
        return redirect(url_for("admin.admin_settings"))

    if not (
        _valid_war_time(tournament_registration_deadline_time) and _valid_war_time(tournament_start_time)
    ):
        flash("天下武道大會的時間格式須為 HH:MM（24 小時制）")
        return redirect(url_for("admin.admin_settings"))

    if avatar_change_base_cost < 0:
        flash("頭像更換基礎費用不可為負數")
        return redirect(url_for("admin.admin_settings"))

    db = get_db()
    db.execute(
        """UPDATE game_settings
           SET turn_wait_seconds = ?, exp_base = ?, exp_growth_novice_percent = ?,
               exp_growth_tier2_percent = ?, exp_growth_tier3_percent = ?, exp_growth_tier4_percent = ?,
               rebirth_stat_bonus_percent = ?, sell_back_percent = ?,
               guardian_encounter_percent = ?, guardian_exp_multiplier = ?,
               boss_exp_multiplier = ?, boss_set_drop_percent = ?, shop_tax_percent = ?,
               heal_cost_per_point = ?, town_defense_level = ?, fortress_defense_level = ?,
               stat_reroll_cost = ?,
               war_town_weekday = ?, war_town_start_time = ?, war_town_end_time = ?,
               war_fortress_weekday = ?, war_fortress_start_time = ?, war_fortress_end_time = ?,
               king_weekly_income_percent = ?, official_weekly_income_percent = ?,
               king_war_defense_bonus_percent = ?, office_challenge_aura_bonus_percent = ?,
               morale_buff_cost = ?, morale_buff_bonus_percent = ?,
               siege_attack_cost = ?, siege_attack_reduction_percent = ?,
               siege_attack_reduction_floor_percent = ?, siege_attack_cooldown_seconds = ?,
               defense_repair_cost_per_percent = ?,
               tournament_registration_fee = ?, tournament_treasury_cut_percent = ?,
               tournament_registration_deadline_weekday = ?, tournament_registration_deadline_time = ?,
               tournament_start_weekday = ?, tournament_start_time = ?,
               hidden_taiji_trigger_percent = ?, hidden_wuji_trigger_percent = ?,
               hidden_taiji_drop_percent = ?, hidden_wuji_drop_percent = ?,
               avatar_change_base_cost = ?, same_bracket_encounter_percent = ?,
               potion_drop_percent = ?
           WHERE id = 1""",
        (
            turn_wait_seconds, exp_base, exp_growth_novice_percent,
            exp_growth_tier2_percent, exp_growth_tier3_percent, exp_growth_tier4_percent,
            rebirth_stat_bonus_percent, sell_back_percent,
            guardian_encounter_percent, guardian_exp_multiplier,
            boss_exp_multiplier, boss_set_drop_percent, shop_tax_percent,
            heal_cost_per_point, town_defense_level, fortress_defense_level,
            stat_reroll_cost,
            war_town_weekday, war_town_start_time, war_town_end_time,
            war_fortress_weekday, war_fortress_start_time, war_fortress_end_time,
            king_weekly_income_percent, official_weekly_income_percent,
            king_war_defense_bonus_percent, office_challenge_aura_bonus_percent,
            morale_buff_cost, morale_buff_bonus_percent,
            siege_attack_cost, siege_attack_reduction_percent,
            siege_attack_reduction_floor_percent, siege_attack_cooldown_seconds,
            defense_repair_cost_per_percent,
            tournament_registration_fee, tournament_treasury_cut_percent,
            tournament_registration_deadline_weekday, tournament_registration_deadline_time,
            tournament_start_weekday, tournament_start_time,
            hidden_taiji_trigger_percent, hidden_wuji_trigger_percent,
            hidden_taiji_drop_percent, hidden_wuji_drop_percent,
            avatar_change_base_cost, same_bracket_encounter_percent,
            potion_drop_percent,
        ),
    )
    db.commit()
    db.close()

    flash("已更新遊戲設定")
    return redirect(url_for("admin.admin_settings"))


@admin_bp.route("/admin/layout/update", methods=["POST"])
@admin_required
def admin_update_layout():
    # Never trust the client's drag-reorder result as-is -- sanitize the same
    # way the read path does, so a malicious/buggy submission can't corrupt
    # the stored order into garbage or drop a block category permanently.
    order = _sanitized_action_block_order(request.form.get("order", ""))

    db = get_db()
    db.execute(
        "UPDATE game_settings SET action_block_order = ? WHERE id = 1",
        (",".join(order),),
    )
    db.commit()
    db.close()

    flash("已更新遊戲畫面排版")
    return redirect(url_for("admin.admin_settings"))


@admin_bp.route("/admin/settings/hunting/<int:ground_id>", methods=["POST"])
@admin_required
def admin_update_hunting_ground(ground_id):
    name = request.form.get("name", "").strip()
    if not name:
        flash("打怪場名稱不可以是空的")
        return redirect(url_for("admin.admin_settings"))

    try:
        min_level = int(request.form.get("min_level", ""))
        max_level = int(request.form.get("max_level", ""))
    except ValueError:
        flash("打怪場數值格式不正確")
        return redirect(url_for("admin.admin_settings"))

    if min_level < 1 or max_level < min_level:
        flash("打怪場等級區間不合理")
        return redirect(url_for("admin.admin_settings"))

    db = get_db()
    db.execute(
        """UPDATE hunting_grounds
           SET name = ?, min_level = ?, max_level = ?
           WHERE id = ?""",
        (name, min_level, max_level, ground_id),
    )
    db.commit()
    db.close()

    flash(f"已更新「{name}」")
    return redirect(url_for("admin.admin_settings"))


# --- Image management (monster art + built-in avatar picker) --------------
# No DB schema involved: an override is just a PNG file dropped under a
# static/*/custom/ subfolder, checked with os.path.exists at request time
# (see web_helpers.avatar_url / monster_image_url) -- consistent with this
# app's existing no-cache, raw-sqlite3, low-traffic simplicity. Defaults
# (the shipped .svg files) are never touched or overwritten, only shadowed.
#
# Two monster-shaped opponents outside the monsters table also carry an
# image_key (see game_data/stats.py's defense_tower_stats / _bandit_lord_stats)
# and must be included in the allow-list / admin UI even though no monsters
# table row names them.
_EXTRA_MONSTER_IMAGE_KEYS = {
    "defender": "城鎮／要塞守軍（防禦塔）",
    "bandit_lord": "山賊領主（中立地塊守衛）",
}


def _monster_image_catalog():
    """Returns an ordered list of dicts, one per known monster image_key
    (deduped from DEFAULT_MONSTERS + HIDDEN_MONSTERS, plus the two
    non-monsters-table keys above), each with the monster name(s) that use
    it for admin readability."""
    names_by_key = {}
    for m in DEFAULT_MONSTERS + HIDDEN_MONSTERS:
        names_by_key.setdefault(m["image_key"], []).append(m["name"])

    catalog = [
        {"key": key, "names": names} for key, names in sorted(names_by_key.items())
    ]
    for key, label in _EXTRA_MONSTER_IMAGE_KEYS.items():
        catalog.append({"key": key, "names": [label]})
    return catalog


def _known_monster_image_keys():
    return {m["image_key"] for m in DEFAULT_MONSTERS + HIDDEN_MONSTERS} | set(_EXTRA_MONSTER_IMAGE_KEYS)


def _monster_custom_dir():
    return os.path.join(current_app.static_folder, "monsters", "custom")


def _avatar_custom_dir():
    return os.path.join(current_app.static_folder, "avatars", "built_in", "custom")


def _favicon_custom_dir():
    return os.path.join(current_app.static_folder, "favicon", "custom")


@admin_bp.route("/admin/images")
@admin_required
def admin_images():
    monster_dir = _monster_custom_dir()
    monster_items = []
    for entry in _monster_image_catalog():
        key = entry["key"]
        names = entry["names"]
        if len(names) > 4:
            names_display = "、".join(names[:3]) + f"…（共 {len(names)} 隻）"
        else:
            names_display = "、".join(names)
        monster_items.append({
            "key": key,
            "names_display": names_display,
            "has_override": os.path.isfile(os.path.join(monster_dir, f"{key}.png")),
            "preview_url": monster_image_url(key),
        })

    avatar_dir = _avatar_custom_dir()
    avatar_items = []
    for entry in BUILT_IN_AVATARS:
        key = entry["key"]
        avatar_items.append({
            "key": key,
            "label": entry["label"],
            "has_override": os.path.isfile(os.path.join(avatar_dir, f"{key}.png")),
            "preview_url": avatar_url(key, None),
        })

    return render_template(
        "admin_images.html", monster_items=monster_items, avatar_items=avatar_items, active_tab="images",
        has_favicon=os.path.isfile(os.path.join(_favicon_custom_dir(), "favicon.png")),
    )


@admin_bp.route("/admin/images/favicon", methods=["POST"])
@admin_required
def admin_upload_favicon():
    upload = request.files.get("image_file")
    if upload is None or not upload.filename:
        flash("請選擇一個圖片檔案")
        return redirect(url_for("admin.admin_images"))

    png_bytes, error = _process_square_image_upload(upload, CUSTOM_AVATAR_MAX_BYTES, CUSTOM_AVATAR_DIMENSION)
    if error:
        flash(error)
        return redirect(url_for("admin.admin_images"))

    target_dir = _favicon_custom_dir()
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, "favicon.png"), "wb") as f:
        f.write(png_bytes)

    flash("已更新網頁標籤圖示")
    return redirect(url_for("admin.admin_images"))


@admin_bp.route("/admin/images/favicon/reset", methods=["POST"])
@admin_required
def admin_reset_favicon():
    path = os.path.join(_favicon_custom_dir(), "favicon.png")
    if os.path.isfile(path):
        os.remove(path)
    flash("已移除自訂網頁標籤圖示，回復瀏覽器預設圖示")
    return redirect(url_for("admin.admin_images"))


@admin_bp.route("/admin/images/monster/<image_key>", methods=["POST"])
@admin_required
def admin_upload_monster_image(image_key):
    # image_key becomes part of a filesystem path below -- validated against
    # the known allow-list rather than sanitized, so there's no path-
    # traversal surface at all (reject anything unknown outright).
    if image_key not in _known_monster_image_keys():
        flash("無效的怪物圖片代碼")
        return redirect(url_for("admin.admin_images"))

    upload = request.files.get("image_file")
    if upload is None or not upload.filename:
        flash("請選擇一個圖片檔案")
        return redirect(url_for("admin.admin_images"))

    png_bytes, error = _process_square_image_upload(upload, CUSTOM_AVATAR_MAX_BYTES, CUSTOM_AVATAR_DIMENSION)
    if error:
        flash(error)
        return redirect(url_for("admin.admin_images"))

    target_dir = _monster_custom_dir()
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, f"{image_key}.png"), "wb") as f:
        f.write(png_bytes)

    flash(f"已更新怪物圖片「{image_key}」")
    return redirect(url_for("admin.admin_images"))


@admin_bp.route("/admin/images/monster/<image_key>/reset", methods=["POST"])
@admin_required
def admin_reset_monster_image(image_key):
    if image_key not in _known_monster_image_keys():
        flash("無效的怪物圖片代碼")
        return redirect(url_for("admin.admin_images"))

    path = os.path.join(_monster_custom_dir(), f"{image_key}.png")
    if os.path.isfile(path):
        os.remove(path)
    flash(f"已將怪物圖片「{image_key}」重置為預設")
    return redirect(url_for("admin.admin_images"))


@admin_bp.route("/admin/images/monster/reset_all", methods=["POST"])
@admin_required
def admin_reset_all_monster_images():
    target_dir = _monster_custom_dir()
    if os.path.isdir(target_dir):
        for name in os.listdir(target_dir):
            path = os.path.join(target_dir, name)
            if os.path.isfile(path):
                os.remove(path)
    flash("已將所有怪物圖片重置為預設")
    return redirect(url_for("admin.admin_images"))


@admin_bp.route("/admin/images/avatar/<avatar_key>", methods=["POST"])
@admin_required
def admin_upload_avatar_image(avatar_key):
    # Same allow-list-only rule as the monster upload above -- avatar_key
    # becomes part of a filesystem path.
    if avatar_key not in BUILT_IN_AVATAR_KEYS:
        flash("無效的頭像代碼")
        return redirect(url_for("admin.admin_images"))

    upload = request.files.get("image_file")
    if upload is None or not upload.filename:
        flash("請選擇一個圖片檔案")
        return redirect(url_for("admin.admin_images"))

    png_bytes, error = _process_square_image_upload(upload, CUSTOM_AVATAR_MAX_BYTES, CUSTOM_AVATAR_DIMENSION)
    if error:
        flash(error)
        return redirect(url_for("admin.admin_images"))

    target_dir = _avatar_custom_dir()
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, f"{avatar_key}.png"), "wb") as f:
        f.write(png_bytes)

    flash(f"已更新頭像圖片「{avatar_key}」")
    return redirect(url_for("admin.admin_images"))


@admin_bp.route("/admin/images/avatar/<avatar_key>/reset", methods=["POST"])
@admin_required
def admin_reset_avatar_image(avatar_key):
    if avatar_key not in BUILT_IN_AVATAR_KEYS:
        flash("無效的頭像代碼")
        return redirect(url_for("admin.admin_images"))

    path = os.path.join(_avatar_custom_dir(), f"{avatar_key}.png")
    if os.path.isfile(path):
        os.remove(path)
    flash(f"已將頭像圖片「{avatar_key}」重置為預設")
    return redirect(url_for("admin.admin_images"))


@admin_bp.route("/admin/images/avatar/reset_all", methods=["POST"])
@admin_required
def admin_reset_all_avatar_images():
    target_dir = _avatar_custom_dir()
    if os.path.isdir(target_dir):
        for name in os.listdir(target_dir):
            path = os.path.join(target_dir, name)
            if os.path.isfile(path):
                os.remove(path)
    flash("已將所有頭像圖片重置為預設")
    return redirect(url_for("admin.admin_images"))


# ---- 背景圖片／背景音樂 (game/battle/tournament/shop backgrounds + one
# global BGM track) -- same admin-override-on-disk convention as the monster
# and avatar routes above, just with a non-square image pipeline (see
# web_helpers._process_background_image_upload) and a trusted-after-
# validation byte pass-through for audio (see web_helpers._sniff_audio_format).

@admin_bp.route("/admin/backgrounds")
@admin_required
def admin_backgrounds():
    background_items = []
    for key, label in BACKGROUND_KEYS.items():
        background_items.append({
            "key": key,
            "label": label,
            "has_override": os.path.isfile(os.path.join(BACKGROUND_CUSTOM_DIR, f"{key}.jpg")),
            "preview_url": background_url(key),
        })

    bgm_path = None
    for ext in BGM_EXTENSIONS:
        candidate = os.path.join(BGM_DIR, f"bgm.{ext}")
        if os.path.isfile(candidate):
            bgm_path = candidate
            break

    return render_template(
        "admin_backgrounds.html", background_items=background_items,
        bgm_preview_url=bgm_url(), has_bgm=bgm_path is not None, active_tab="backgrounds",
    )


@admin_bp.route("/admin/backgrounds/image/<key>", methods=["POST"])
@admin_required
def admin_upload_background_image(key):
    # key becomes part of a filesystem path below -- validated against the
    # fixed allow-list rather than sanitized, same rule as the monster/avatar
    # upload routes above.
    if key not in BACKGROUND_KEYS:
        flash("無效的背景圖片代碼")
        return redirect(url_for("admin.admin_backgrounds"))

    upload = request.files.get("image_file")
    if upload is None or not upload.filename:
        flash("請選擇一個圖片檔案")
        return redirect(url_for("admin.admin_backgrounds"))

    jpeg_bytes, error = _process_background_image_upload(
        upload, BACKGROUND_MAX_BYTES, BACKGROUND_MAX_WIDTH, BACKGROUND_MAX_HEIGHT,
    )
    if error:
        flash(error)
        return redirect(url_for("admin.admin_backgrounds"))

    os.makedirs(BACKGROUND_CUSTOM_DIR, exist_ok=True)
    with open(os.path.join(BACKGROUND_CUSTOM_DIR, f"{key}.jpg"), "wb") as f:
        f.write(jpeg_bytes)

    flash(f"已更新背景圖片「{BACKGROUND_KEYS[key]}」")
    return redirect(url_for("admin.admin_backgrounds"))


@admin_bp.route("/admin/backgrounds/image/<key>/reset", methods=["POST"])
@admin_required
def admin_reset_background_image(key):
    if key not in BACKGROUND_KEYS:
        flash("無效的背景圖片代碼")
        return redirect(url_for("admin.admin_backgrounds"))

    path = os.path.join(BACKGROUND_CUSTOM_DIR, f"{key}.jpg")
    if os.path.isfile(path):
        os.remove(path)
    flash(f"已將背景圖片「{BACKGROUND_KEYS[key]}」重置為預設")
    return redirect(url_for("admin.admin_backgrounds"))


@admin_bp.route("/admin/backgrounds/image/reset_all", methods=["POST"])
@admin_required
def admin_reset_all_background_images():
    if os.path.isdir(BACKGROUND_CUSTOM_DIR):
        for name in os.listdir(BACKGROUND_CUSTOM_DIR):
            path = os.path.join(BACKGROUND_CUSTOM_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
    flash("已將所有背景圖片重置為預設")
    return redirect(url_for("admin.admin_backgrounds"))


@admin_bp.route("/admin/backgrounds/music", methods=["POST"])
@admin_required
def admin_upload_bgm():
    upload = request.files.get("audio_file")
    if upload is None or not upload.filename:
        flash("請選擇一個音樂檔案")
        return redirect(url_for("admin.admin_backgrounds"))

    ext = upload.filename.rsplit(".", 1)[-1].lower() if "." in upload.filename else ""
    if ext not in BGM_EXTENSIONS:
        flash(BGM_FORMAT_HINT)
        return redirect(url_for("admin.admin_backgrounds"))

    data = upload.read()
    if not data:
        flash("請選擇一個音樂檔案")
        return redirect(url_for("admin.admin_backgrounds"))
    if len(data) > BGM_MAX_BYTES:
        flash(f"檔案大小超過 {BGM_MAX_BYTES // (1024 * 1024)}MB 上限")
        return redirect(url_for("admin.admin_backgrounds"))

    # Extension AND magic-number sniff must agree -- neither alone is
    # trusted (see web_helpers._sniff_audio_format for why there's no
    # decode/re-encode step to fall back on here).
    if _sniff_audio_format(data) != ext:
        flash(f"檔案內容與副檔名不符。{BGM_FORMAT_HINT}")
        return redirect(url_for("admin.admin_backgrounds"))

    os.makedirs(BGM_DIR, exist_ok=True)
    for other_ext in BGM_EXTENSIONS:
        if other_ext != ext:
            other_path = os.path.join(BGM_DIR, f"bgm.{other_ext}")
            if os.path.isfile(other_path):
                os.remove(other_path)
    with open(os.path.join(BGM_DIR, f"bgm.{ext}"), "wb") as f:
        f.write(data)

    flash("已更新背景音樂")
    return redirect(url_for("admin.admin_backgrounds"))


@admin_bp.route("/admin/backgrounds/music/reset", methods=["POST"])
@admin_required
def admin_reset_bgm():
    for ext in BGM_EXTENSIONS:
        path = os.path.join(BGM_DIR, f"bgm.{ext}")
        if os.path.isfile(path):
            os.remove(path)
    flash("已移除背景音樂")
    return redirect(url_for("admin.admin_backgrounds"))
