"""The main game loop: map movement, hunting, conquest, shop, bank and equipment."""
import random
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from db import get_db, log_activity, LEVEL_CAP
from map_layout import axial_to_pixel, hex_corners, axial_distance
from web_helpers import (
    character_required, _next_action_at, _cooldown_remaining_seconds, _in_war_window,
    _war_window_label, _war_window_kind_for_tile_type, _format_duration, _add_to_inventory,
    _remove_from_inventory,
)
from game_data.constants import (
    SHOP_TYPE_LABELS, SLOT_LABELS, EQUIP_SLOT_COLUMNS, GOVERNMENT_ROLES, tile_display_name,
    HEX_SIZE, ELEMENT_COLORS, NEUTRAL_TILE_COLOR, MOUNTAIN_TILE_COLOR, STAT_LABELS,
)
from game_data.jobs import _process_job_progression
from game_data.skills import TIER4_SLOT2_SKILL_KEYS, SKILL_CATALOG, _character_usable_skills
from game_data.equipment import _fetch_equipped_items
from game_data.stats import (
    character_final_stats, defense_tower_stats, _current_hp_mp, _bandit_lord_stats,
)
from game_data.progression import exp_required_for_level, LEVEL_UP_POINT_VALUE, apply_exp
from game_data.combat import gold_luk_bonus_pct, run_battle

game_bp = Blueprint("game", __name__)


def _relocate_or_clear_garrison(db, character_id, new_tile):
    """If character_id currently has a garrison row, relocate it to new_tile
    (refreshing stationed_at so a genuine relocation counts as a fresh "most
    recent" stationing) when new_tile is a valid own-country fortress/town;
    otherwise the destination isn't a legal garrison location, so the garrison
    row is deleted entirely."""
    garrison = db.execute(
        "SELECT id FROM garrisons WHERE character_id = ?", (character_id,)
    ).fetchone()
    if garrison is None:
        return
    character = db.execute(
        "SELECT country_id FROM characters WHERE id = ?", (character_id,)
    ).fetchone()
    valid = (
        new_tile["tile_type"] in ("fortress", "town")
        and new_tile["country_id"] == character["country_id"]
    )
    if valid:
        db.execute(
            "UPDATE garrisons SET tile_id = ?, stationed_at = datetime('now') WHERE character_id = ?",
            (new_tile["id"], character_id),
        )
    else:
        db.execute("DELETE FROM garrisons WHERE character_id = ?", (character_id,))


def _render_game(**extra):
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.current_tile_id,
                  characters.currency, characters.bank_balance, characters.level, characters.exp,
                  characters.next_action_at, characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.name AS character_name,
                  characters.current_hp, characters.current_mp, characters.job_class, characters.job_tier,
                  characters.rebirth_count, characters.stat_floor_hp, characters.stat_floor_mp,
                  characters.stat_floor_str, characters.stat_floor_def, characters.stat_floor_agi,
                  characters.stat_floor_luk, characters.level_bonus_hp, characters.level_bonus_mp,
                  characters.level_bonus_str, characters.level_bonus_def, characters.level_bonus_agi,
                  characters.level_bonus_luk, characters.contribution,
                  characters.donated_today, characters.donated_today_date, countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    tiles = [
        dict(row) for row in db.execute(
            """SELECT map_tiles.id AS tile_id, map_tiles.q, map_tiles.r, map_tiles.tile_type,
                      map_tiles.name, map_tiles.country_id, map_tiles.bandit_hp,
                      countries.element, countries.name AS country_name
               FROM map_tiles LEFT JOIN countries ON countries.id = map_tiles.country_id"""
        ).fetchall()
    ]
    for t in tiles:
        t["display_name"] = tile_display_name(t["name"], t["tile_type"])
    current_tile = next(t for t in tiles if t["tile_id"] == character["current_tile_id"])
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    hunting_grounds = db.execute(
        "SELECT * FROM hunting_grounds ORDER BY min_level"
    ).fetchall()
    admin_monsters = []
    if session.get("is_admin"):
        rows = db.execute(
            """SELECT monsters.*, hunting_grounds.name AS ground_name
               FROM monsters JOIN hunting_grounds ON hunting_grounds.id = monsters.hunting_ground_id
               ORDER BY hunting_grounds.min_level, monsters.is_boss, monsters.is_guardian, monsters.level_min"""
        ).fetchall()
        for m in rows:
            if m["is_boss"]:
                level_label = "首領"
            elif m["is_guardian"]:
                level_label = "守衛怪"
            else:
                level_label = f"Lv{m['level_min']}-{m['level_max']}"
            admin_monsters.append({
                "id": m["id"], "name": m["name"], "ground_name": m["ground_name"],
                "level_label": level_label,
            })
    equipped_items = _fetch_equipped_items(db, character)

    # Garrison status: fetched off the garrisons table itself (not inferred
    # from current_tile_id) -- see game.html/point 12 for why this is
    # defensive rather than assumed.
    garrison = db.execute(
        "SELECT tile_id, stationed_at FROM garrisons WHERE character_id = ?",
        (character["character_id"],),
    ).fetchone()
    garrison_tile = None
    if garrison is not None:
        garrison_tile = next((t for t in tiles if t["tile_id"] == garrison["tile_id"]), None)
    can_station_here = (
        current_tile["tile_type"] in ("fortress", "town")
        and current_tile["country_id"] == character["id"]
    )
    own_tile_count = db.execute(
        "SELECT COUNT(*) AS c FROM map_tiles WHERE country_id = ?", (character["id"],)
    ).fetchone()["c"]
    country_destroyed = own_tile_count == 0
    pending_trade_invite_count = db.execute(
        "SELECT COUNT(*) AS c FROM trades WHERE target_character_id = ? AND status = 'pending'",
        (character["character_id"],),
    ).fetchone()["c"]
    db.close()

    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    exp_needed = (
        exp_required_for_level(character["level"], settings, force_one=session.get("is_admin", False))
        if character["level"] < LEVEL_CAP else None
    )

    cooldown_seconds = _cooldown_remaining_seconds(character["next_action_at"])

    missing = (stats["hp"] - current_hp) + (stats["mp"] - current_mp)
    recover_cost = round(missing * settings["heal_cost_per_point"])

    can_attack_tile = (
        current_tile["tile_type"] == "neutral"
        or (
            current_tile["tile_type"] in ("fortress", "town")
            and current_tile["country_id"] is not None
            and current_tile["country_id"] != character["id"]
        )
    )
    war_window_kind = _war_window_kind_for_tile_type(current_tile["tile_type"])
    if war_window_kind == "fortress":
        war_weekday, war_start_time, war_end_time = (
            settings["war_fortress_weekday"], settings["war_fortress_start_time"], settings["war_fortress_end_time"]
        )
    else:
        war_weekday, war_start_time, war_end_time = (
            settings["war_town_weekday"], settings["war_town_start_time"], settings["war_town_end_time"]
        )
    in_war_window = _in_war_window(war_weekday, war_start_time, war_end_time)
    war_window_label = _war_window_label(war_weekday, war_start_time, war_end_time)
    bandit_hp_max = None
    bandit_hp = None
    if current_tile["tile_type"] == "neutral":
        bandit_hp_max = _bandit_lord_stats(settings)["hp"]
        # Read-only view: falls back to the max when NULL rather than writing
        # the lazy-init back to the DB -- only game_conquer() ever persists it.
        bandit_hp = current_tile["bandit_hp"] if current_tile["bandit_hp"] is not None else bandit_hp_max
    job_action_available = (
        (character["job_tier"] == 0 and character["level"] >= 10)
        or (character["job_tier"] == 1 and character["level"] >= 30)
        or (character["job_tier"] == 2 and character["level"] >= 70)
        or (character["job_tier"] == 3 and character["level"] >= 120)
    )
    defense_level = (
        settings["fortress_defense_level"] if current_tile["tile_type"] == "fortress"
        else settings["town_defense_level"]
    )

    here = (current_tile["q"], current_tile["r"])
    move_targets = [
        t for t in tiles
        if t["tile_type"] != "mountain"
        and axial_distance(here, (t["q"], t["r"])) == 1
    ]

    hexes = []
    xs, ys = [], []
    for t in tiles:
        cx, cy = axial_to_pixel(t["q"], t["r"], HEX_SIZE)
        color = (
            MOUNTAIN_TILE_COLOR if t["tile_type"] == "mountain"
            else ELEMENT_COLORS.get(t["element"], NEUTRAL_TILE_COLOR)
        )
        points = " ".join(f"{px:.1f},{py:.1f}" for px, py in hex_corners(cx, cy, HEX_SIZE))
        xs += [cx - HEX_SIZE, cx + HEX_SIZE]
        ys += [cy - HEX_SIZE, cy + HEX_SIZE]
        hexes.append({
            "points": points,
            "cx": cx,
            "cy": cy,
            "color": color,
            "tile_type": t["tile_type"],
            "name": t["name"],
            "country_name": t["country_name"],
            "is_player_here": t["tile_id"] == character["current_tile_id"],
        })

    padding = HEX_SIZE
    min_x, max_x = min(xs) - padding, max(xs) + padding
    min_y, max_y = min(ys) - padding, max(ys) + padding

    # 貢獻值 donation cap display: NOTE this SELECT ends with a bare
    # countries.*, so character["id"] resolves to the COUNTRY's id (last
    # column wins in sqlite3.Row) -- the character's own id is
    # character["character_id"]. Comparing against king/advisor/general
    # character id MUST use character["character_id"], not character["id"].
    if character["character_id"] == character["king_character_id"]:
        donate_cap = DONATE_DAILY_CAP_KING
    elif character["character_id"] in (
        character["advisor_character_id"], character["general_character_id"],
    ):
        donate_cap = DONATE_DAILY_CAP_OFFICER
    else:
        donate_cap = DONATE_DAILY_CAP_DEFAULT
    today = datetime.utcnow().strftime("%Y-%m-%d")
    donated_today_display = (
        character["donated_today"] if character["donated_today_date"] == today else 0
    )

    context = dict(
        character=character,
        stats=stats,
        current_hp=current_hp,
        current_mp=current_mp,
        level_cap=LEVEL_CAP,
        exp_needed=exp_needed,
        current_tile=current_tile,
        move_targets=move_targets,
        hunting_grounds=hunting_grounds,
        admin_monsters=admin_monsters,
        cooldown_seconds=cooldown_seconds,
        recover_cost=recover_cost,
        can_attack_tile=can_attack_tile,
        in_war_window=in_war_window,
        war_window_label=war_window_label,
        defense_level=defense_level,
        bandit_hp=bandit_hp,
        bandit_hp_max=bandit_hp_max,
        job_action_available=job_action_available,
        country_destroyed=country_destroyed,
        own_treasury=character["treasury"],
        hexes=hexes,
        view_box=f"{min_x:.1f} {min_y:.1f} {max_x - min_x:.1f} {max_y - min_y:.1f}",
        garrison=garrison,
        garrison_tile=garrison_tile,
        can_station_here=can_station_here,
        donate_cap=donate_cap,
        donated_today_display=donated_today_display,
        pending_trade_invite_count=pending_trade_invite_count,
    )
    context.update(extra)
    return render_template("game.html", **context)


@game_bp.route("/game")
@character_required
def game():
    return _render_game()


@game_bp.route("/game/move", methods=["POST"])
@character_required
def game_move():
    db = get_db()
    character = db.execute(
        "SELECT id, current_tile_id, next_action_at FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game"))

    current_tile = db.execute(
        "SELECT q, r FROM map_tiles WHERE id = ?", (character["current_tile_id"],)
    ).fetchone()
    target_tile = db.execute(
        "SELECT id, q, r, tile_type, name, country_id FROM map_tiles WHERE id = ?",
        (request.form.get("tile_id", ""),),
    ).fetchone()

    valid_target = (
        target_tile is not None
        and target_tile["tile_type"] != "mountain"
        and axial_distance((current_tile["q"], current_tile["r"]), (target_tile["q"], target_tile["r"])) == 1
    )
    if not valid_target:
        db.close()
        flash("無法移動到那個地點")
        return redirect(url_for("game.game"))

    target_name = tile_display_name(target_tile["name"], target_tile["tile_type"])

    garrison = db.execute(
        "SELECT id FROM garrisons WHERE character_id = ?", (character["id"],)
    ).fetchone()
    if garrison is not None and request.form.get("confirm_garrison_move") != "1":
        db.close()
        flash(f"你目前正在駐防中，移動到「{target_name}」將會變更或解除駐防狀態，請確認是否繼續移動")
        return _render_game(
            pending_move_tile_id=target_tile["id"],
            pending_move_tile_name=target_name,
        )

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        "UPDATE characters SET current_tile_id = ?, next_action_at = ?, pending_boss_monster_id = NULL WHERE id = ?",
        (target_tile["id"], _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    if garrison is not None:
        _relocate_or_clear_garrison(db, character["id"], target_tile)
    log_activity(
        db, session["user_id"], session["username"], "move",
        detail=target_name, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"移動到了「{target_name}」")
    return redirect(url_for("game.game"))


@game_bp.route("/game/hunt", methods=["POST"])
@character_required
def game_hunt():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp, characters.next_action_at,
                  characters.current_hp, characters.current_mp, characters.currency, characters.name AS character_name,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  map_tiles.tile_type, countries.*
           FROM characters
           JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game"))

    if character["tile_type"] == "fortress":
        db.close()
        flash("要塞內沒有打怪地點，請先移動到要塞外")
        return redirect(url_for("game.game"))

    forced_monster = None
    if session.get("is_admin") and request.form.get("monster_id", ""):
        forced_monster = db.execute(
            "SELECT * FROM monsters WHERE id = ?", (request.form.get("monster_id", ""),)
        ).fetchone()

    if forced_monster is not None:
        ground = db.execute(
            "SELECT * FROM hunting_grounds WHERE id = ?", (forced_monster["hunting_ground_id"],)
        ).fetchone()
    else:
        ground = db.execute(
            "SELECT * FROM hunting_grounds WHERE id = ?", (request.form.get("ground_id", ""),)
        ).fetchone()
    if ground is None:
        db.close()
        flash("請選擇一個有效的打怪場")
        return redirect(url_for("game.game"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game.game"))

    monsters = db.execute(
        "SELECT * FROM monsters WHERE hunting_ground_id = ?", (ground["id"],)
    ).fetchall()
    boss = next((m for m in monsters if m["is_boss"]), None)

    if forced_monster is not None:
        monster = forced_monster
        is_guardian_fight = bool(monster["is_guardian"])
    else:
        regulars_in_bracket = [
            m for m in monsters
            if not m["is_boss"] and not m["is_guardian"]
            and m["level_min"] is not None and m["level_min"] <= character["level"] <= m["level_max"]
        ]
        regulars_any = [m for m in monsters if not m["is_boss"] and not m["is_guardian"]]
        guardian = next((m for m in monsters if m["is_guardian"]), None)
        regular_pool = regulars_in_bracket or regulars_any
        if not regular_pool and not guardian:
            db.close()
            flash("這個打怪場目前還沒有設定怪物")
            return redirect(url_for("game.game"))
        is_guardian_fight = bool(guardian) and random.random() * 100 < settings["guardian_encounter_percent"]
        monster = guardian if is_guardian_fight else random.choice(regular_pool)

    usable_skills = _character_usable_skills(db, character)
    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, monster,
        player_mp=current_mp, usable_skills=usable_skills,
    )

    exp_gain = 0
    currency_gain = 0
    currency_lost = 0
    new_level, new_exp = character["level"], character["exp"]
    stat_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    pending_boss_id = None
    boss_room_available = False
    skill_book_dropped = None
    if result["won"]:
        if is_guardian_fight:
            exp_multiplier = settings["guardian_exp_multiplier"]
        elif monster["is_boss"]:
            exp_multiplier = settings["boss_exp_multiplier"]
        else:
            exp_multiplier = 1.0
        exp_gain = round(monster["exp_reward"] * exp_multiplier)
        currency_gain = round(monster["currency_reward"] * (1 + gold_luk_bonus_pct(stats["luk"]) / 100))
        new_level, new_exp, stat_gain = apply_exp(
            character["level"], character["exp"], exp_gain, settings,
            force_one=session.get("is_admin", False),
            job_class=character["job_class"], job_tier=character["job_tier"],
        )
        new_currency = character["currency"] + currency_gain
        if is_guardian_fight and boss is not None and result["player_hp"] > 0:
            boss_room_available = True
            pending_boss_id = boss["id"]
        # Rare monster-drop skill book: only rolls in the top-tier ultimate
        # hunting ground, independent of the hunter's own job (a "wrong
        # job's" book still isn't wasted -- once actually learned at 四轉,
        # every learned skill becomes usable regardless of lineage).
        if ground["tier"] == "ultimate" and random.random() < 1 / 20000:
            dropped_key = random.choice(TIER4_SLOT2_SKILL_KEYS)
            dropped_skill = SKILL_CATALOG[dropped_key]
            skill_book_dropped = dropped_skill["name"]
            db.execute(
                """INSERT INTO character_skill_books (character_id, skill_key, quantity)
                   VALUES (?, ?, 1)
                   ON CONFLICT(character_id, skill_key) DO UPDATE SET quantity = quantity + 1""",
                (character["character_id"], dropped_key),
            )
            log_activity(
                db, session["user_id"], session["username"], "skill_book_drop",
                detail=dropped_skill["name"], ip_address=request.remote_addr,
            )
    elif not result["timed_out"]:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
    else:
        new_currency = character["currency"]

    _process_job_progression(db, character, character["level"], new_level)

    db.execute(
        """UPDATE characters
           SET level = ?, exp = ?, currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               pending_boss_monster_id = ?,
               level_bonus_hp = level_bonus_hp + ?, level_bonus_mp = level_bonus_mp + ?,
               level_bonus_str = level_bonus_str + ?, level_bonus_def = level_bonus_def + ?,
               level_bonus_agi = level_bonus_agi + ?, level_bonus_luk = level_bonus_luk + ?
           WHERE id = ?""",
        (
            new_level, new_exp, new_currency, result["player_hp"], result["player_mp"],
            _next_action_at(settings["turn_wait_seconds"]), 1 if result["won"] else 0,
            pending_boss_id,
            stat_gain["hp"], stat_gain["mp"], stat_gain["str"],
            stat_gain["def"], stat_gain["agi"], stat_gain["luk"],
            character["character_id"],
        ),
    )
    if result["won"]:
        outcome_detail = f"擊敗{monster['name']}，+{exp_gain} EXP +{currency_gain} 諸神幣"
    elif result["timed_out"]:
        outcome_detail = f"與{monster['name']}戰鬥回合已滿，未分勝負，沒有任何諸神幣損失"
    else:
        outcome_detail = f"敗給{monster['name']}，身上 {currency_lost} 諸神幣化為烏有"
    if is_guardian_fight:
        outcome_detail = f"[守衛怪] {outcome_detail}"
    log_activity(
        db, session["user_id"], session["username"], "hunt",
        detail=f"{ground['name']} {outcome_detail}", ip_address=request.remote_addr,
    )

    if new_level > character["level"]:
        updated = dict(character)
        updated["level"] = new_level
        for stat in ("hp", "mp", "str", "def", "agi", "luk"):
            updated[f"level_bonus_{stat}"] = character[f"level_bonus_{stat}"] + stat_gain[stat]
        stats_after = character_final_stats(updated, equipped_items, settings)
    else:
        stats_after = None

    db.commit()
    db.close()

    return render_template(
        "battle.html",
        ground=ground,
        monster=monster,
        guardian_encounter=is_guardian_fight,
        boss_room_available=boss_room_available,
        skill_book_dropped=skill_book_dropped,
        log=result["log"],
        won=result["won"],
        timed_out=result["timed_out"],
        leveled_up=new_level > character["level"],
        new_level=new_level,
        exp_gain=exp_gain,
        currency_gain=currency_gain,
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


@game_bp.route("/game/hunt/boss_room", methods=["POST"])
@character_required
def game_hunt_boss_room():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.level, characters.exp, characters.currency,
                  characters.name AS character_name, characters.pending_boss_monster_id,
                  characters.current_hp, characters.current_mp,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  countries.*
           FROM characters
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    boss = None
    ground = None
    if character["pending_boss_monster_id"] is not None:
        boss = db.execute(
            "SELECT * FROM monsters WHERE id = ? AND is_boss = 1", (character["pending_boss_monster_id"],)
        ).fetchone()
        if boss is not None:
            ground = db.execute(
                "SELECT * FROM hunting_grounds WHERE id = ?", (boss["hunting_ground_id"],)
            ).fetchone()
    if boss is None or ground is None:
        db.close()
        flash("魔王房間的挑戰機會已經沒有了")
        return redirect(url_for("game.game"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.execute(
            "UPDATE characters SET pending_boss_monster_id = NULL WHERE id = ?", (character["character_id"],)
        )
        db.commit()
        db.close()
        flash("HP 已耗盡，無法挑戰魔王，請先回到要塞回復")
        return redirect(url_for("game.game"))

    usable_skills = _character_usable_skills(db, character)
    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, boss,
        player_mp=current_mp, usable_skills=usable_skills,
    )

    exp_gain = 0
    currency_gain = 0
    currency_lost = 0
    new_level, new_exp = character["level"], character["exp"]
    stat_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    if result["won"]:
        exp_gain = round(boss["exp_reward"] * settings["boss_exp_multiplier"])
        currency_gain = round(boss["currency_reward"] * (1 + gold_luk_bonus_pct(stats["luk"]) / 100))
        new_level, new_exp, stat_gain = apply_exp(
            character["level"], character["exp"], exp_gain, settings,
            force_one=session.get("is_admin", False),
            job_class=character["job_class"], job_tier=character["job_tier"],
        )
        new_currency = character["currency"] + currency_gain
    elif not result["timed_out"]:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
    else:
        new_currency = character["currency"]

    _process_job_progression(db, character, character["level"], new_level)

    db.execute(
        """UPDATE characters
           SET level = ?, exp = ?, currency = ?, current_hp = ?, current_mp = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               pending_boss_monster_id = NULL,
               level_bonus_hp = level_bonus_hp + ?, level_bonus_mp = level_bonus_mp + ?,
               level_bonus_str = level_bonus_str + ?, level_bonus_def = level_bonus_def + ?,
               level_bonus_agi = level_bonus_agi + ?, level_bonus_luk = level_bonus_luk + ?
           WHERE id = ?""",
        (
            new_level, new_exp, new_currency, result["player_hp"], result["player_mp"],
            1 if result["won"] else 0,
            stat_gain["hp"], stat_gain["mp"], stat_gain["str"],
            stat_gain["def"], stat_gain["agi"], stat_gain["luk"],
            character["character_id"],
        ),
    )
    if result["won"]:
        outcome_detail = f"擊敗{boss['name']}，+{exp_gain} EXP +{currency_gain} 諸神幣"
    elif result["timed_out"]:
        outcome_detail = f"與{boss['name']}戰鬥回合已滿，未分勝負，沒有任何諸神幣損失"
    else:
        outcome_detail = f"敗給{boss['name']}，身上 {currency_lost} 諸神幣化為烏有"
    log_activity(
        db, session["user_id"], session["username"], "hunt",
        detail=f"[魔王房間] {ground['name']} {outcome_detail}", ip_address=request.remote_addr,
    )

    if new_level > character["level"]:
        updated = dict(character)
        updated["level"] = new_level
        for stat in ("hp", "mp", "str", "def", "agi", "luk"):
            updated[f"level_bonus_{stat}"] = character[f"level_bonus_{stat}"] + stat_gain[stat]
        stats_after = character_final_stats(updated, equipped_items, settings)
    else:
        stats_after = None

    db.commit()
    db.close()

    return render_template(
        "battle.html",
        ground=ground,
        monster=boss,
        boss_room_challenge=True,
        log=result["log"],
        won=result["won"],
        timed_out=result["timed_out"],
        leveled_up=new_level > character["level"],
        new_level=new_level,
        exp_gain=exp_gain,
        currency_gain=currency_gain,
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
        stats_after=stats_after,
        stat_labels=STAT_LABELS,
    )


def _resolve_bandit_conquest(db, character, settings, stats, current_hp, current_mp):
    """Neutral-tile fight against the persistent-HP 山賊領主 (bandit lord) --
    the one deliberate exception to this game's usual single-action instant
    win/loss battle model (see the module note above _bandit_lord_stats in
    game_data/stats.py). bandit_hp survives across separate /game/conquer
    actions and never regenerates; only a killing blow flips the tile to an
    ordinary country-owned town, with the finishing attacker installed as
    mayor -- matching the existing garrison system's town-capture rule."""
    tile_name = tile_display_name(character["tile_name"], character["tile_type"])
    bandit_profile = _bandit_lord_stats(settings)
    bandit_hp_max = bandit_profile["hp"]

    tile_row = db.execute(
        "SELECT bandit_hp FROM map_tiles WHERE id = ?", (character["current_tile_id"],)
    ).fetchone()
    bandit_hp_before = tile_row["bandit_hp"] if tile_row["bandit_hp"] is not None else bandit_hp_max

    bandit_monster = dict(bandit_profile)
    bandit_monster["hp"] = bandit_hp_before

    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, bandit_monster,
        player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
    )

    bandit_hp_after = result["monster_hp"]
    # monster_hp <= 0 always coincides with the player still being alive at
    # that instant (run_battle's loop breaks the moment either side hits 0,
    # so a simultaneous double-KO can't happen) -- i.e. this is equivalent to
    # result["won"], just phrased the way the spec's mechanics are: "if the
    # resulting monster_hp <= 0, the bandit is dead."
    tile_captured = bandit_hp_after <= 0

    # Modeled like a normal PvE loss (game_hunt), NOT the country-vs-country
    # conquer loss rule -- there's no owning country's treasury for a
    # neutral-tile fight to pay into, so a forfeited half-currency simply
    # vanishes. bandit_hp is deliberately tuned to take several actions to
    # deplete, so BATTLE_ROUND_CAP is routinely hit with both sides still
    # standing -- run_battle reports that inconclusive case as
    # timed_out=True, which is neither a capture nor a defeat and must not
    # cost the attacker anything; only an actual player death (not won, not
    # timed_out) forfeits currency.
    currency_lost = 0
    new_currency = character["currency"]
    if not result["won"] and not result["timed_out"]:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost

    if tile_captured:
        db.execute(
            """UPDATE map_tiles
               SET country_id = ?, tile_type = 'town', mayor_character_id = ?, bandit_hp = NULL
               WHERE id = ?""",
            (character["id"], character["character_id"], character["current_tile_id"]),
        )
        outcome_detail = f"擊敗盤據於{tile_name}的山賊領主，將無主之地收歸領土並自動成為城主"
    else:
        db.execute(
            "UPDATE map_tiles SET bandit_hp = ? WHERE id = ?",
            (bandit_hp_after, character["current_tile_id"]),
        )
        if currency_lost:
            outcome_detail = (
                f"攻打{tile_name}的山賊領主時力竭倒下，身上{currency_lost}諸神幣化為烏有"
                f"（山賊領主剩餘 HP {bandit_hp_after}/{bandit_hp_max}）"
            )
        else:
            outcome_detail = f"削弱了{tile_name}的山賊領主（剩餘 HP {bandit_hp_after}/{bandit_hp_max}）"

    db.execute(
        """UPDATE characters
           SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               pending_boss_monster_id = NULL
           WHERE id = ?""",
        (
            new_currency, result["player_hp"], result["player_mp"], _next_action_at(settings["turn_wait_seconds"]),
            1 if tile_captured else 0, character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "conquer_win" if tile_captured else "conquer_loss",
        detail=outcome_detail, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    return render_template(
        "battle.html",
        conquest=True,
        bandit_fight=True,
        captured_tile_name=tile_name,
        defending_country_name=character["name"],  # attacker's own country, once captured
        monster=bandit_monster,
        log=result["log"],
        won=tile_captured,
        timed_out=result["timed_out"],
        tile_captured=tile_captured,
        attacker_defeated=not result["won"] and not result["timed_out"],
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
        bandit_hp_remaining=max(0, bandit_hp_after),
        bandit_hp_max=bandit_hp_max,
    )


@game_bp.route("/game/conquer", methods=["POST"])
@character_required
def game_conquer():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.current_tile_id, characters.level,
                  characters.exp, characters.next_action_at, characters.current_hp, characters.current_mp,
                  characters.currency, characters.name AS character_name,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  map_tiles.tile_type, map_tiles.country_id AS tile_country_id, map_tiles.name AS tile_name,
                  countries.*
           FROM characters
           JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game"))

    is_neutral_target = character["tile_type"] == "neutral"
    is_enemy_town_target = (
        character["tile_type"] in ("fortress", "town")
        and character["tile_country_id"] is not None
        and character["tile_country_id"] != character["id"]
    )
    if not is_neutral_target and not is_enemy_town_target:
        db.close()
        flash("這裡沒有可以攻打的敵方據點")
        return redirect(url_for("game.game"))

    settings = db.execute(
        """SELECT turn_wait_seconds, town_defense_level, fortress_defense_level, rebirth_stat_bonus_percent,
                  war_town_weekday, war_town_start_time, war_town_end_time,
                  war_fortress_weekday, war_fortress_start_time, war_fortress_end_time
           FROM game_settings WHERE id = 1"""
    ).fetchone()

    # War-window gate: must run before the garrison-withdrawal-confirmation
    # gate below, so a player outside the window gets the "not war time"
    # message immediately instead of first being asked to confirm withdrawing
    # a garrison for an attack that can't happen anyway.
    window_kind = _war_window_kind_for_tile_type(character["tile_type"])
    if window_kind == "fortress":
        war_weekday, war_start_time, war_end_time = (
            settings["war_fortress_weekday"], settings["war_fortress_start_time"], settings["war_fortress_end_time"]
        )
    else:
        war_weekday, war_start_time, war_end_time = (
            settings["war_town_weekday"], settings["war_town_start_time"], settings["war_town_end_time"]
        )
    if not _in_war_window(war_weekday, war_start_time, war_end_time):
        db.close()
        kind_label = "要塞" if window_kind == "fortress" else "城鎮／荒地"
        flash(
            f"現在不是國戰時段，無法攻打{kind_label}。"
            f"開放時段：{_war_window_label(war_weekday, war_start_time, war_end_time)}（台灣時間）"
        )
        return redirect(url_for("game.game"))

    # Garrisoning anywhere (not necessarily at this tile) blocks attacking --
    # you can't defend your own country's tiles and attack an enemy tile in
    # the same breath. Withdrawal-and-attack combine into this one action
    # once the player confirms, rather than wasting a separate turn.
    own_garrison = db.execute(
        "SELECT id FROM garrisons WHERE character_id = ?", (character["character_id"],)
    ).fetchone()
    if own_garrison is not None and request.form.get("confirm_withdraw_garrison") != "1":
        db.close()
        flash("你目前正在駐防中，攻打前必須先撤離駐防，請確認是否撤離並攻打")
        return _render_game(pending_conquer_confirm=True)

    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game.game"))

    if own_garrison is not None:
        db.execute("DELETE FROM garrisons WHERE id = ?", (own_garrison["id"],))

    if is_neutral_target:
        return _resolve_bandit_conquest(db, character, settings, stats, current_hp, current_mp)

    defending_country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (character["tile_country_id"],)
    ).fetchone()
    tile_name = tile_display_name(character["tile_name"], character["tile_type"])

    # LIFO defender queue: the most recently-stationed garrison at this tile
    # is fought first. Only once every garrisoned defender is cleared does an
    # attack action reach the tile's NPC defense tower.
    defender_row = db.execute(
        """SELECT garrisons.id AS garrison_id, characters.id AS defender_id,
                  characters.name AS defender_name, characters.level, characters.job_class,
                  characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.pvp_battles_count, characters.pvp_wins_count,
                  countries.*
           FROM garrisons
           JOIN characters ON characters.id = garrisons.character_id
           JOIN countries ON countries.id = characters.country_id
           WHERE garrisons.tile_id = ?
           ORDER BY garrisons.stationed_at DESC
           LIMIT 1""",
        (character["current_tile_id"],),
    ).fetchone()

    if defender_row is not None:
        # A PvP defender is on top of the stack -- fight them instead of the
        # tower this action. Their stats are computed fresh at full HP/MP
        # every time (character_final_stats), never their own possibly-
        # damaged current_hp/current_mp from unrelated activity elsewhere --
        # same approach defense_tower_stats already uses for the NPC tower.
        # Their own learned skills do NOT trigger (same simplification
        # defense_tower_stats already has: only the "player" side of
        # run_battle ever gets usable_skills).
        defender_equipped_items = _fetch_equipped_items(db, {
            "equipped_weapon_id": defender_row["equipped_weapon_id"],
            "equipped_armor_id": defender_row["equipped_armor_id"],
            "equipped_accessory_id": defender_row["equipped_accessory_id"],
        })
        defender_stats = character_final_stats(defender_row, defender_equipped_items, settings)
        defender_monster = {
            "name": defender_row["defender_name"],
            "hp": defender_stats["hp"], "atk": defender_stats["str"],
            "def": defender_stats["def"], "agi": defender_stats["agi"],
            "element": defender_row["element"],
        }

        result = run_battle(
            character["character_name"], stats, character["element"], current_hp, defender_monster,
            player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
        )

        # No currency/EXP for either side on a defender-vs-attacker duel --
        # the only reward remains actually flipping the tile, which only
        # happens once the tower itself falls. An attacker loss still
        # forfeits half currency to the defending country's treasury exactly
        # as an ordinary tower loss does.
        currency_lost = 0
        new_currency = character["currency"]
        if result["won"]:
            db.execute("DELETE FROM garrisons WHERE id = ?", (defender_row["garrison_id"],))
            outcome_detail = f"擊敗了駐防於{tile_name}的{defender_row['defender_name']}"
        elif result["timed_out"]:
            outcome_detail = (
                f"攻打{tile_name}時與駐防的{defender_row['defender_name']}戰鬥回合已滿，"
                f"未分勝負，沒有任何諸神幣損失"
            )
        else:
            currency_lost = character["currency"] // 2
            new_currency = character["currency"] - currency_lost
            db.execute(
                "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
                (currency_lost, defending_country["id"]),
            )
            outcome_detail = (
                f"攻打{tile_name}時輸給了駐防的{defender_row['defender_name']}，"
                f"身上{currency_lost}諸神幣被{defending_country['name']}沒收"
            )

        # 貢獻值: only national PvP conquest combat (this branch and the tower
        # branch below) earns contribution -- timed_out is a true no-op, same
        # as it already is for currency.
        if result["won"]:
            attacker_contribution_delta = CONTRIBUTION_ATTACK_WIN
            defender_contribution_delta = CONTRIBUTION_DEFENSE_LOSS
        elif result["timed_out"]:
            attacker_contribution_delta = 0
            defender_contribution_delta = 0
        else:
            attacker_contribution_delta = CONTRIBUTION_ATTACK_LOSS
            defender_contribution_delta = CONTRIBUTION_DEFENSE_WIN

        db.execute(
            """UPDATE characters
               SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
                   pvp_battles_count = pvp_battles_count + 1,
                   pvp_wins_count = pvp_wins_count + ?,
                   contribution = contribution + ?,
                   pending_boss_monster_id = NULL
               WHERE id = ?""",
            (
                new_currency, result["player_hp"], result["player_mp"],
                _next_action_at(settings["turn_wait_seconds"]),
                1 if result["won"] else 0, attacker_contribution_delta, character["character_id"],
            ),
        )
        # Defender's own current_hp/current_mp are NOT touched (point 6 --
        # always a fresh full-stats fight, no persisted damage carryover);
        # only their PvP counters change, and their garrison row is removed
        # above if they lost. A defender loss (attacker won) also starts a
        # 10-minute garrison_cooldown_until so they can't immediately
        # re-station; a defender win sets no cooldown.
        if result["won"]:
            db.execute(
                """UPDATE characters
                   SET pvp_battles_count = pvp_battles_count + 1,
                       pvp_wins_count = pvp_wins_count + 0,
                       contribution = contribution + ?,
                       garrison_cooldown_until = ?
                   WHERE id = ?""",
                (
                    defender_contribution_delta,
                    _next_action_at(GARRISON_DEFENSE_LOSS_COOLDOWN_SECONDS),
                    defender_row["defender_id"],
                ),
            )
        else:
            db.execute(
                """UPDATE characters
                   SET pvp_battles_count = pvp_battles_count + 1,
                       pvp_wins_count = pvp_wins_count + ?,
                       contribution = contribution + ?
                   WHERE id = ?""",
                (
                    1,
                    defender_contribution_delta,
                    defender_row["defender_id"],
                ),
            )
        log_activity(
            db, session["user_id"], session["username"],
            "conquer_win" if result["won"] else "conquer_loss",
            detail=outcome_detail, ip_address=request.remote_addr,
        )
        db.commit()
        db.close()

        return render_template(
            "battle.html",
            conquest=True,
            captured_tile_name=tile_name,
            defending_country_name=defending_country["name"],
            monster=defender_monster,
            log=result["log"],
            won=result["won"],
            timed_out=result["timed_out"],
            currency_lost=currency_lost,
            player_hp=result["player_hp"],
            max_hp=stats["hp"],
            player_mp=result["player_mp"],
            max_mp=stats["mp"],
            player_stats=stats,
        )

    # No PvP defender remains at this tile -- proceed exactly as the
    # existing tower-fight logic, plus mayor assignment on a town capture.
    tower = defense_tower_stats(defending_country, character["tile_type"], settings)

    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, tower,
        player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
    )

    currency_lost = 0
    if result["won"]:
        new_currency = character["currency"]
        db.execute(
            "UPDATE map_tiles SET country_id = ?, mayor_character_id = ? WHERE id = ?",
            (
                character["id"],
                character["character_id"] if character["tile_type"] == "town" else None,
                character["current_tile_id"],
            ),
        )
        outcome_detail = f"攻下{tile_name}（原屬{defending_country['name']}）"
    elif result["timed_out"]:
        new_currency = character["currency"]
        outcome_detail = f"攻打{tile_name}戰鬥回合已滿，未分勝負，沒有任何諸神幣損失"
    else:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
            (currency_lost, defending_country["id"]),
        )
        outcome_detail = (
            f"攻打{tile_name}失敗，身上{currency_lost}諸神幣被{defending_country['name']}沒收"
        )

    # 貢獻值: NPC tower fight -- attacker-only, no defender to award (there is
    # no defender_row in this branch). timed_out stays a no-op.
    if result["won"]:
        tower_attacker_contribution_delta = CONTRIBUTION_ATTACK_WIN
    elif result["timed_out"]:
        tower_attacker_contribution_delta = 0
    else:
        tower_attacker_contribution_delta = CONTRIBUTION_ATTACK_LOSS

    db.execute(
        """UPDATE characters
           SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               battles_count = battles_count + 1, wins_count = wins_count + ?,
               contribution = contribution + ?,
               pending_boss_monster_id = NULL
           WHERE id = ?""",
        (
            new_currency, result["player_hp"], result["player_mp"], _next_action_at(settings["turn_wait_seconds"]),
            1 if result["won"] else 0, tower_attacker_contribution_delta, character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "conquer_win" if result["won"] else "conquer_loss",
        detail=outcome_detail, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    return render_template(
        "battle.html",
        conquest=True,
        captured_tile_name=tile_name,
        defending_country_name=defending_country["name"],
        monster=tower,
        log=result["log"],
        won=result["won"],
        timed_out=result["timed_out"],
        currency_lost=currency_lost,
        player_hp=result["player_hp"],
        max_hp=stats["hp"],
        player_mp=result["player_mp"],
        max_mp=stats["mp"],
        player_stats=stats,
    )


@game_bp.route("/game/garrison/station", methods=["POST"])
@character_required
def game_garrison_station():
    db = get_db()
    character = db.execute(
        "SELECT id, current_tile_id, country_id, garrison_cooldown_until FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()

    remaining_cooldown = _cooldown_remaining_seconds(character["garrison_cooldown_until"])
    if remaining_cooldown > 0:
        db.close()
        flash(f"防守失敗後需要等待才能重新駐防，還需 {_format_duration(remaining_cooldown)}")
        return redirect(url_for("game.game"))

    existing = db.execute(
        "SELECT id, tile_id FROM garrisons WHERE character_id = ?", (character["id"],)
    ).fetchone()
    if existing is not None:
        db.close()
        if existing["tile_id"] == character["current_tile_id"]:
            flash("你已經駐防在這裡了")
        else:
            flash("你已經在別處駐防中，請先撤離駐防")
        return redirect(url_for("game.game"))

    tile = db.execute(
        "SELECT id, tile_type, country_id, name FROM map_tiles WHERE id = ?",
        (character["current_tile_id"],),
    ).fetchone()
    if (
        tile is None
        or tile["tile_type"] not in ("fortress", "town")
        or tile["country_id"] != character["country_id"]
    ):
        db.close()
        flash("只能在自己國家的要塞或城鎮駐防")
        return redirect(url_for("game.game"))

    db.execute(
        "INSERT INTO garrisons (character_id, tile_id) VALUES (?, ?)",
        (character["id"], tile["id"]),
    )
    tile_name = tile_display_name(tile["name"], tile["tile_type"])
    log_activity(
        db, session["user_id"], session["username"], "garrison_station",
        detail=tile_name, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已駐防於「{tile_name}」")
    return redirect(url_for("game.game"))


@game_bp.route("/game/garrison/withdraw", methods=["POST"])
@character_required
def game_garrison_withdraw():
    db = get_db()
    character = db.execute(
        "SELECT id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    deleted = db.execute(
        "DELETE FROM garrisons WHERE character_id = ?", (character["id"],)
    ).rowcount
    if deleted:
        log_activity(
            db, session["user_id"], session["username"], "garrison_withdraw",
            ip_address=request.remote_addr,
        )
    db.commit()
    db.close()
    flash("已撤離駐防" if deleted else "你目前沒有在駐防")
    return redirect(url_for("game.game"))


@game_bp.route("/game/recover", methods=["POST"])
@character_required
def game_recover():
    db = get_db()
    character = db.execute(
        """SELECT characters.id, characters.level, characters.next_action_at, characters.currency,
                  characters.current_hp, characters.current_mp, map_tiles.tile_type,
                  map_tiles.country_id AS tile_country_id,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id, characters.job_class, characters.job_tier,
                  characters.rebirth_count, characters.stat_floor_hp, characters.stat_floor_mp,
                  characters.stat_floor_str, characters.stat_floor_def, characters.stat_floor_agi,
                  characters.stat_floor_luk, characters.level_bonus_hp, characters.level_bonus_mp,
                  characters.level_bonus_str, characters.level_bonus_def, characters.level_bonus_agi,
                  characters.level_bonus_luk, countries.*
           FROM characters
           JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內回復 HP／MP")
        return redirect(url_for("game.game"))

    settings = db.execute(
        "SELECT turn_wait_seconds, heal_cost_per_point, rebirth_stat_bonus_percent FROM game_settings WHERE id = 1"
    ).fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, stats)

    missing = (stats["hp"] - current_hp) + (stats["mp"] - current_mp)
    cost = round(missing * settings["heal_cost_per_point"])
    if cost > character["currency"]:
        if current_hp <= 0:
            # Stuck-safety valve: HP is fully gone and can't afford a full
            # heal -- still heal, just take every last coin instead of
            # blocking the player from ever recovering.
            cost = character["currency"]
        else:
            db.close()
            flash(f"諸神幣不足，完全回復需要 {cost} 諸神幣")
            return redirect(url_for("game.game"))

    db.execute(
        """UPDATE characters SET current_hp = ?, current_mp = ?, currency = currency - ?,
               next_action_at = ?, pending_boss_monster_id = NULL WHERE id = ?""",
        (stats["hp"], stats["mp"], cost, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    if cost and character["tile_country_id"] is not None:
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
            (cost, character["tile_country_id"]),
        )
    log_activity(
        db, session["user_id"], session["username"], "recover",
        detail=f"花費 {cost} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"HP／MP 已完全回復，花費 {cost} 諸神幣")
    return redirect(url_for("game.game"))


def _character_for_shop(db):
    return db.execute(
        """SELECT characters.id, characters.currency, characters.bank_balance,
                  characters.next_action_at, characters.country_id, map_tiles.tile_type,
                  map_tiles.country_id AS tile_country_id,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.contribution, characters.donated_today, characters.donated_today_date
           FROM characters JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


@game_bp.route("/game/shop")
@character_required
def game_shop():
    db = get_db()
    character = _character_for_shop(db)

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內使用商店")
        return redirect(url_for("game.game"))

    settings = db.execute(
        "SELECT turn_wait_seconds, sell_back_percent FROM game_settings WHERE id = 1"
    ).fetchone()

    all_items = db.execute(
        """SELECT items.*, countries.name AS set_country_name
           FROM items LEFT JOIN countries ON countries.id = items.country_id
           WHERE items.country_id IS NULL OR items.country_id = ?
           ORDER BY items.shop_type, items.price""",
        (character["tile_country_id"],),
    ).fetchall()
    shop_items = {shop_type: [] for shop_type in SHOP_TYPE_LABELS}
    for item in all_items:
        shop_items[item["shop_type"]].append(item)

    inventory_rows = db.execute(
        """SELECT items.*, inventory.quantity AS quantity
           FROM inventory JOIN items ON items.id = inventory.item_id
           WHERE inventory.character_id = ?
           ORDER BY items.shop_type, items.price""",
        (character["id"],),
    ).fetchall()
    inventory_items = {shop_type: [] for shop_type in SHOP_TYPE_LABELS}
    for row in inventory_rows:
        inventory_items[row["shop_type"]].append(row)

    equipped_slots = []
    suggestions = []
    for shop_type, label in SLOT_LABELS.items():
        slot_column = EQUIP_SLOT_COLUMNS[shop_type]
        equipped_item_id = character[slot_column]
        equipped_item = (
            db.execute("SELECT * FROM items WHERE id = ?", (equipped_item_id,)).fetchone()
            if equipped_item_id else None
        )
        equipped_slots.append({"slot": shop_type, "label": label, "item": equipped_item})

        candidates = inventory_items[shop_type]
        if candidates:
            best = max(candidates, key=lambda i: i["stat_bonus"])
            if equipped_item is None or best["stat_bonus"] > equipped_item["stat_bonus"]:
                suggestions.append({
                    "slot": shop_type,
                    "label": label,
                    "item": best,
                    "current": equipped_item,
                })

    db.close()

    return render_template(
        "shop.html",
        character=character,
        cooldown_seconds=_cooldown_remaining_seconds(character["next_action_at"]),
        shop_items=shop_items,
        shop_type_labels=SHOP_TYPE_LABELS,
        inventory_items=inventory_items,
        sell_back_percent=settings["sell_back_percent"],
        equipped_slots=equipped_slots,
        suggestions=suggestions,
    )


@game_bp.route("/game/shop/buy", methods=["POST"])
@character_required
def game_shop_buy():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game_shop"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內的商店購買裝備")
        return redirect(url_for("game.game"))

    item_ids = [i for i in request.form.getlist("item_ids") if i]
    if not item_ids:
        db.close()
        flash("請至少選擇一件要購買的裝備")
        return redirect(url_for("game.game_shop"))

    placeholders = ",".join("?" for _ in item_ids)
    items = db.execute(f"SELECT * FROM items WHERE id IN ({placeholders})", item_ids).fetchall()
    if not items:
        db.close()
        flash("請選擇有效的商品")
        return redirect(url_for("game.game_shop"))

    total_price = sum(item["price"] for item in items)
    if character["currency"] < total_price:
        db.close()
        flash(f"諸神幣不足，這次購買需要 {total_price} 諸神幣")
        return redirect(url_for("game.game_shop"))

    settings = db.execute(
        "SELECT turn_wait_seconds, shop_tax_percent FROM game_settings WHERE id = 1"
    ).fetchone()
    for item in items:
        _add_to_inventory(db, character["id"], item["id"], 1)
    db.execute(
        "UPDATE characters SET currency = currency - ?, next_action_at = ? WHERE id = ?",
        (total_price, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    tax = round(total_price * settings["shop_tax_percent"] / 100)
    if tax:
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (tax, character["country_id"])
        )
    names = "、".join(item["name"] for item in items)
    log_activity(
        db, session["user_id"], session["username"], "shop_buy",
        detail=f"{names} ({total_price} 諸神幣)", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已購買「{names}」，放入背包")
    return redirect(url_for("game.game_shop"))


@game_bp.route("/game/shop/sell", methods=["POST"])
@character_required
def game_shop_sell():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game_shop"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內的商店出售裝備")
        return redirect(url_for("game.game"))

    item_ids = [i for i in request.form.getlist("item_ids") if i]
    if not item_ids:
        db.close()
        flash("請至少選擇一件要出售的裝備")
        return redirect(url_for("game.game_shop"))

    settings = db.execute(
        "SELECT turn_wait_seconds, sell_back_percent, shop_tax_percent FROM game_settings WHERE id = 1"
    ).fetchone()

    sold_names = []
    total_refund = 0
    for item_id in item_ids:
        try:
            qty = int(request.form.get(f"qty_{item_id}", "1"))
        except ValueError:
            qty = 1
        row = db.execute(
            """SELECT items.*, inventory.quantity AS owned
               FROM inventory JOIN items ON items.id = inventory.item_id
               WHERE inventory.character_id = ? AND inventory.item_id = ?""",
            (character["id"], item_id),
        ).fetchone()
        if row is None:
            continue
        qty = max(1, min(qty, row["owned"]))
        if not _remove_from_inventory(db, character["id"], row["id"], qty):
            continue
        refund = round(row["price"] * settings["sell_back_percent"] / 100) * qty
        total_refund += refund
        sold_names.append(f"{row['name']} x{qty}" if qty > 1 else row["name"])

    if not sold_names:
        db.close()
        flash("背包裡沒有可以出售的裝備")
        return redirect(url_for("game.game_shop"))

    db.execute(
        "UPDATE characters SET currency = currency + ?, next_action_at = ? WHERE id = ?",
        (total_refund, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    tax = round(total_refund * settings["shop_tax_percent"] / 100)
    if tax:
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (tax, character["country_id"])
        )
    log_activity(
        db, session["user_id"], session["username"], "shop_sell",
        detail=f"{'、'.join(sold_names)} (+{total_refund} 諸神幣)", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已出售「{'、'.join(sold_names)}」，獲得 {total_refund} 諸神幣")
    return redirect(url_for("game.game_shop"))


BANK_AMOUNT_UNIT = 1000

# 貢獻值 (contribution) constants -- earned via national PvP conquest combat
# (game_conquer, enemy-country tiles only) and via treasury donations
# (game_treasury_donate). See CLAUDE.md-adjacent design notes in those
# functions for the exact rules.
CONTRIBUTION_ATTACK_WIN = 10
CONTRIBUTION_ATTACK_LOSS = 5
CONTRIBUTION_DEFENSE_WIN = 10
CONTRIBUTION_DEFENSE_LOSS = 5
GARRISON_DEFENSE_LOSS_COOLDOWN_SECONDS = 600
CONTRIBUTION_PER_DONATION_UNIT = 1000
DONATE_DAILY_CAP_DEFAULT = 10000
DONATE_DAILY_CAP_KING = 20000
DONATE_DAILY_CAP_OFFICER = 15000


def _parse_bank_amount(raw):
    """Bank deposits/withdrawals and treasury donations must be a positive
    multiple of BANK_AMOUNT_UNIT -- no other amount is accepted."""
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or amount % BANK_AMOUNT_UNIT != 0:
        return None
    return amount


@game_bp.route("/game/bank/deposit", methods=["POST"])
@character_required
def game_bank_deposit():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內使用銀行")
        return redirect(url_for("game.game"))

    amount = _parse_bank_amount(request.form.get("amount", ""))
    if amount is None:
        db.close()
        flash(f"存入金額必須是 {BANK_AMOUNT_UNIT} 的倍數")
        return redirect(url_for("game.game"))
    if amount > character["currency"]:
        db.close()
        flash("存入金額不可超過身上諸神幣數量")
        return redirect(url_for("game.game"))

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        """UPDATE characters SET currency = currency - ?, bank_balance = bank_balance + ?,
               next_action_at = ? WHERE id = ?""",
        (amount, amount, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "bank_deposit",
        detail=f"存入 {amount} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已存入 {amount} 諸神幣")
    return redirect(url_for("game.game"))


@game_bp.route("/game/bank/withdraw", methods=["POST"])
@character_required
def game_bank_withdraw():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內使用銀行")
        return redirect(url_for("game.game"))

    amount = _parse_bank_amount(request.form.get("amount", ""))
    if amount is None:
        db.close()
        flash(f"提領金額必須是 {BANK_AMOUNT_UNIT} 的倍數")
        return redirect(url_for("game.game"))
    if amount > character["bank_balance"]:
        db.close()
        flash("提領金額不可超過銀行存款數量")
        return redirect(url_for("game.game"))

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        """UPDATE characters SET currency = currency + ?, bank_balance = bank_balance - ?,
               next_action_at = ? WHERE id = ?""",
        (amount, amount, _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "bank_withdraw",
        detail=f"提領 {amount} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已提領 {amount} 諸神幣")
    return redirect(url_for("game.game"))


@game_bp.route("/game/treasury/donate", methods=["POST"])
@character_required
def game_treasury_donate():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.game"))

    if character["tile_type"] != "fortress":
        db.close()
        flash("只能在要塞內捐獻給國庫")
        return redirect(url_for("game.game"))

    amount = _parse_bank_amount(request.form.get("amount", ""))
    if amount is None:
        db.close()
        flash(f"捐獻金額必須是 {BANK_AMOUNT_UNIT} 的倍數")
        return redirect(url_for("game.game"))
    if amount > character["currency"]:
        db.close()
        flash("捐獻金額不可超過身上諸神幣數量")
        return redirect(url_for("game.game"))

    # 貢獻值 daily donation cap: depends on the character's CURRENT government
    # role in their own country (國王 > 參謀/大將軍 > 一般人), resets when the
    # UTC calendar date changes.
    country_roles = db.execute(
        "SELECT king_character_id, advisor_character_id, general_character_id FROM countries WHERE id = ?",
        (character["country_id"],),
    ).fetchone()
    if country_roles is not None and character["id"] == country_roles["king_character_id"]:
        donate_cap = DONATE_DAILY_CAP_KING
    elif country_roles is not None and character["id"] in (
        country_roles["advisor_character_id"], country_roles["general_character_id"],
    ):
        donate_cap = DONATE_DAILY_CAP_OFFICER
    else:
        donate_cap = DONATE_DAILY_CAP_DEFAULT

    today = datetime.utcnow().strftime("%Y-%m-%d")
    donated_so_far = character["donated_today"] if character["donated_today_date"] == today else 0
    if donated_so_far + amount > donate_cap:
        db.close()
        flash(
            f"今日捐獻已達上限（{donate_cap} 諸神幣），"
            f"今天還可以捐獻 {max(0, donate_cap - donated_so_far)} 諸神幣"
        )
        return redirect(url_for("game.game"))

    contribution_gained = amount // CONTRIBUTION_PER_DONATION_UNIT
    new_donated_today = donated_so_far + amount

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        """UPDATE characters
           SET currency = currency - ?, next_action_at = ?,
               contribution = contribution + ?,
               donated_today = ?, donated_today_date = ?
           WHERE id = ?""",
        (
            amount, _next_action_at(settings["turn_wait_seconds"]),
            contribution_gained, new_donated_today, today, character["id"],
        ),
    )
    db.execute(
        "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (amount, character["country_id"])
    )
    log_activity(
        db, session["user_id"], session["username"], "treasury_donate",
        detail=f"捐獻 {amount} 諸神幣給國庫，獲得 {contribution_gained} 貢獻值",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已捐獻 {amount} 諸神幣給國庫，獲得 {contribution_gained} 貢獻值")
    return redirect(url_for("game.game"))


EQUIPMENT_RETURN_ENDPOINTS = {
    "shop": "game.game_shop",
    "character": "character.character_page",
}


def _equipment_return_redirect(request):
    endpoint = EQUIPMENT_RETURN_ENDPOINTS.get(request.form.get("next", "shop"), "game.game_shop")
    return redirect(url_for(endpoint))


@game_bp.route("/game/equip", methods=["POST"])
@character_required
def game_equip():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return _equipment_return_redirect(request)

    item = db.execute(
        "SELECT * FROM items WHERE id = ?", (request.form.get("item_id", ""),)
    ).fetchone()
    if item is None:
        db.close()
        flash("請選擇一個有效的裝備")
        return _equipment_return_redirect(request)

    if not _remove_from_inventory(db, character["id"], item["id"], 1):
        db.close()
        flash("背包裡沒有這件裝備")
        return _equipment_return_redirect(request)

    slot_column = EQUIP_SLOT_COLUMNS[item["shop_type"]]
    old_item_id = character[slot_column]
    if old_item_id:
        _add_to_inventory(db, character["id"], old_item_id, 1)

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        f"UPDATE characters SET {slot_column} = ?, next_action_at = ? WHERE id = ?",
        (item["id"], _next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "equip",
        detail=item["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已裝備「{item['name']}」")
    return _equipment_return_redirect(request)


@game_bp.route("/game/unequip", methods=["POST"])
@character_required
def game_unequip():
    db = get_db()
    character = _character_for_shop(db)

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return _equipment_return_redirect(request)

    slot = request.form.get("slot", "")
    slot_column = EQUIP_SLOT_COLUMNS.get(slot)
    if slot_column is None:
        db.close()
        flash("請選擇一個有效的裝備部位")
        return _equipment_return_redirect(request)

    item_id = character[slot_column]
    if not item_id:
        db.close()
        flash("該部位目前沒有裝備任何東西")
        return _equipment_return_redirect(request)

    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    _add_to_inventory(db, character["id"], item_id, 1)

    settings = db.execute("SELECT turn_wait_seconds FROM game_settings WHERE id = 1").fetchone()
    db.execute(
        f"UPDATE characters SET {slot_column} = NULL, next_action_at = ? WHERE id = ?",
        (_next_action_at(settings["turn_wait_seconds"]), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "unequip",
        detail=item["name"], ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已卸下「{item['name']}」，放回背包")
    return _equipment_return_redirect(request)


@game_bp.route("/countries")
@character_required
def countries_page():
    db = get_db()
    countries = db.execute("SELECT * FROM countries ORDER BY id").fetchall()
    tile_counts = {
        row["country_id"]: row["c"]
        for row in db.execute(
            "SELECT country_id, COUNT(*) AS c FROM map_tiles WHERE country_id IS NOT NULL GROUP BY country_id"
        ).fetchall()
    }
    character_names = {
        row["id"]: row["name"] for row in db.execute("SELECT id, name FROM characters").fetchall()
    }

    # Garrison headcounts are only ever computed (and only ever shown) for
    # the viewing character's OWN country, and only when that character
    # holds one of its government seats -- never exposed for other countries.
    own_character = db.execute(
        "SELECT id, country_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    own_country = next((c for c in countries if c["id"] == own_character["country_id"]), None)
    is_officer = own_country is not None and own_character["id"] in (
        own_country["king_character_id"],
        own_country["advisor_character_id"],
        own_country["general_character_id"],
    )
    garrison_tiles_by_country_id = {}
    if is_officer:
        tile_rows = db.execute(
            """SELECT map_tiles.id AS tile_id, map_tiles.name, map_tiles.tile_type,
                      COUNT(garrisons.id) AS garrison_count
               FROM map_tiles LEFT JOIN garrisons ON garrisons.tile_id = map_tiles.id
               WHERE map_tiles.country_id = ? AND map_tiles.tile_type IN ('fortress', 'town')
               GROUP BY map_tiles.id
               ORDER BY map_tiles.tile_type DESC, map_tiles.name""",
            (own_country["id"],),
        ).fetchall()
        garrison_tiles_by_country_id[own_country["id"]] = [
            {
                "name": tile_display_name(r["name"], r["tile_type"]),
                "garrison_count": r["garrison_count"],
            }
            for r in tile_rows
        ]
    db.close()

    rows = []
    for c in countries:
        rows.append({
            "name": c["name"],
            "element": c["element"],
            "description": c["description"],
            "treasury": c["treasury"],
            "tile_count": tile_counts.get(c["id"], 0),
            "roles": [
                {"label": role["label"], "holder": character_names.get(c[role["column"]])}
                for role in GOVERNMENT_ROLES
            ],
            "garrison_tiles": garrison_tiles_by_country_id.get(c["id"]),
        })

    return render_template("countries.html", countries=rows, roles=GOVERNMENT_ROLES)
