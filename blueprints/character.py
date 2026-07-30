"""Character creation, the character sheet, promotions, rebirth and skills."""
import io
import os
import sqlite3
import uuid

from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from PIL import Image, UnidentifiedImageError

from db import get_db, log_activity, LEVEL_CAP, ELEMENT_OVERCOMES
from web_helpers import (
    login_required, admin_required, character_required, _cooldown_remaining_seconds,
    _validate_character_name, _in_any_war_window, _morale_buff_active, avatar_url,
)
from game_data.constants import (
    SHOP_TYPE_LABELS, SLOT_LABELS, EQUIP_SLOT_COLUMNS, LEVEL_STAT_GROWTH, STAT_LABELS,
    RENAME_MAX_CHARACTER_NAME_LEN,
)
from game_data.avatars import (
    BUILT_IN_AVATARS, BUILT_IN_AVATAR_KEYS, CUSTOM_AVATAR_MAX_BYTES, CUSTOM_AVATAR_ALLOWED_FORMATS,
    CUSTOM_AVATAR_DIMENSION, CUSTOM_AVATAR_FORMAT_HINT, CUSTOM_AVATAR_UPLOAD_DIR,
)
from game_data.jobs import (
    TIER1_JOBS, TIER2_JOBS, TIER2_CHILDREN_BY_FAMILY, TIER3_JOBS, TIER3_CHILDREN_BY_PARENT,
    JOB_TIER_LABELS, _resolve_tier4_job, _process_job_progression,
)
from game_data.skills import (
    SKILL_CATALOG, _learnable_skills, _usable_skill_keys, _ordered_usable_skills,
    _learned_skill_keys,
)
from game_data.equipment import (
    _active_set_summaries, _own_element_bonus_summary, _fetch_equipped_items,
    _hidden_set_summaries,
)
from game_data.stats import STAT_FLOOR_COLUMNS, character_final_stats, _current_hp_mp
from game_data.progression import (
    exp_required_for_level, LEVEL_UP_POINT_VALUE, _roll_level_up_stat_points,
)
from game_data.combat import ELEMENT_OVERCOME_BONUS, ELEMENT_OVERCOME_PENALTY, derived_combat_stats

character_bp = Blueprint("character", __name__)


def _create_character(db, country_id, character_name):
    """Insert a new character row. Returns (country_row, error_message)."""
    country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (country_id,)
    ).fetchone()
    if country is None:
        return None, "請選擇一個有效的國家"

    fortress = db.execute(
        "SELECT id FROM map_tiles WHERE country_id = ? AND tile_type = 'fortress'",
        (country["id"],),
    ).fetchone()
    if fortress is None:
        # Fortress may have been conquered away -- fall back to any tile the
        # country still owns (a town). If it owns nothing at all, it has no
        # territory left to spawn characters on.
        fortress = db.execute(
            "SELECT id FROM map_tiles WHERE country_id = ? LIMIT 1", (country["id"],)
        ).fetchone()
    if fortress is None:
        return None, f"{country['name']}目前沒有任何據點，暫時無法在此建立角色"

    try:
        db.execute(
            """INSERT INTO characters (user_id, country_id, current_tile_id, name, tutorial_seen)
               VALUES (?, ?, ?, ?, 0)""",
            (session["user_id"], country["id"], fortress["id"], character_name),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return None, "這個角色名稱剛好被用掉了，請重新整理再試一次"

    log_activity(
        db, session["user_id"], session["username"], "character_create",
        detail=f"{character_name}（{country['name']}）", ip_address=request.remote_addr,
    )
    db.commit()
    return country, None


@character_bp.route("/character/create", methods=["GET", "POST"])
@login_required
def character_create():
    db = get_db()
    existing = db.execute(
        "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    if existing:
        db.close()
        return redirect(url_for("game.game"))

    character_name = session.get("pending_character_name")
    if not character_name:
        character_name = f"{session['username']}的角色"
        suffix = 2
        while db.execute(
            "SELECT id FROM characters WHERE lower(name) = lower(?)", (character_name,)
        ).fetchone():
            character_name = f"{session['username']}的角色{suffix}"
            suffix += 1

    # A player who picked their country on the landing page before registering
    # already made this choice -- don't ask again, just create the character.
    pending_country_id = session.get("pending_country_id")
    if pending_country_id is not None:
        country, error = _create_character(db, pending_country_id, character_name)
        session.pop("pending_country_id", None)
        if error:
            db.close()
            flash(error)
            return redirect(url_for("character.character_create"))
        db.close()
        session.pop("pending_character_name", None)
        session["character_name"] = character_name
        flash(f"歡迎來到{country['name']}，{character_name}！")
        return redirect(url_for("game.game"))

    if request.method == "GET":
        countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
        db.close()
        return render_template("character_create.html", countries=countries, character_name=character_name)

    country, error = _create_character(db, request.form.get("country_id", ""), character_name)
    if error:
        db.close()
        flash(error)
        return redirect(url_for("character.character_create"))

    db.close()
    session.pop("pending_character_name", None)
    session["character_name"] = character_name
    flash(f"歡迎來到{country['name']}，{character_name}！")
    return redirect(url_for("game.game"))


@character_bp.route("/character/tutorial")
@character_required
def character_tutorial():
    """New-character onboarding: an accept/skip prompt, then (if accepted) a
    client-side-only slideshow -- no per-step server round trip, only the
    final "skip" or "開始遊戲" action ever POSTs. Gated by game.game() which
    redirects here whenever the character's own tutorial_seen is still 0;
    this view itself doesn't need to re-check that (harmless to visit again
    even after it's been marked seen)."""
    return render_template("tutorial.html")


@character_bp.route("/character/tutorial/dismiss", methods=["POST"])
@character_required
def character_tutorial_dismiss():
    db = get_db()
    db.execute("UPDATE characters SET tutorial_seen = 1 WHERE user_id = ?", (session["user_id"],))
    log_activity(
        db, session["user_id"], session["username"], "tutorial_dismissed",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    return redirect(url_for("game.game"))


@character_bp.route("/character")
@character_required
def character_page():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp,
                  characters.next_action_at, characters.name AS character_name,
                  characters.current_hp, characters.current_mp, characters.job_class,
                  characters.job_tier, characters.rebirth_count, characters.currency,
                  characters.stat_floor_hp,
                  characters.stat_floor_mp, characters.stat_floor_str, characters.stat_floor_def,
                  characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.battles_count,
                  characters.wins_count, characters.pvp_battles_count, characters.pvp_wins_count,
                  characters.equipped_skill_1, characters.equipped_skill_2,
                  characters.rename_count, characters.contribution, characters.avatar_change_count,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    mastery_names = [
        row["job_name"] for row in db.execute(
            "SELECT job_name FROM job_masteries WHERE character_id = ?", (character["character_id"],)
        )
    ]
    learned_keys = _learned_skill_keys(db, character["character_id"])
    equipped_items = _fetch_equipped_items(db, character)

    equipped_slots = []
    for shop_type, label in SLOT_LABELS.items():
        item_id = character[EQUIP_SLOT_COLUMNS[shop_type]]
        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone() if item_id else None
        equipped_slots.append({"slot": shop_type, "label": label, "item": item})

    inventory_rows = db.execute(
        """SELECT items.*, inventory.quantity AS quantity
           FROM inventory JOIN items ON items.id = inventory.item_id
           WHERE inventory.character_id = ?
           ORDER BY items.shop_type, items.price""",
        (character["character_id"],),
    ).fetchall()
    inventory_items = {shop_type: [] for shop_type in SHOP_TYPE_LABELS}
    for row in inventory_rows:
        inventory_items[row["shop_type"]].append(row)

    skill_book_rows = db.execute(
        "SELECT skill_key, quantity FROM character_skill_books WHERE character_id = ? AND quantity > 0",
        (character["character_id"],),
    ).fetchall()

    db.close()

    # King war-defense bonus: character["character_id"] is the viewer's own
    # id (character["id"] is shadowed to the country's id by the bare
    # countries.* join above, same gotcha documented in blueprints/game.py).
    is_reigning_king = character["character_id"] == character["king_character_id"]
    king_war_defense_bonus = is_reigning_king and _in_any_war_window(settings)
    morale_buff_active = _morale_buff_active(character)
    stats = character_final_stats(character, equipped_items, settings, king_war_defense_bonus, morale_buff_active)
    current_hp, current_mp = _current_hp_mp(character, stats)
    exp_needed = (
        exp_required_for_level(character["level"], settings, force_one=session.get("is_admin", False))
        if character["level"] < LEVEL_CAP else None
    )
    can_promote_tier1 = character["job_tier"] == 0 and character["level"] >= 10
    can_promote_tier2 = character["job_tier"] == 1 and character["level"] >= 30
    can_promote_tier3 = character["job_tier"] == 2 and character["level"] >= 70
    mastery_count = len(mastery_names)
    can_promote_tier4 = (
        character["job_tier"] == 3 and character["level"] >= 120
        and character["rebirth_count"] >= 3 and mastery_count >= 3
    )
    can_rebirth = character["job_tier"] == 3 and character["level"] >= 120 and not can_promote_tier4
    mastered = set(mastery_names)
    # A tier2 job whose both tier3 children are already mastered is excluded,
    # so a future rebirth into this family never dead-ends at tier3 with zero
    # choices left; a tier3 job that's already mastered is excluded outright
    # (rebirthing into it again would waste the life re-mastering nothing new).
    tier2_choices = [
        name for name in TIER2_CHILDREN_BY_FAMILY.get(character["job_class"], [])
        if not set(TIER3_CHILDREN_BY_PARENT.get(name, [])) <= mastered
    ]
    tier3_choices = [
        name for name in TIER3_CHILDREN_BY_PARENT.get(character["job_class"], [])
        if name not in mastered
    ]
    win_rate = (
        round(character["wins_count"] / character["battles_count"] * 100, 1)
        if character["battles_count"] else None
    )
    pvp_win_rate = (
        round(character["pvp_wins_count"] / character["pvp_battles_count"] * 100, 1)
        if character["pvp_battles_count"] else None
    )
    overcomes = ELEMENT_OVERCOMES.get(character["element"])
    overcome_by = next(
        (k for k, v in ELEMENT_OVERCOMES.items() if v == character["element"]), None
    )
    active_sets = _active_set_summaries(equipped_items)
    own_element_bonus = _own_element_bonus_summary(equipped_items, character["element"])
    hidden_sets = _hidden_set_summaries(equipped_items)
    learnable_skills = _learnable_skills(character, learned_keys)
    usable_keys = _usable_skill_keys(character, learned_keys)
    learned_locked_skills = sorted(
        (SKILL_CATALOG[k] for k in learned_keys - usable_keys if k in SKILL_CATALOG),
        key=lambda s: (s["job_tier"], s["slot"]),
    )

    # 4 distinct skill groups shown on this page: (1) equipped (<=2, actually
    # fires in combat -- see _equipped_combat_skills), (2) the "skill library"
    # (learned + currently lineage-usable but not equipped), (3) learnable
    # (existing currency path), (4) learned_locked_skills (existing, above).
    equipped_key_slots = [("1", character["equipped_skill_1"]), ("2", character["equipped_skill_2"])]
    equipped_skills = [
        {"slot": slot, "skill": SKILL_CATALOG[key]}
        for slot, key in equipped_key_slots
        if key and key in SKILL_CATALOG
    ]
    equipped_keys = {key for _, key in equipped_key_slots if key}
    skill_library = [
        s for s in _ordered_usable_skills(usable_keys) if s["key"] not in equipped_keys
    ]
    held_skill_books = sorted(
        (
            {"skill": SKILL_CATALOG[row["skill_key"]], "quantity": row["quantity"]}
            for row in skill_book_rows if row["skill_key"] in SKILL_CATALOG
        ),
        key=lambda entry: entry["skill"]["name"],
    )

    return render_template(
        "character.html",
        character=character,
        stats=stats,
        combat_stats=derived_combat_stats(stats),
        win_rate=win_rate,
        pvp_win_rate=pvp_win_rate,
        overcomes=overcomes,
        overcome_by=overcome_by,
        element_overcome_bonus=round((ELEMENT_OVERCOME_BONUS - 1) * 100),
        element_overcome_penalty=round((1 - ELEMENT_OVERCOME_PENALTY) * 100),
        current_hp=current_hp,
        current_mp=current_mp,
        level_cap=LEVEL_CAP,
        exp_needed=exp_needed,
        equipped_items=equipped_items,
        equipped_slots=equipped_slots,
        active_sets=active_sets,
        own_element_bonus=own_element_bonus,
        hidden_sets=hidden_sets,
        inventory_items=inventory_items,
        shop_type_labels=SHOP_TYPE_LABELS,
        cooldown_seconds=_cooldown_remaining_seconds(character["next_action_at"]),
        job_tier_label=JOB_TIER_LABELS.get(character["job_tier"], ""),
        mastery_names=mastery_names,
        can_promote_tier1=can_promote_tier1,
        can_promote_tier2=can_promote_tier2,
        can_promote_tier3=can_promote_tier3,
        can_promote_tier4=can_promote_tier4,
        can_rebirth=can_rebirth,
        tier1_jobs=TIER1_JOBS,
        tier2_choices=tier2_choices,
        tier2_job_info=TIER2_JOBS,
        tier3_choices=tier3_choices,
        tier3_job_info=TIER3_JOBS,
        learnable_skills=learnable_skills,
        equipped_skills=equipped_skills,
        skill_library=skill_library,
        held_skill_books=held_skill_books,
        learned_locked_skills=learned_locked_skills,
        stat_labels=STAT_LABELS,
        stat_reroll_cost=settings["stat_reroll_cost"],
        next_rename_cost=(character["rename_count"] + 1) * 1000,
        rename_max_len=RENAME_MAX_CHARACTER_NAME_LEN,
        built_in_avatars=BUILT_IN_AVATARS,
        current_avatar_key=session.get("avatar_key"),
        next_avatar_change_cost=(character["avatar_change_count"] + 1) * settings["avatar_change_base_cost"],
        custom_avatar_format_hint=CUSTOM_AVATAR_FORMAT_HINT,
    )


def _escape_like_pattern(value):
    """Escapes %, _ and the escape character itself so a player's search text
    is matched literally by LIKE rather than as a wildcard pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@character_bp.route("/character/lookup")
@character_required
def character_lookup():
    query = request.args.get("q", "").strip()
    results = []
    if query:
        db = get_db()
        results = db.execute(
            """SELECT characters.id AS character_id, characters.name AS character_name,
                      characters.level, characters.job_class, characters.rebirth_count,
                      countries.name AS country_name
               FROM characters JOIN countries ON countries.id = characters.country_id
               WHERE lower(characters.name) LIKE lower(?) ESCAPE '\\'
               ORDER BY characters.name
               LIMIT 50""",
            (f"%{_escape_like_pattern(query)}%",),
        ).fetchall()
        db.close()
    return render_template("character_lookup.html", query=query, results=results)


@character_bp.route("/character/view/<int:character_id>")
@character_required
def character_view(character_id):
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.user_id, characters.level,
                  characters.name AS character_name, characters.job_class, characters.job_tier,
                  characters.rebirth_count, characters.battles_count, characters.wins_count,
                  characters.pvp_battles_count, characters.pvp_wins_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.id = ?""",
        (character_id,),
    ).fetchone()
    if character is None:
        db.close()
        flash("找不到這個角色")
        return redirect(url_for("character.character_lookup"))

    profile_user = db.execute(
        "SELECT avatar_key, avatar_custom_filename FROM users WHERE id = ?", (character["user_id"],)
    ).fetchone()
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)

    equipped_slots = []
    for shop_type, label in SLOT_LABELS.items():
        item_id = character[EQUIP_SLOT_COLUMNS[shop_type]]
        item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone() if item_id else None
        equipped_slots.append({"slot": shop_type, "label": label, "item": item})

    db.close()

    # Same king-war-defense / morale-buff gotcha as character_page: this is a
    # read-only view of ANOTHER character, so re-derive both flags for the
    # VIEWED character (not the caller) exactly the same way character_page
    # does for the logged-in player -- otherwise the shown stats would be
    # wrong for anyone looking up a reigning king mid-war or a buffed country.
    is_reigning_king = character["character_id"] == character["king_character_id"]
    king_war_defense_bonus = is_reigning_king and _in_any_war_window(settings)
    morale_buff_active = _morale_buff_active(character)
    stats = character_final_stats(character, equipped_items, settings, king_war_defense_bonus, morale_buff_active)

    win_rate = (
        round(character["wins_count"] / character["battles_count"] * 100, 1)
        if character["battles_count"] else None
    )
    pvp_win_rate = (
        round(character["pvp_wins_count"] / character["pvp_battles_count"] * 100, 1)
        if character["pvp_battles_count"] else None
    )

    return render_template(
        "character_view.html",
        character=character,
        stats=stats,
        win_rate=win_rate,
        pvp_win_rate=pvp_win_rate,
        job_tier_label=JOB_TIER_LABELS.get(character["job_tier"], ""),
        equipped_slots=equipped_slots,
        profile_avatar_url=avatar_url(profile_user["avatar_key"], profile_user["avatar_custom_filename"]),
    )


def _mastered_job_names(db, character_id):
    return {
        row["job_name"] for row in db.execute(
            "SELECT job_name FROM job_masteries WHERE character_id = ?", (character_id,)
        )
    }


def _character_for_promotion(db):
    return db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.job_tier,
                  characters.job_class, characters.rebirth_count, characters.currency,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.country_id AS char_country_id, characters.name AS character_name,
                  characters.rename_count, characters.avatar_change_count, characters.user_id,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


def _snapshot_stat_floor(character, equipped_items, settings):
    """Pre-promotion stats (already folded against any existing floor via
    character_final_stats' own max()) become the new floor -- so a promotion
    can never make any stat go down, chained across multiple promotions."""
    stats = character_final_stats(character, equipped_items, settings)
    return {STAT_FLOOR_COLUMNS[key]: value for key, value in stats.items()}


def _tile_counts(db):
    return {
        row["country_id"]: row["c"]
        for row in db.execute(
            "SELECT country_id, COUNT(*) AS c FROM map_tiles WHERE country_id IS NOT NULL GROUP BY country_id"
        ).fetchall()
    }


@character_bp.route("/character/rejoin_country", methods=["GET", "POST"])
@character_required
def character_rejoin_country():
    db = get_db()
    character = _character_for_promotion(db)
    tile_counts = _tile_counts(db)

    if tile_counts.get(character["char_country_id"], 0) >= 1:
        db.close()
        flash("你的國家還沒有滅國，無法加入其他國家")
        return redirect(url_for("character.character_page"))

    if request.method == "GET":
        countries = db.execute(
            "SELECT * FROM countries WHERE id != ? ORDER BY id", (character["char_country_id"],)
        ).fetchall()
        db.close()
        surviving_countries = [c for c in countries if tile_counts.get(c["id"], 0) >= 1]
        return render_template("rejoin_country.html", countries=surviving_countries)

    new_country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (request.form.get("country_id", ""),)
    ).fetchone()
    if (
        new_country is None
        or new_country["id"] == character["char_country_id"]
        or tile_counts.get(new_country["id"], 0) < 1
    ):
        db.close()
        flash("請選擇一個有效的國家")
        return redirect(url_for("character.character_page"))

    fortress = db.execute(
        "SELECT id FROM map_tiles WHERE country_id = ? AND tile_type = 'fortress'",
        (new_country["id"],),
    ).fetchone()
    if fortress is None:
        fortress = db.execute(
            "SELECT id FROM map_tiles WHERE country_id = ? LIMIT 1", (new_country["id"],)
        ).fetchone()

    old_country_name = character["name"]
    db.execute(
        "UPDATE characters SET country_id = ?, current_tile_id = ?, contribution = 0 WHERE id = ?",
        (new_country["id"], fortress["id"], character["character_id"]),
    )
    db.execute("DELETE FROM garrisons WHERE character_id = ?", (character["character_id"],))
    log_activity(
        db, session["user_id"], session["username"], "rejoin_country",
        detail=f"{old_country_name} → {new_country['name']}", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"你的國家已滅亡，成功加入「{new_country['name']}」！")
    return redirect(url_for("game.game"))


@character_bp.route("/character/reroll_stats", methods=["POST"])
@character_required
def character_reroll_stats():
    db = get_db()
    character = _character_for_promotion(db)
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    if character["level"] <= 1:
        db.close()
        flash("尚未升過級，沒有可重洗的屬性點")
        return redirect(url_for("character.character_page"))

    cost = settings["stat_reroll_cost"]
    if character["currency"] < cost:
        db.close()
        flash(f"諸神幣不足，重洗屬性需要 {cost} 諸神幣")
        return redirect(url_for("character.character_page"))

    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)

    new_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    for _ in range(character["level"] - 1):
        for stat, points in _roll_level_up_stat_points(character["job_class"], character["job_tier"]).items():
            new_gain[stat] += points * LEVEL_UP_POINT_VALUE[stat]

    db.execute(
        """UPDATE characters SET currency = currency - ?,
               level_bonus_hp = ?, level_bonus_mp = ?, level_bonus_str = ?,
               level_bonus_def = ?, level_bonus_agi = ?, level_bonus_luk = ?
           WHERE id = ?""",
        (
            cost, new_gain["hp"], new_gain["mp"], new_gain["str"],
            new_gain["def"], new_gain["agi"], new_gain["luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "reroll_stats",
        detail=f"花費{cost}諸神幣重洗屬性點", ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["currency"] -= cost
    updated["level_bonus_hp"] = new_gain["hp"]
    updated["level_bonus_mp"] = new_gain["mp"]
    updated["level_bonus_str"] = new_gain["str"]
    updated["level_bonus_def"] = new_gain["def"]
    updated["level_bonus_agi"] = new_gain["agi"]
    updated["level_bonus_luk"] = new_gain["luk"]
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title="屬性重洗完成！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@character_bp.route("/character/rename", methods=["POST"])
@character_required
def character_rename():
    db = get_db()
    character = _character_for_promotion(db)

    if character["job_tier"] != 4:
        db.close()
        flash("需先四轉才能改名")
        return redirect(url_for("character.character_page"))

    new_name = request.form.get("new_name", "").strip()
    old_name = character["character_name"]

    if new_name.lower() == old_name.lower():
        db.close()
        flash("名稱沒有變更")
        return redirect(url_for("character.character_page"))

    name_error = _validate_character_name(db, new_name, session["username"], max_len=RENAME_MAX_CHARACTER_NAME_LEN)
    if name_error:
        db.close()
        flash(name_error)
        return redirect(url_for("character.character_page"))

    cost = (character["rename_count"] + 1) * 1000
    if character["currency"] < cost:
        db.close()
        flash(f"諸神幣不足，改名需要 {cost} 諸神幣")
        return redirect(url_for("character.character_page"))

    try:
        db.execute(
            "UPDATE characters SET name = ?, currency = currency - ?, rename_count = rename_count + 1 WHERE id = ?",
            (new_name, cost, character["character_id"]),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        db.close()
        flash("這個名稱剛好被用掉了，請重新整理再試一次")
        return redirect(url_for("character.character_page"))

    log_activity(
        db, session["user_id"], session["username"], "rename_character",
        detail=f"{old_name} → {new_name}", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    session["character_name"] = new_name
    flash(f"角色名稱已改為「{new_name}」")
    return redirect(url_for("character.character_page"))


def _process_avatar_upload(file_storage):
    """Validates and re-encodes an uploaded avatar into a fixed-size square
    PNG under a random filename -- the uploaded bytes/extension are NEVER
    trusted or stored as-is, only what Pillow can actually decode as a real
    image (and re-encode from scratch) ends up on disk. Returns
    (new_filename, error_message), exactly one of which is None."""
    data = file_storage.read()
    if not data:
        return None, "請選擇一個圖片檔案"
    if len(data) > CUSTOM_AVATAR_MAX_BYTES:
        return None, f"檔案大小超過 {CUSTOM_AVATAR_MAX_BYTES // (1024 * 1024)}MB 上限"

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
    img = img.resize((CUSTOM_AVATAR_DIMENSION, CUSTOM_AVATAR_DIMENSION), Image.LANCZOS)

    os.makedirs(CUSTOM_AVATAR_UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.png"
    img.save(os.path.join(CUSTOM_AVATAR_UPLOAD_DIR, filename), format="PNG")
    return filename, None


@character_bp.route("/character/change_avatar", methods=["POST"])
@character_required
def character_change_avatar():
    db = get_db()
    character = _character_for_promotion(db)

    if character["job_tier"] != 4:
        db.close()
        flash("需先四轉才能更換頭像")
        return redirect(url_for("character.character_page"))

    settings = db.execute("SELECT avatar_change_base_cost FROM game_settings WHERE id = 1").fetchone()
    cost = (character["avatar_change_count"] + 1) * settings["avatar_change_base_cost"]
    if character["currency"] < cost:
        db.close()
        flash(f"諸神幣不足，更換頭像需要 {cost} 諸神幣")
        return redirect(url_for("character.character_page"))

    upload = request.files.get("avatar_file")
    avatar_key_choice = request.form.get("avatar_key", "")

    new_avatar_key = None
    new_custom_filename = None
    if upload is not None and upload.filename:
        new_custom_filename, error = _process_avatar_upload(upload)
        if error:
            db.close()
            flash(error)
            return redirect(url_for("character.character_page"))
    elif avatar_key_choice in BUILT_IN_AVATAR_KEYS:
        new_avatar_key = avatar_key_choice
    else:
        db.close()
        flash("請選擇一個內建頭像，或上傳一張圖片")
        return redirect(url_for("character.character_page"))

    old_user = db.execute(
        "SELECT avatar_custom_filename FROM users WHERE id = ?", (character["user_id"],)
    ).fetchone()
    old_custom_filename = old_user["avatar_custom_filename"]

    if new_custom_filename:
        db.execute(
            "UPDATE users SET avatar_custom_filename = ? WHERE id = ?",
            (new_custom_filename, character["user_id"]),
        )
    else:
        db.execute(
            "UPDATE users SET avatar_key = ?, avatar_custom_filename = NULL WHERE id = ?",
            (new_avatar_key, character["user_id"]),
        )
    db.execute(
        "UPDATE characters SET currency = currency - ?, avatar_change_count = avatar_change_count + 1 WHERE id = ?",
        (cost, character["character_id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "change_avatar",
        detail="上傳自訂頭像" if new_custom_filename else new_avatar_key,
        ip_address=request.remote_addr,
    )
    db.commit()

    # An old uploaded file no longer referenced by this account is deleted
    # only after the DB commit succeeds, so a mid-request failure never
    # leaves the account pointing at a file that's already been removed.
    if old_custom_filename and old_custom_filename != new_custom_filename:
        old_path = os.path.join(CUSTOM_AVATAR_UPLOAD_DIR, old_custom_filename)
        if os.path.exists(old_path):
            os.remove(old_path)

    updated_user = db.execute(
        "SELECT avatar_key, avatar_custom_filename FROM users WHERE id = ?", (character["user_id"],)
    ).fetchone()
    db.close()

    session["avatar_key"] = updated_user["avatar_key"]
    session["avatar_custom_filename"] = updated_user["avatar_custom_filename"]
    flash("頭像已更新！")
    return redirect(url_for("character.character_page"))


@character_bp.route("/character/promote/tier1", methods=["POST"])
@character_required
def character_promote_tier1():
    db = get_db()
    character = _character_for_promotion(db)

    job_name = request.form.get("job_name", "")
    if character["job_tier"] != 0 or character["level"] < 10:
        db.close()
        flash("目前還不能一轉")
        return redirect(url_for("character.character_page"))
    if job_name not in TIER1_JOBS:
        db.close()
        flash("請選擇一個有效的職業")
        return redirect(url_for("character.character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 1,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier1",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 1
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"一轉成功，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@character_bp.route("/character/promote/tier2", methods=["POST"])
@character_required
def character_promote_tier2():
    db = get_db()
    character = _character_for_promotion(db)

    job_name = request.form.get("job_name", "")
    mastered = _mastered_job_names(db, character["character_id"])
    valid_choices = [
        name for name in TIER2_CHILDREN_BY_FAMILY.get(character["job_class"], [])
        if not set(TIER3_CHILDREN_BY_PARENT.get(name, [])) <= mastered
    ]
    if character["job_tier"] != 1 or character["level"] < 30:
        db.close()
        flash("目前還不能二轉")
        return redirect(url_for("character.character_page"))
    if job_name not in valid_choices:
        db.close()
        flash("請選擇一個有效的職業")
        return redirect(url_for("character.character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 2,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier2",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 2
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"二轉成功，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@character_bp.route("/character/promote/tier3", methods=["POST"])
@character_required
def character_promote_tier3():
    db = get_db()
    character = _character_for_promotion(db)

    job_name = request.form.get("job_name", "")
    mastered = _mastered_job_names(db, character["character_id"])
    valid_choices = [
        name for name in TIER3_CHILDREN_BY_PARENT.get(character["job_class"], [])
        if name not in mastered
    ]
    if character["job_tier"] != 2 or character["level"] < 70:
        db.close()
        flash("目前還不能三轉")
        return redirect(url_for("character.character_page"))
    if job_name not in valid_choices:
        db.close()
        flash("請選擇一個有效的職業")
        return redirect(url_for("character.character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 3,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier3",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 3
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"三轉成功，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@character_bp.route("/character/promote/tier4", methods=["POST"])
@character_required
def character_promote_tier4():
    db = get_db()
    character = _character_for_promotion(db)

    mastery_count = db.execute(
        "SELECT COUNT(*) AS c FROM job_masteries WHERE character_id = ?",
        (character["character_id"],),
    ).fetchone()["c"]
    if not (
        character["job_tier"] == 3 and character["level"] >= 120
        and character["rebirth_count"] >= 3 and mastery_count >= 3
    ):
        db.close()
        flash("目前還不能四轉")
        return redirect(url_for("character.character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)
    floor = _snapshot_stat_floor(character, equipped_items, settings)
    job_name = _resolve_tier4_job(db, character["character_id"])

    db.execute(
        """UPDATE characters SET job_class = ?, job_tier = 4,
               stat_floor_hp = ?, stat_floor_mp = ?, stat_floor_str = ?,
               stat_floor_def = ?, stat_floor_agi = ?, stat_floor_luk = ?
           WHERE id = ?""",
        (
            job_name, floor["stat_floor_hp"], floor["stat_floor_mp"], floor["stat_floor_str"],
            floor["stat_floor_def"], floor["stat_floor_agi"], floor["stat_floor_luk"],
            character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "promote_tier4",
        detail=job_name, ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["job_class"] = job_name
    updated["job_tier"] = 4
    updated.update(floor)
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title=f"四轉！你已臻至巔峰，成為「{job_name}」！",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@character_bp.route("/character/rebirth", methods=["POST"])
@character_required
def character_rebirth():
    db = get_db()
    character = _character_for_promotion(db)

    if character["job_tier"] != 3 or character["level"] < 120:
        db.close()
        flash("目前還不能轉生")
        return redirect(url_for("character.character_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats_before = character_final_stats(character, equipped_items, settings)

    db.execute(
        """UPDATE characters
           SET rebirth_count = rebirth_count + 1, level = 10, exp = 0,
               job_class = '初心者', job_tier = 0,
               stat_floor_hp = NULL, stat_floor_mp = NULL, stat_floor_str = NULL,
               stat_floor_def = NULL, stat_floor_agi = NULL, stat_floor_luk = NULL
           WHERE id = ?""",
        (character["character_id"],),
    )
    log_activity(
        db, session["user_id"], session["username"], "rebirth",
        detail=f"第 {character['rebirth_count'] + 1} 次轉生", ip_address=request.remote_addr,
    )
    db.commit()

    updated = dict(character)
    updated["level"] = 10
    updated["job_class"] = "初心者"
    updated["job_tier"] = 0
    for col in STAT_FLOOR_COLUMNS.values():
        updated[col] = None
    stats_after = character_final_stats(updated, equipped_items, settings)

    db.close()
    return render_template(
        "job_change_result.html",
        title="轉生完成！等級重置為 10 級，職業回到初心者，準備踏上新的旅程",
        stats_before=stats_before,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@character_bp.route("/character/learn_skill", methods=["POST"])
@character_required
def character_learn_skill():
    db = get_db()
    character = _character_for_promotion(db)

    skill_key = request.form.get("skill_key", "")
    skill = SKILL_CATALOG.get(skill_key)
    learned_keys = _learned_skill_keys(db, character["character_id"])
    learnable_keys = {s["key"] for s in _learnable_skills(character, learned_keys)}

    if skill is None or skill_key not in learnable_keys:
        db.close()
        flash("目前無法學習這個技能")
        return redirect(url_for("character.character_page"))
    if character["currency"] < skill["learn_cost"]:
        db.close()
        flash(f"諸神幣不足，學習「{skill['name']}」需要 {skill['learn_cost']} 諸神幣")
        return redirect(url_for("character.character_page"))

    db.execute(
        "UPDATE characters SET currency = currency - ? WHERE id = ?",
        (skill["learn_cost"], character["character_id"]),
    )
    db.execute(
        "INSERT INTO character_skills (character_id, skill_key) VALUES (?, ?)",
        (character["character_id"], skill_key),
    )
    log_activity(
        db, session["user_id"], session["username"], "learn_skill",
        detail=skill["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"學會了「{skill['name']}」！")
    return redirect(url_for("character.character_page"))


@character_bp.route("/character/skill_book/use", methods=["POST"])
@character_required
def character_skill_book_use():
    db = get_db()
    character = _character_for_promotion(db)

    skill_key = request.form.get("skill_key", "")
    skill = SKILL_CATALOG.get(skill_key)
    book_row = db.execute(
        "SELECT id, quantity FROM character_skill_books WHERE character_id = ? AND skill_key = ?",
        (character["character_id"], skill_key),
    ).fetchone()
    learned_keys = _learned_skill_keys(db, character["character_id"])

    if skill is None or book_row is None or book_row["quantity"] < 1:
        db.close()
        flash("你目前沒有這本技能書")
        return redirect(url_for("character.character_page"))
    if character["job_tier"] != 4:
        db.close()
        flash("必須先四轉才能使用這本技能書")
        return redirect(url_for("character.character_page"))
    if skill_key in learned_keys:
        db.close()
        flash("你已經學會這個技能了")
        return redirect(url_for("character.character_page"))

    if book_row["quantity"] > 1:
        db.execute(
            "UPDATE character_skill_books SET quantity = quantity - 1 WHERE id = ?", (book_row["id"],)
        )
    else:
        db.execute("DELETE FROM character_skill_books WHERE id = ?", (book_row["id"],))
    db.execute(
        "INSERT INTO character_skills (character_id, skill_key) VALUES (?, ?)",
        (character["character_id"], skill_key),
    )
    log_activity(
        db, session["user_id"], session["username"], "learn_skill",
        detail=skill["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"使用技能書學會了「{skill['name']}」！")
    return redirect(url_for("character.character_page"))


@character_bp.route("/character/equip_skill", methods=["POST"])
@character_required
def character_equip_skill():
    db = get_db()
    character = _character_for_promotion(db)

    skill_key = request.form.get("skill_key", "")
    slot = request.form.get("slot", "")
    if slot not in ("1", "2"):
        db.close()
        flash("請選擇一個有效的技能欄位")
        return redirect(url_for("character.character_page"))

    learned_keys = _learned_skill_keys(db, character["character_id"])
    usable_keys = _usable_skill_keys(character, learned_keys)
    if skill_key not in usable_keys:
        db.close()
        flash("這個技能目前無法配置")
        return redirect(url_for("character.character_page"))

    current = db.execute(
        "SELECT equipped_skill_1, equipped_skill_2 FROM characters WHERE id = ?",
        (character["character_id"],),
    ).fetchone()

    # Never let both slots hold the same skill -- clear the other slot first
    # if it already has this skill_key, then (over)write the target slot.
    if slot == "1":
        if current["equipped_skill_2"] == skill_key:
            db.execute(
                "UPDATE characters SET equipped_skill_2 = NULL WHERE id = ?", (character["character_id"],)
            )
        db.execute(
            "UPDATE characters SET equipped_skill_1 = ? WHERE id = ?", (skill_key, character["character_id"])
        )
    else:
        if current["equipped_skill_1"] == skill_key:
            db.execute(
                "UPDATE characters SET equipped_skill_1 = NULL WHERE id = ?", (character["character_id"],)
            )
        db.execute(
            "UPDATE characters SET equipped_skill_2 = ? WHERE id = ?", (skill_key, character["character_id"])
        )
    db.commit()
    db.close()

    skill = SKILL_CATALOG.get(skill_key)
    flash(f"已將「{skill['name'] if skill else skill_key}」配置為技能{slot}")
    return redirect(url_for("character.character_page"))


@character_bp.route("/character/unequip_skill", methods=["POST"])
@character_required
def character_unequip_skill():
    db = get_db()
    character = _character_for_promotion(db)

    slot = request.form.get("slot", "")
    if slot not in ("1", "2"):
        db.close()
        flash("請選擇一個有效的技能欄位")
        return redirect(url_for("character.character_page"))

    if slot == "1":
        db.execute(
            "UPDATE characters SET equipped_skill_1 = NULL WHERE id = ?", (character["character_id"],)
        )
    else:
        db.execute(
            "UPDATE characters SET equipped_skill_2 = NULL WHERE id = ?", (character["character_id"],)
        )
    db.commit()
    db.close()

    flash("已卸下技能")
    return redirect(url_for("character.character_page"))


@character_bp.route("/character/debug/set_level", methods=["POST"])
@admin_required
@character_required
def character_debug_set_level():
    """Admin-only shortcut so the developer's own account can jump straight
    to any level to eyeball stat growth, without grinding real EXP."""
    try:
        level = int(request.form.get("level", ""))
    except ValueError:
        flash("等級格式不正確")
        return redirect(url_for("character.character_page"))

    level = max(1, min(level, LEVEL_CAP))
    # Debug-jumping a level can't fairly replay the random per-level roll for
    # every level skipped, so it approximates with the old flat formula --
    # good enough for eyeballing roughly how strong that level should feel.
    level_growth = max(0, level - 1)
    db = get_db()
    character = db.execute(
        "SELECT id AS character_id, level, job_class, job_tier, rebirth_count FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()

    # Route through the same mastery-recording check real hunts use, so this
    # shortcut can actually be used to test that flow. 四轉 is no longer an
    # automatic side effect here -- it's now an explicit player action.
    _process_job_progression(db, character, character["level"], level)

    db.execute(
        """UPDATE characters SET level = ?, exp = 0,
               level_bonus_hp = ?, level_bonus_mp = ?, level_bonus_str = ?,
               level_bonus_def = ?, level_bonus_agi = ?, level_bonus_luk = ?
           WHERE id = ?""",
        (
            level,
            LEVEL_STAT_GROWTH["hp"] * level_growth, LEVEL_STAT_GROWTH["mp"] * level_growth,
            LEVEL_STAT_GROWTH["str"] * level_growth, LEVEL_STAT_GROWTH["def"] * level_growth,
            LEVEL_STAT_GROWTH["agi"] * level_growth, LEVEL_STAT_GROWTH["luk"] * level_growth,
            character["character_id"],
        ),
    )
    db.commit()
    db.close()

    flash(f"（除錯）等級已設為 {level}")
    return redirect(url_for("character.character_page"))


@character_bp.route("/character/debug/set_rebirth", methods=["POST"])
@admin_required
@character_required
def character_debug_set_rebirth():
    """Admin-only shortcut to directly set the rebirth count, so the stacking
    stat bonus can be checked without actually grinding out 3 full lifetimes."""
    try:
        rebirth_count = int(request.form.get("rebirth_count", ""))
    except ValueError:
        flash("轉生次數格式不正確")
        return redirect(url_for("character.character_page"))

    rebirth_count = max(0, rebirth_count)
    db = get_db()
    db.execute(
        "UPDATE characters SET rebirth_count = ? WHERE user_id = ?",
        (rebirth_count, session["user_id"]),
    )
    db.commit()
    db.close()

    flash(f"（除錯）轉生次數已設為 {rebirth_count}")
    return redirect(url_for("character.character_page"))


@character_bp.route("/character/debug/reset", methods=["POST"])
@admin_required
@character_required
def character_debug_reset():
    """Admin-only one-click reset back to a brand new 初心者, so the whole
    job/rebirth/four-zhuan flow can be tested again from scratch. Currency,
    country, equipment and inventory are left untouched (same policy as a
    normal rebirth) -- this only wipes progression state."""
    db = get_db()
    character = db.execute(
        "SELECT id AS character_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()

    db.execute("DELETE FROM job_masteries WHERE character_id = ?", (character["character_id"],))
    db.execute(
        """UPDATE characters
           SET level = 1, exp = 0, rebirth_count = 0,
               job_class = '初心者', job_tier = 0,
               current_hp = NULL, current_mp = NULL, pending_boss_monster_id = NULL,
               stat_floor_hp = NULL, stat_floor_mp = NULL, stat_floor_str = NULL,
               stat_floor_def = NULL, stat_floor_agi = NULL, stat_floor_luk = NULL,
               level_bonus_hp = 0, level_bonus_mp = 0, level_bonus_str = 0,
               level_bonus_def = 0, level_bonus_agi = 0, level_bonus_luk = 0
           WHERE id = ?""",
        (character["character_id"],),
    )
    log_activity(
        db, session["user_id"], session["username"], "debug_reset",
        detail="重置為初心者 1 級", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash("（除錯）角色已重置為 1 級初心者，精通紀錄已清空")
    return redirect(url_for("character.character_page"))
