"""Admin console: sessions, logs, country roles and game settings."""
import sqlite3
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

from db import get_db, LEVEL_CAP
from web_helpers import admin_required, _parse_dt, _valid_war_time, _format_duration
from game_data.constants import STAT_FIELDS, IDLE_THRESHOLD_MINUTES, ACTION_LABELS, GOVERNMENT_ROLES

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
        "SELECT username, is_admin, is_online, last_login_at, last_seen_at "
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
        guardian_exp_multiplier = float(request.form.get("guardian_exp_multiplier", ""))
        boss_exp_multiplier = float(request.form.get("boss_exp_multiplier", ""))
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

    db = get_db()
    db.execute(
        """UPDATE game_settings
           SET turn_wait_seconds = ?, exp_base = ?, exp_growth_novice_percent = ?,
               exp_growth_tier2_percent = ?, exp_growth_tier3_percent = ?, exp_growth_tier4_percent = ?,
               rebirth_stat_bonus_percent = ?, sell_back_percent = ?,
               guardian_encounter_percent = ?, guardian_exp_multiplier = ?,
               boss_exp_multiplier = ?, shop_tax_percent = ?,
               heal_cost_per_point = ?, town_defense_level = ?, fortress_defense_level = ?,
               stat_reroll_cost = ?,
               war_town_weekday = ?, war_town_start_time = ?, war_town_end_time = ?,
               war_fortress_weekday = ?, war_fortress_start_time = ?, war_fortress_end_time = ?,
               king_weekly_income_percent = ?, official_weekly_income_percent = ?,
               king_war_defense_bonus_percent = ?, office_challenge_aura_bonus_percent = ?,
               morale_buff_cost = ?, morale_buff_bonus_percent = ?,
               siege_attack_cost = ?, siege_attack_reduction_percent = ?,
               siege_attack_reduction_floor_percent = ?, siege_attack_cooldown_seconds = ?,
               defense_repair_cost_per_percent = ?
           WHERE id = 1""",
        (
            turn_wait_seconds, exp_base, exp_growth_novice_percent,
            exp_growth_tier2_percent, exp_growth_tier3_percent, exp_growth_tier4_percent,
            rebirth_stat_bonus_percent, sell_back_percent,
            guardian_encounter_percent, guardian_exp_multiplier,
            boss_exp_multiplier, shop_tax_percent,
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
        ),
    )
    db.commit()
    db.close()

    flash("已更新遊戲設定")
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
