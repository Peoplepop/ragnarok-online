"""The main game loop: map movement, hunting, conquest, shop, bank and equipment."""
import random
from datetime import datetime, timezone

from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from db import get_db, log_activity, LEVEL_CAP, HIDDEN_GROUND_TIERS, HIDDEN_SET_KEYS_BY_MAP
from map_layout import axial_to_pixel, hex_corners, axial_distance
from web_helpers import (
    character_required, _next_action_at, _cooldown_remaining_seconds, _in_war_window,
    _war_window_label, _war_window_kind_for_tile_type, _format_duration, _add_to_inventory,
    _remove_from_inventory, _in_any_war_window, _taipei_now, _parse_dt,
    _current_war_window_end, _morale_buff_active, _tournament_registration_open,
    _taipei_time_label, ACTION_DT_FORMAT, _major_event_feed,
)
from game_data.constants import (
    SHOP_TYPE_LABELS, SLOT_LABELS, EQUIP_SLOT_COLUMNS, GOVERNMENT_ROLES, tile_display_name,
    HEX_SIZE, ELEMENT_COLORS, NEUTRAL_TILE_COLOR, MOUNTAIN_TILE_COLOR, STAT_LABELS,
    CHAT_MESSAGE_MAX_LEN,
)
from game_data.jobs import _process_job_progression
from game_data.skills import TIER4_SLOT2_SKILL_KEYS, SKILL_CATALOG, _character_usable_skills
from game_data.equipment import _fetch_equipped_items, character_special_effects
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


def _roll_hidden_encounter(db, settings):
    """The 秘境 interrupt roll, made once per ordinary /game/hunt action
    (never for conquest, garrison duels, the tournament, the bandit lord or
    the 魔王房間 follow-up, and never when an admin force-picks a monster).

    Rarest first, and returned on the FIRST hit, so 太極 and 無極 can never
    both fire in the same action. Applies at every one of the 4 regular
    hunting grounds -- which ground the player picked makes no difference.

    Returns (hidden_key, ground_row, monster_row) or (None, None, None)."""
    for hidden_key in ("wuji", "taiji"):
        cfg = HIDDEN_GROUND_TIERS[hidden_key]
        if random.random() * 100 >= settings[cfg["trigger_setting"]]:
            continue
        ground = db.execute(
            "SELECT * FROM hunting_grounds WHERE tier = ?", (cfg["tier"],)
        ).fetchone()
        if ground is None:
            continue
        monster = db.execute(
            "SELECT * FROM monsters WHERE hunting_ground_id = ? LIMIT 1", (ground["id"],)
        ).fetchone()
        if monster is None:
            continue
        return hidden_key, ground, monster
    return None, None, None


def _hidden_key_for_ground(ground):
    """Which HIDDEN_GROUND_TIERS entry (if any) a hunting_grounds row is.
    Used to recognize an admin-forced hidden fight, which reaches game_hunt
    through the forced-monster path instead of the trigger roll."""
    if not ground["is_hidden"]:
        return None
    return next(
        (key for key, cfg in HIDDEN_GROUND_TIERS.items() if cfg["tier"] == ground["tier"]), None
    )


def _roll_hidden_loot(db, settings, hidden_key, character_id):
    """Post-WIN drop roll for a 秘境 fight. On success picks ONE row uniformly
    at random from that map's own 15 pieces (5 elemental sets x 3 slots) --
    explicitly NOT tied to the winner's own country element ("五行隨機") --
    and puts it straight in the winner's inventory through the same
    _add_to_inventory the shop and trade systems use.

    Returns the dropped item row, or None when the roll failed (or, defensively,
    when the map somehow has no loot pool seeded)."""
    if random.random() * 100 >= settings[HIDDEN_GROUND_TIERS[hidden_key]["drop_setting"]]:
        return None
    set_keys = HIDDEN_SET_KEYS_BY_MAP.get(hidden_key, [])
    if not set_keys:
        return None
    placeholders = ",".join("?" for _ in set_keys)
    pool = db.execute(
        f"SELECT * FROM items WHERE hidden_set_key IN ({placeholders}) ORDER BY id", set_keys,
    ).fetchall()
    if not pool:
        return None
    dropped = random.choice(pool)
    _add_to_inventory(db, character_id, dropped["id"], 1)
    return dropped


def _debuffed_monster(monster, special_effects):
    """A plain-dict copy of a monster row with its hp/atk/def/agi scaled down
    by the wearer's 怪物弱化 (enemy_debuff) percent, floored at 1 so a huge
    future percent can never produce a 0-stat monster. Always returns a dict
    copy rather than mutating the argument: monster rows are sqlite3.Row,
    which is immutable, and the tower/bandit callers pass dicts that other
    code still reads afterwards.

    Deliberately scoped to monster fights only (game_hunt and
    game_hunt_boss_room) -- conquest, garrison duels and the tournament never
    call this, per the confirmed "怪物 only" scope."""
    percent = special_effects.get("enemy_debuff", 0)
    scaled = dict(monster)
    if not percent:
        return scaled
    for key in ("hp", "atk", "def", "agi"):
        scaled[key] = max(1, round(scaled[key] * (1 - percent / 100)))
    return scaled


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
    # is_hidden = 0: the two 秘境 are never selectable destinations -- they are
    # only ever reached through the rare random interrupt inside game_hunt.
    hunting_grounds = db.execute(
        "SELECT * FROM hunting_grounds WHERE is_hidden = 0 ORDER BY min_level"
    ).fetchall()
    admin_monsters = []
    if session.get("is_admin"):
        # Deliberately NOT filtered by is_hidden -- admin must be able to
        # force-fight 陰陽尊者/混沌天尊 at any time, so the 秘境 monsters stay in
        # this dropdown and are just labelled distinctly.
        rows = db.execute(
            """SELECT monsters.*, hunting_grounds.name AS ground_name,
                      hunting_grounds.is_hidden AS ground_is_hidden
               FROM monsters JOIN hunting_grounds ON hunting_grounds.id = monsters.hunting_ground_id
               ORDER BY hunting_grounds.min_level, monsters.is_boss, monsters.is_guardian, monsters.level_min"""
        ).fetchall()
        for m in rows:
            if m["ground_is_hidden"]:
                level_label = "秘境"
            elif m["is_boss"]:
                level_label = "首領"
            elif m["is_guardian"]:
                level_label = "守衛怪"
            else:
                level_label = f"Lv{m['level_min']}-{m['level_max']}"
            ground_name = m["ground_name"]
            if m["ground_is_hidden"]:
                ground_name = f"[隱藏] {ground_name}"
            admin_monsters.append({
                "id": m["id"], "name": m["name"], "ground_name": ground_name,
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

    # 天下武道大會 signup panel: only shown standing on a fortress tile (any
    # country's -- see tournament_register), while the open cycle is still
    # accepting entries, and only if this character hasn't already entered.
    # The route re-validates all of this; this is purely what to render.
    open_tournament = db.execute("SELECT * FROM tournaments ORDER BY id DESC LIMIT 1").fetchone()
    tournament_registration_open = _tournament_registration_open(open_tournament)
    tournament_already_registered = open_tournament is not None and db.execute(
        "SELECT id FROM tournament_registrations WHERE tournament_id = ? AND character_id = ?",
        (open_tournament["id"], character["character_id"]),
    ).fetchone() is not None
    tournament_deadline_label = (
        _taipei_time_label(open_tournament["registration_deadline_at"]) if open_tournament else "-"
    )

    # 重大事件 sidebar: a curated subset of activity_log (see
    # _major_event_feed) -- new players joining a country, 秘境 loot drops,
    # and 天下武道大會 champions. Global across every player, not scoped to
    # this character, so it's read here rather than filtered by user_id.
    major_event_rows = db.execute(
        """SELECT action, detail, username, created_at FROM activity_log
           WHERE action IN ('character_create', 'hidden_loot_drop', 'tournament_champion')
           ORDER BY created_at DESC LIMIT 100"""
    ).fetchall()
    major_events = _major_event_feed(major_event_rows)
    db.close()

    # King war-defense bonus: character["character_id"] is the character's
    # own id (character["id"] is shadowed to the COUNTRY's id by the bare
    # countries.* join above -- see the 貢獻值 donate_cap note further down).
    is_reigning_king = character["character_id"] == character["king_character_id"]
    king_war_defense_bonus = is_reigning_king and _in_any_war_window(settings)
    morale_buff_active = _morale_buff_active(character)
    stats = character_final_stats(character, equipped_items, settings, king_war_defense_bonus, morale_buff_active)
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
    # Extra bottom padding: game.html overlays the map legend as an absolutely
    # positioned box in the map panel's bottom-left corner, which otherwise
    # sits on top of the lowest row of hexes. Reserving this much viewBox
    # space below the actual hex content pushes the legend into empty space
    # instead of covering tiles.
    legend_padding = HEX_SIZE * 2.4
    min_x, max_x = min(xs) - padding, max(xs) + padding
    min_y, max_y = min(ys) - padding, max(ys) + legend_padding

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
        tournament_registration_open=tournament_registration_open,
        tournament_already_registered=tournament_already_registered,
        tournament_fee=settings["tournament_registration_fee"],
        tournament_deadline_label=tournament_deadline_label,
        major_events=major_events,
        chat_message_max_len=CHAT_MESSAGE_MAX_LEN,
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
        # is_hidden = 0 mirrors _render_game's dropdown filter: a 秘境 is never
        # a selectable destination, so a hand-crafted POST naming one must be
        # rejected outright rather than silently farming the best PvE reward
        # in the game on demand. The only two legitimate ways into a hidden
        # ground are the trigger roll below and the admin forced-monster path
        # above, both of which bypass this lookup entirely.
        ground = db.execute(
            "SELECT * FROM hunting_grounds WHERE id = ? AND is_hidden = 0",
            (request.form.get("ground_id", ""),),
        ).fetchone()
    if ground is None:
        db.close()
        flash("請選擇一個有效的打怪場")
        return redirect(url_for("game.game"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    # King war-defense bonus: character["character_id"] is the hunter's own
    # id (character["id"] is shadowed to the country's id by the bare
    # countries.* join in this route's SELECT above).
    is_reigning_king = character["character_id"] == character["king_character_id"]
    king_war_defense_bonus = is_reigning_king and _in_any_war_window(settings)
    stats = character_final_stats(character, equipped_items, settings, king_war_defense_bonus)
    current_hp, current_mp = _current_hp_mp(character, stats)
    special_effects = character_special_effects(equipped_items)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game.game"))

    # 秘境 interrupt: rolled BEFORE any guardian/regular selection, and only
    # when the player is hunting normally (never on an admin forced fight).
    # A hit replaces the chosen ground AND its monster wholesale for the rest
    # of this request -- rewards, battle, template and log all read the hidden
    # ground's row from here on.
    hidden_key = None
    if forced_monster is None:
        hidden_key, hidden_ground, hidden_monster = _roll_hidden_encounter(db, settings)
        if hidden_key is not None:
            ground, forced_monster = hidden_ground, hidden_monster
    elif ground["is_hidden"]:
        # Admin force-picked one of the 秘境 monsters -- same fight, same
        # banner, same drop roll, just deliberately rather than by luck.
        hidden_key = _hidden_key_for_ground(ground)

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

    # 怪物弱化: the monster actually fought is a scaled-down copy; every
    # reward below still reads the ORIGINAL row's currency_reward/exp_reward,
    # so weakening a monster never also shrinks its payout.
    fought_monster = _debuffed_monster(monster, special_effects)

    usable_skills = _character_usable_skills(db, character)
    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, fought_monster,
        player_mp=current_mp, usable_skills=usable_skills,
        player_independent_damage_percent=special_effects.get("independent_damage", 0),
    )

    exp_gain = 0
    currency_gain = 0
    currency_lost = 0
    new_level, new_exp = character["level"], character["exp"]
    stat_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    pending_boss_id = None
    boss_room_available = False
    skill_book_dropped = None
    hidden_loot_dropped = None
    if result["won"]:
        if is_guardian_fight:
            exp_multiplier = settings["guardian_exp_multiplier"]
        elif monster["is_boss"]:
            exp_multiplier = settings["boss_exp_multiplier"]
        else:
            # A 秘境 monster is neither boss nor guardian, so it takes no
            # multiplier at all -- its raw exp_reward is already the best in
            # the game by design.
            exp_multiplier = 1.0
        exp_gain = round(
            monster["exp_reward"] * exp_multiplier * (1 + special_effects.get("exp_rate", 0) / 100)
        )
        currency_gain = round(
            monster["currency_reward"]
            * (1 + gold_luk_bonus_pct(stats["luk"]) / 100 + special_effects.get("gold_rate", 0) / 100)
        )
        new_level, new_exp, stat_gain = apply_exp(
            character["level"], character["exp"], exp_gain, settings,
            force_one=session.get("is_admin", False),
            job_class=character["job_class"], job_tier=character["job_tier"],
        )
        new_currency = character["currency"] + currency_gain
        # No 魔王房間 chain ever follows a 秘境 fight: a hidden ground holds a
        # single monster and no boss at all, so `boss` is None there anyway --
        # the explicit hidden_key guard just makes that intent unmissable.
        if hidden_key is None and is_guardian_fight and boss is not None and result["player_hp"] > 0:
            boss_room_available = True
            pending_boss_id = boss["id"]
        if hidden_key is not None:
            hidden_loot_dropped = _roll_hidden_loot(
                db, settings, hidden_key, character["character_id"],
            )
            if hidden_loot_dropped is not None:
                log_activity(
                    db, session["user_id"], session["username"], "hidden_loot_drop",
                    detail=f"{ground['name']}：{hidden_loot_dropped['name']}"
                           f"（{hidden_loot_dropped['hidden_set_name']}）",
                    ip_address=request.remote_addr,
                )
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
    if hidden_key is not None:
        outcome_detail = f"[秘境] {outcome_detail}"
    log_activity(
        db, session["user_id"], session["username"], "hunt",
        detail=f"{ground['name']} {outcome_detail}", ip_address=request.remote_addr,
    )

    if new_level > character["level"]:
        updated = dict(character)
        updated["level"] = new_level
        for stat in ("hp", "mp", "str", "def", "agi", "luk"):
            updated[f"level_bonus_{stat}"] = character[f"level_bonus_{stat}"] + stat_gain[stat]
        stats_after = character_final_stats(updated, equipped_items, settings, king_war_defense_bonus)
    else:
        stats_after = None

    db.commit()
    db.close()

    return render_template(
        "battle.html",
        ground=ground,
        # The DEBUFFED copy is what the player actually fought, so the
        # "敵方" stat panel must show those numbers, not the pristine row's.
        monster=fought_monster,
        guardian_encounter=is_guardian_fight,
        hidden_encounter_banner=(
            HIDDEN_GROUND_TIERS[hidden_key]["banner"] if hidden_key is not None else None
        ),
        hidden_loot_dropped=hidden_loot_dropped,
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
    special_effects = character_special_effects(equipped_items)

    if current_hp <= 0:
        db.execute(
            "UPDATE characters SET pending_boss_monster_id = NULL WHERE id = ?", (character["character_id"],)
        )
        db.commit()
        db.close()
        flash("HP 已耗盡，無法挑戰魔王，請先回到要塞回復")
        return redirect(url_for("game.game"))

    fought_boss = _debuffed_monster(boss, special_effects)
    usable_skills = _character_usable_skills(db, character)
    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, fought_boss,
        player_mp=current_mp, usable_skills=usable_skills,
        player_independent_damage_percent=special_effects.get("independent_damage", 0),
    )

    exp_gain = 0
    currency_gain = 0
    currency_lost = 0
    new_level, new_exp = character["level"], character["exp"]
    stat_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    if result["won"]:
        exp_gain = round(
            boss["exp_reward"] * settings["boss_exp_multiplier"]
            * (1 + special_effects.get("exp_rate", 0) / 100)
        )
        currency_gain = round(
            boss["currency_reward"]
            * (1 + gold_luk_bonus_pct(stats["luk"]) / 100 + special_effects.get("gold_rate", 0) / 100)
        )
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
        monster=fought_boss,
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


def _resolve_bandit_conquest(
    db, character, settings, stats, current_hp, current_mp, independent_damage_percent=0,
):
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
        player_independent_damage_percent=independent_damage_percent,
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
               SET country_id = ?, tile_type = 'town', mayor_character_id = ?, bandit_hp = NULL,
                   defense_reduction_percent = 0
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
                  map_tiles.defense_reduction_percent,
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
                  war_fortress_weekday, war_fortress_start_time, war_fortress_end_time,
                  king_war_defense_bonus_percent, morale_buff_bonus_percent
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
    # King war-defense bonus for the ATTACKER: character["character_id"] is
    # the attacker's own id (character["id"] is shadowed to the country's id
    # by the bare countries.* join above). The war-window gate above already
    # guarantees we're inside the window matching this tile's kind, so "is
    # the reigning king" is the only remaining condition to check here.
    attacker_is_reigning_king = character["character_id"] == character["king_character_id"]
    attacker_morale_buff_active = _morale_buff_active(character)
    stats = character_final_stats(
        character, equipped_items, settings, attacker_is_reigning_king, attacker_morale_buff_active,
    )
    current_hp, current_mp = _current_hp_mp(character, stats)
    # 獨立傷害 is a genuine equipped-gear stat, so unlike the four hunt-scoped
    # effects it works in every kind of fight -- bandit lord, garrison duel
    # and NPC tower alike. The other four are deliberately NOT read here.
    attacker_independent_damage = character_special_effects(equipped_items).get(
        "independent_damage", 0
    )

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法戰鬥，請先回到要塞回復")
        return redirect(url_for("game.game"))

    if own_garrison is not None:
        db.execute("DELETE FROM garrisons WHERE id = ?", (own_garrison["id"],))

    if is_neutral_target:
        return _resolve_bandit_conquest(
            db, character, settings, stats, current_hp, current_mp, attacker_independent_damage,
        )

    defending_country = db.execute(
        "SELECT * FROM countries WHERE id = ?", (character["tile_country_id"],)
    ).fetchone()
    tile_name = tile_display_name(character["tile_name"], character["tile_type"])

    # LIFO defender queue: the most recently-stationed garrison at this tile
    # is fought first. Only once every garrisoned defender is cleared does an
    # attack action reach the tile's NPC defense tower.
    #
    # Exception (explicit user requirement): the reigning King is always the
    # LAST player defender fought at any of his own country's tiles,
    # regardless of when he stationed relative to everyone else -- he never
    # takes a turn in the ordinary stationed_at-DESC rotation. The first
    # ORDER BY key sorts non-king garrisons (0) before the king's own row
    # (1) when both are present; stationed_at DESC is only the tiebreaker
    # among the non-king rows, same LIFO behaviour as before. countries.*
    # here is always the DEFENDING country (joined off the garrisoned
    # character's own country_id, which can only be a tile that country
    # owns), so countries.king_character_id correctly identifies its own
    # king without any cross-country ambiguity.
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
           ORDER BY (characters.id = countries.king_character_id) ASC, garrisons.stationed_at DESC
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
        # Same king war-defense bonus, for the DEFENDER this time: the
        # war-window gate above already guarantees we're inside the window
        # matching this tile's kind, so only "is the reigning king" matters.
        # defender_row["defender_id"] is the defender's own id (its "id" is
        # shadowed to the country's id by the bare countries.* join above).
        defender_is_reigning_king = defender_row["defender_id"] == defender_row["king_character_id"]
        defender_morale_buff_active = _morale_buff_active(defender_row)
        defender_stats = character_final_stats(
            defender_row, defender_equipped_items, settings, defender_is_reigning_king,
            defender_morale_buff_active,
        )
        defender_monster = {
            "name": defender_row["defender_name"],
            "hp": defender_stats["hp"], "atk": defender_stats["str"],
            "def": defender_stats["def"], "agi": defender_stats["agi"],
            "element": defender_row["element"],
        }

        result = run_battle(
            character["character_name"], stats, character["element"], current_hp, defender_monster,
            player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
            player_independent_damage_percent=attacker_independent_damage,
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
    tower = defense_tower_stats(
        defending_country, character["tile_type"], settings, character["defense_reduction_percent"],
    )

    result = run_battle(
        character["character_name"], stats, character["element"], current_hp, tower,
        player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
        player_independent_damage_percent=attacker_independent_damage,
    )

    currency_lost = 0
    if result["won"]:
        new_currency = character["currency"]
        db.execute(
            "UPDATE map_tiles SET country_id = ?, mayor_character_id = ?, defense_reduction_percent = 0 "
            "WHERE id = ?",
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
    # 回復費用折扣 from a fully-equipped 秘境 水 set. Applied to the computed
    # bill before anything is charged, so both the affordability check below
    # and the treasury credit further down see the discounted number.
    recovery_discount = character_special_effects(equipped_items).get("recovery_discount", 0)
    if recovery_discount:
        cost = max(0, round(cost * (1 - recovery_discount / 100)))
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

    # hidden_set_key IS NULL is what makes the 30 秘境 legendary pieces
    # unpurchasable: they are ordinary weapon/armor/accessory rows in every
    # other respect (they have to be, since shop_type doubles as the equip
    # slot key), so this single listing query is the one place that decides
    # what can be bought at all.
    all_items = db.execute(
        """SELECT items.*, countries.name AS set_country_name
           FROM items LEFT JOIN countries ON countries.id = items.country_id
           WHERE items.hidden_set_key IS NULL
             AND (items.country_id IS NULL OR items.country_id = ?)
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

    # hidden_set_key IS NULL mirrors game_shop's listing filter: the 秘境
    # pieces are never rendered as buyable, and (since they are priced 0) a
    # hand-crafted POST must not be able to mint one for free either.
    placeholders = ",".join("?" for _ in item_ids)
    items = db.execute(
        f"SELECT * FROM items WHERE id IN ({placeholders}) AND hidden_set_key IS NULL", item_ids
    ).fetchall()
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
        "SELECT id, country_id, income_claimed_at, siege_attack_next_at FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()
    own_character_id = own_character["id"]
    own_country = next((c for c in countries if c["id"] == own_character["country_id"]), None)
    is_own_king = own_country is not None and own_character_id == own_country["king_character_id"]
    is_own_advisor = own_country is not None and own_character_id == own_country["advisor_character_id"]
    is_own_general = own_country is not None and own_character_id == own_country["general_character_id"]
    is_own_official = is_own_advisor or is_own_general
    is_officer = is_own_king or is_own_official
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

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    in_any_war_window = _in_any_war_window(settings)

    now_taipei = _taipei_now()
    is_friday = now_taipei.isoweekday() == 5
    already_claimed_today = False
    if own_character["income_claimed_at"]:
        claimed_dt = _parse_dt(own_character["income_claimed_at"])
        already_claimed_today = claimed_dt is not None and claimed_dt.date() == now_taipei.date()

    # 士氣激勵 (Advisor's morale buff): "already used this window" is detected
    # by comparing the country's persisted morale_buff_expires_at against the
    # CURRENT window's end (recomputed fresh every request) -- see
    # _current_war_window_end/country_cast_morale_buff for why this single
    # column serves double duty as both "currently active" and "already
    # consumed for this window occurrence".
    morale_buff_cost = settings["morale_buff_cost"]
    morale_buff_currently_active = own_country is not None and _morale_buff_active(own_country)
    morale_buff_already_used_this_window = False
    current_window_end = _current_war_window_end(settings, now_taipei)
    if own_country is not None and current_window_end is not None:
        window_end_str = (
            current_window_end.astimezone(timezone.utc)
        ).replace(tzinfo=None).strftime(ACTION_DT_FORMAT)
        morale_buff_already_used_this_window = own_country["morale_buff_expires_at"] == window_end_str

    # 攻城武器 (General's siege attack): target list is every OTHER country's
    # fortress/town tile (never neutral, never the viewer's own country).
    siege_attack_cost = settings["siege_attack_cost"]
    siege_attack_cooldown_seconds = (
        _cooldown_remaining_seconds(own_character["siege_attack_next_at"]) if is_own_general else 0
    )
    siege_attack_cooldown_label = (
        _format_duration(siege_attack_cooldown_seconds) if siege_attack_cooldown_seconds > 0 else None
    )
    enemy_target_tiles = []
    if is_own_general and own_country is not None:
        tile_rows = db.execute(
            """SELECT map_tiles.id AS tile_id, map_tiles.name, map_tiles.tile_type,
                      map_tiles.defense_reduction_percent, countries.name AS country_name
               FROM map_tiles JOIN countries ON countries.id = map_tiles.country_id
               WHERE map_tiles.tile_type IN ('fortress', 'town') AND map_tiles.country_id != ?
               ORDER BY countries.name, map_tiles.tile_type DESC, map_tiles.name""",
            (own_country["id"],),
        ).fetchall()
        enemy_target_tiles = [
            {
                "id": r["tile_id"],
                "name": tile_display_name(r["name"], r["tile_type"]),
                "country_name": r["country_name"],
                "defense_reduction_percent": r["defense_reduction_percent"],
            }
            for r in tile_rows
        ]

    # 修補防禦 (King/Advisor/General repair): only the viewer's own country's
    # currently-damaged fortress/town tiles.
    defense_repair_cost_per_percent = settings["defense_repair_cost_per_percent"]
    damaged_own_tiles = []
    if is_officer and own_country is not None:
        tile_rows = db.execute(
            """SELECT id, name, tile_type, defense_reduction_percent
               FROM map_tiles
               WHERE country_id = ? AND tile_type IN ('fortress', 'town') AND defense_reduction_percent > 0
               ORDER BY tile_type DESC, name""",
            (own_country["id"],),
        ).fetchall()
        damaged_own_tiles = [
            {
                "id": r["id"],
                "name": tile_display_name(r["name"], r["tile_type"]),
                "defense_reduction_percent": r["defense_reduction_percent"],
                "repair_cost": r["defense_reduction_percent"] * defense_repair_cost_per_percent,
            }
            for r in tile_rows
        ]

    # Authorized-challenger banner: search across ALL countries (not just the
    # viewer's own) for a pending_challenge_character_id matching the viewer
    # -- the king who authorized them may belong to any country.
    authorized_challenge = None
    for c in countries:
        if c["pending_challenge_character_id"] == own_character_id:
            authorized_challenge = {
                "country_name": c["name"],
                "seat": c["pending_challenge_seat"],
                "seat_label": "參謀" if c["pending_challenge_seat"] == "advisor" else "大將軍",
            }
            break

    db.close()

    rows = []
    for c in countries:
        is_own = own_country is not None and c["id"] == own_country["id"]
        pending_challenge = None
        if c["pending_challenge_seat"]:
            pending_challenge = {
                "seat": c["pending_challenge_seat"],
                "seat_label": "參謀" if c["pending_challenge_seat"] == "advisor" else "大將軍",
                "holder_name": character_names.get(c["pending_challenge_character_id"]),
            }
        rows.append({
            "id": c["id"],
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
            "is_own_country": is_own,
            "is_own_king": is_own and is_own_king,
            "pending_challenge": pending_challenge,
        })

    return render_template(
        "countries.html", countries=rows, roles=GOVERNMENT_ROLES,
        is_own_king=is_own_king,
        is_own_official=is_own_official,
        is_own_advisor=is_own_advisor,
        is_own_general=is_own_general,
        is_officer=is_officer,
        in_any_war_window=in_any_war_window,
        is_friday=is_friday,
        already_claimed_today=already_claimed_today,
        authorized_challenge=authorized_challenge,
        morale_buff_cost=morale_buff_cost,
        morale_buff_currently_active=morale_buff_currently_active,
        morale_buff_already_used_this_window=morale_buff_already_used_this_window,
        siege_attack_cost=siege_attack_cost,
        siege_attack_cooldown_seconds=siege_attack_cooldown_seconds,
        siege_attack_cooldown_label=siege_attack_cooldown_label,
        enemy_target_tiles=enemy_target_tiles,
        damaged_own_tiles=damaged_own_tiles,
    )


# ---------------------------------------------------------------------------
# 篡位 (king usurpation), 官職挑戰 (advisor/general challenge authorization),
# and 每週分潤 (weekly treasury income claim). Added next to countries_page
# above since that's already where /countries lives -- no new blueprint.
# ---------------------------------------------------------------------------

@game_bp.route("/country/challenge_king", methods=["POST"])
@character_required
def country_challenge_king():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.country_id, characters.next_action_at,
                  characters.current_hp, characters.current_mp, characters.currency, characters.level,
                  characters.name AS character_name,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.countries_page"))

    if character["king_character_id"] is None:
        db.close()
        flash("這個國家目前沒有國王，無法篡位")
        return redirect(url_for("game.countries_page"))

    if character["king_character_id"] == character["character_id"]:
        db.close()
        flash("你已經是這個國家的國王了")
        return redirect(url_for("game.countries_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    # Opposite polarity from game_conquer's war-window gate: usurpation is
    # only allowed OUTSIDE both the town and fortress war windows.
    if _in_any_war_window(settings):
        db.close()
        flash("國戰或城鎮戰期間無法篡位")
        return redirect(url_for("game.countries_page"))

    king_row = db.execute(
        """SELECT characters.id AS defender_id, characters.name AS defender_name, characters.level,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.id = ?""",
        (character["king_character_id"],),
    ).fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    challenger_stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, challenger_stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法發動篡位挑戰，請先回到要塞回復")
        return redirect(url_for("game.countries_page"))

    # King war-defense bonus intentionally left OFF here (default False):
    # this route's own gate above already requires being outside BOTH war
    # windows before a fight can happen at all, so the bonus's "inside a war
    # window" condition can never be true for this battle -- wiring it here
    # would just be permanently-dead code.
    king_equipped_items = _fetch_equipped_items(db, king_row)
    king_stats = character_final_stats(king_row, king_equipped_items, settings)
    king_monster = {
        "name": king_row["defender_name"],
        "hp": king_stats["hp"], "atk": king_stats["str"],
        "def": king_stats["def"], "agi": king_stats["agi"],
        "element": king_row["element"],
    }

    result = run_battle(
        character["character_name"], challenger_stats, character["element"], current_hp, king_monster,
        player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
        player_independent_damage_percent=character_special_effects(equipped_items).get(
            "independent_damage", 0
        ),
    )

    country_id = character["country_id"]
    country_name = character["name"]  # bare countries.* -> "name" is the country's own name here
    currency_lost = 0
    if result["won"]:
        new_currency = character["currency"]
        # One-seat-per-player: vacate any seat this challenger already holds
        # ANYWHERE ELSE first -- a sitting Advisor/General winning a King
        # challenge is a promotion, not a block, but they can't hold two
        # seats at once.
        for seat_col in ("king_character_id", "advisor_character_id", "general_character_id"):
            db.execute(
                f"UPDATE countries SET {seat_col} = NULL WHERE {seat_col} = ?",
                (character["character_id"],),
            )
        db.execute(
            """UPDATE countries
               SET king_character_id = ?, pending_challenge_seat = NULL,
                   pending_challenge_character_id = NULL, pending_challenge_authorized_at = NULL
               WHERE id = ?""",
            (character["character_id"], country_id),
        )
        outcome_detail = f"擊敗國王{king_row['defender_name']}，篡位成功，成為「{country_name}」新任國王"
        flash(f"篡位成功！你已成為「{country_name}」的新任國王")
    elif result["timed_out"]:
        new_currency = character["currency"]
        outcome_detail = "篡位挑戰回合已滿，未分勝負，沒有任何諸神幣損失"
        flash("篡位挑戰未分勝負（戰鬥回合已滿），沒有任何諸神幣損失")
    else:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
        db.execute(
            "UPDATE countries SET treasury = treasury + ? WHERE id = ?", (currency_lost, country_id)
        )
        outcome_detail = f"篡位失敗，身上 {currency_lost} 諸神幣被沒入「{country_name}」國庫"
        flash(f"篡位失敗，身上 {currency_lost} 諸神幣被沒入國庫")

    db.execute(
        """UPDATE characters
           SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               pvp_battles_count = pvp_battles_count + 1, pvp_wins_count = pvp_wins_count + ?,
               pending_boss_monster_id = NULL
           WHERE id = ?""",
        (
            new_currency, result["player_hp"], result["player_mp"], _next_action_at(settings["turn_wait_seconds"]),
            1 if result["won"] else 0, character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "challenge_king",
        detail=outcome_detail, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    return redirect(url_for("game.countries_page"))


@game_bp.route("/country/authorize_challenge", methods=["POST"])
@character_required
def country_authorize_challenge():
    db = get_db()
    character = db.execute(
        "SELECT id, country_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    country = db.execute("SELECT * FROM countries WHERE id = ?", (character["country_id"],)).fetchone()

    if country is None or country["king_character_id"] != character["id"]:
        db.close()
        flash("只有國王可以指派挑戰者")
        return redirect(url_for("game.countries_page"))

    seat = request.form.get("seat", "").strip()
    if seat not in ("advisor", "general"):
        db.close()
        flash("請選擇有效的官職（參謀或大將軍）")
        return redirect(url_for("game.countries_page"))

    if country["pending_challenge_seat"] is not None:
        db.close()
        flash("目前已經有授權中的挑戰者，請先收回授權")
        return redirect(url_for("game.countries_page"))

    target_name = request.form.get("target_name", "").strip()
    if not target_name:
        db.close()
        flash("請輸入要授權挑戰的角色名稱")
        return redirect(url_for("game.countries_page"))

    target = db.execute(
        "SELECT id, name FROM characters WHERE lower(name) = lower(?)", (target_name,)
    ).fetchone()
    if target is None:
        db.close()
        flash("找不到這個角色")
        return redirect(url_for("game.countries_page"))

    if target["id"] == country["king_character_id"]:
        db.close()
        flash("不能授權國王本人挑戰自己國家的官職")
        return redirect(url_for("game.countries_page"))

    seat_label = "參謀" if seat == "advisor" else "大將軍"
    db.execute(
        """UPDATE countries
           SET pending_challenge_seat = ?, pending_challenge_character_id = ?, pending_challenge_authorized_at = ?
           WHERE id = ?""",
        (seat, target["id"], datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), country["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "authorize_challenge",
        detail=f"授權 {target['name']} 挑戰{seat_label}之位", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已授權 {target['name']} 挑戰{seat_label}之位")
    return redirect(url_for("game.countries_page"))


@game_bp.route("/country/revoke_challenge", methods=["POST"])
@character_required
def country_revoke_challenge():
    db = get_db()
    character = db.execute(
        "SELECT id, country_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    country = db.execute("SELECT * FROM countries WHERE id = ?", (character["country_id"],)).fetchone()

    if country is None or country["king_character_id"] != character["id"]:
        db.close()
        flash("只有國王可以收回授權")
        return redirect(url_for("game.countries_page"))

    if country["pending_challenge_seat"] is None:
        db.close()
        flash("目前沒有授權中的挑戰者")
        return redirect(url_for("game.countries_page"))

    db.execute(
        """UPDATE countries
           SET pending_challenge_seat = NULL, pending_challenge_character_id = NULL,
               pending_challenge_authorized_at = NULL
           WHERE id = ?""",
        (country["id"],),
    )
    log_activity(
        db, session["user_id"], session["username"], "revoke_challenge", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("已收回挑戰授權")
    return redirect(url_for("game.countries_page"))


@game_bp.route("/country/challenge_official", methods=["POST"])
@character_required
def country_challenge_official():
    db = get_db()
    character = db.execute(
        """SELECT characters.id AS character_id, characters.country_id, characters.next_action_at,
                  characters.current_hp, characters.current_mp, characters.currency, characters.level,
                  characters.name AS character_name,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()

    if _cooldown_remaining_seconds(character["next_action_at"]) > 0:
        db.close()
        flash("還在冷卻中，請稍候再行動")
        return redirect(url_for("game.countries_page"))

    target_country = db.execute(
        "SELECT * FROM countries WHERE pending_challenge_character_id = ?", (character["character_id"],)
    ).fetchone()
    if target_country is None:
        db.close()
        flash("你目前沒有被授權的挑戰資格")
        return redirect(url_for("game.countries_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    if _in_any_war_window(settings):
        db.close()
        flash("國戰或城鎮戰期間無法應戰")
        return redirect(url_for("game.countries_page"))

    seat = target_country["pending_challenge_seat"]
    seat_column = "advisor_character_id" if seat == "advisor" else "general_character_id"
    seat_label = "參謀" if seat == "advisor" else "大將軍"
    officeholder_id = target_country[seat_column]

    if officeholder_id is None:
        # Shouldn't normally happen (every seat always has an NPC or player
        # in it), but guard anyway rather than crash on a NULL id lookup.
        db.execute(
            """UPDATE countries
               SET pending_challenge_seat = NULL, pending_challenge_character_id = NULL,
                   pending_challenge_authorized_at = NULL
               WHERE id = ?""",
            (target_country["id"],),
        )
        db.commit()
        db.close()
        flash(f"「{target_country['name']}」的{seat_label}目前出缺，授權已被清除")
        return redirect(url_for("game.countries_page"))

    officeholder_row = db.execute(
        """SELECT characters.id AS defender_id, characters.name AS defender_name, characters.level,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  characters.equipped_weapon_id, characters.equipped_armor_id, characters.equipped_accessory_id,
                  countries.*
           FROM characters JOIN countries ON countries.id = characters.country_id
           WHERE characters.id = ?""",
        (officeholder_id,),
    ).fetchone()

    equipped_items = _fetch_equipped_items(db, character)
    base_stats = character_final_stats(character, equipped_items, settings)
    current_hp, current_mp = _current_hp_mp(character, base_stats)

    if current_hp <= 0:
        db.close()
        flash("HP 已耗盡，無法應戰，請先回到要塞回復")
        return redirect(url_for("game.countries_page"))

    # Office-challenge aura: a one-off +office_challenge_aura_bonus_percent%
    # multiply applied to ALL SIX stats, for THIS fight only -- deliberately
    # a local post-hoc multiply on the already-computed dict rather than
    # threaded into compute_final_stats' permanent-bonus formula. run_battle
    # never actually reads player_stats["hp"]/["mp"] (only the separate
    # player_hp/player_mp args below, which stay at the character's real
    # persisted current HP/MP so a one-fight buff never inflates their real
    # stored HP ceiling) -- hp/mp are still scaled here for consistency with
    # the "all six stats" spec even though they're inert for this battle.
    aura_pct = settings["office_challenge_aura_bonus_percent"]
    challenger_stats = {key: round(value * (1 + aura_pct / 100)) for key, value in base_stats.items()}

    # King war-defense bonus intentionally left OFF for the officeholder here
    # (default False): the one-seat-per-player rule means an Advisor/General
    # can never simultaneously be a King, and this route's own gate above
    # already requires being outside both war windows anyway -- the bonus
    # condition is doubly unreachable in this route.
    defender_equipped_items = _fetch_equipped_items(db, officeholder_row)
    defender_stats = character_final_stats(officeholder_row, defender_equipped_items, settings)
    defender_monster = {
        "name": officeholder_row["defender_name"],
        "hp": defender_stats["hp"], "atk": defender_stats["str"],
        "def": defender_stats["def"], "agi": defender_stats["agi"],
        "element": officeholder_row["element"],
    }

    result = run_battle(
        character["character_name"], challenger_stats, character["element"], current_hp, defender_monster,
        player_mp=current_mp, usable_skills=_character_usable_skills(db, character),
        player_independent_damage_percent=character_special_effects(equipped_items).get(
            "independent_damage", 0
        ),
    )

    country_id = target_country["id"]
    country_name = target_country["name"]
    currency_lost = 0
    if result["won"]:
        new_currency = character["currency"]
        for seat_col in ("king_character_id", "advisor_character_id", "general_character_id"):
            db.execute(
                f"UPDATE countries SET {seat_col} = NULL WHERE {seat_col} = ?",
                (character["character_id"],),
            )
        db.execute(
            f"""UPDATE countries
                SET {seat_column} = ?, pending_challenge_seat = NULL,
                    pending_challenge_character_id = NULL, pending_challenge_authorized_at = NULL
                WHERE id = ?""",
            (character["character_id"], country_id),
        )
        outcome_detail = f"擊敗{officeholder_row['defender_name']}，成功奪得「{country_name}」{seat_label}之位"
        flash(f"應戰成功！你已成為「{country_name}」的{seat_label}")
    elif result["timed_out"]:
        # Not explicitly specced (spec only describes WIN/LOSS) -- treated
        # like every other timed_out fight in this codebase: a true no-op,
        # so the authorization is deliberately left in place (unconsumed)
        # rather than cleared, letting the challenger simply try again once
        # their cooldown expires.
        new_currency = character["currency"]
        outcome_detail = "應戰回合已滿，未分勝負，沒有任何諸神幣損失，授權依然有效"
        flash("應戰未分勝負（戰鬥回合已滿），沒有任何諸神幣損失，授權依然有效，可再次應戰")
    else:
        currency_lost = character["currency"] // 2
        new_currency = character["currency"] - currency_lost
        db.execute("UPDATE countries SET treasury = treasury + ? WHERE id = ?", (currency_lost, country_id))
        db.execute(
            """UPDATE countries
               SET pending_challenge_seat = NULL, pending_challenge_character_id = NULL,
                   pending_challenge_authorized_at = NULL
               WHERE id = ?""",
            (country_id,),
        )
        outcome_detail = f"應戰失敗，身上 {currency_lost} 諸神幣被沒入「{country_name}」國庫，授權已消耗"
        flash(f"應戰失敗，身上 {currency_lost} 諸神幣被沒入國庫，授權已消耗")

    db.execute(
        """UPDATE characters
           SET currency = ?, current_hp = ?, current_mp = ?, next_action_at = ?,
               pvp_battles_count = pvp_battles_count + 1, pvp_wins_count = pvp_wins_count + ?,
               pending_boss_monster_id = NULL
           WHERE id = ?""",
        (
            new_currency, result["player_hp"], result["player_mp"], _next_action_at(settings["turn_wait_seconds"]),
            1 if result["won"] else 0, character["character_id"],
        ),
    )
    log_activity(
        db, session["user_id"], session["username"], "challenge_official",
        detail=outcome_detail, ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    return redirect(url_for("game.countries_page"))


@game_bp.route("/country/claim_income", methods=["POST"])
@character_required
def country_claim_income():
    db = get_db()
    character = db.execute(
        "SELECT id, country_id, currency, income_claimed_at FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()

    now = _taipei_now()
    if now.isoweekday() != 5:
        db.close()
        flash("只有每週五才能領取國庫分潤")
        return redirect(url_for("game.countries_page"))

    country = db.execute("SELECT * FROM countries WHERE id = ?", (character["country_id"],)).fetchone()
    if country is not None and character["id"] == country["king_character_id"]:
        role = "king"
    elif country is not None and character["id"] in (
        country["advisor_character_id"], country["general_character_id"],
    ):
        role = "official"
    else:
        db.close()
        flash("你目前不是王室成員")
        return redirect(url_for("game.countries_page"))

    if character["income_claimed_at"]:
        claimed_dt = _parse_dt(character["income_claimed_at"])
        if claimed_dt is not None and claimed_dt.date() == now.date():
            db.close()
            flash("這週已經領取過了")
            return redirect(url_for("game.countries_page"))

    settings = db.execute(
        "SELECT king_weekly_income_percent, official_weekly_income_percent FROM game_settings WHERE id = 1"
    ).fetchone()
    percent = (
        settings["king_weekly_income_percent"] if role == "king"
        else settings["official_weekly_income_percent"]
    )
    amount = round(country["treasury"] * percent / 100)

    db.execute("UPDATE countries SET treasury = treasury - ? WHERE id = ?", (amount, country["id"]))
    db.execute(
        "UPDATE characters SET currency = currency + ?, income_claimed_at = ? WHERE id = ?",
        (amount, now.strftime("%Y-%m-%d %H:%M:%S"), character["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "claim_income",
        detail=f"領取本週國庫分潤 {amount} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"已領取本週國庫分潤 {amount} 諸神幣")
    return redirect(url_for("game.countries_page"))


# ---------------------------------------------------------------------------
# 國庫花費三大官職行動: 士氣激勵 (Advisor's morale buff), 攻城武器 (General's
# siege attack) and 修補防禦 (King/Advisor/General repair). All three spend
# the ACTING officer's own country's treasury, none involve run_battle, and
# none touch the shared per-character next_action_at cooldown (siege_attack
# instead uses its own dedicated characters.siege_attack_next_at column).
# ---------------------------------------------------------------------------

@game_bp.route("/country/cast_morale_buff", methods=["POST"])
@character_required
def country_cast_morale_buff():
    db = get_db()
    character = db.execute(
        "SELECT id, country_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    country = db.execute("SELECT * FROM countries WHERE id = ?", (character["country_id"],)).fetchone()

    if country is None or country["advisor_character_id"] != character["id"]:
        db.close()
        flash("只有參謀可以施放士氣激勵")
        return redirect(url_for("game.countries_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    window_end = _current_war_window_end(settings)
    if window_end is None:
        db.close()
        flash("只有在國戰時段才能施放士氣激勵")
        return redirect(url_for("game.countries_page"))

    window_end_str = (window_end.astimezone(timezone.utc)).replace(tzinfo=None).strftime(ACTION_DT_FORMAT)
    if country["morale_buff_expires_at"] == window_end_str:
        db.close()
        flash("本次國戰時段已經施放過士氣激勵，需等下次國戰時段才能再次施放")
        return redirect(url_for("game.countries_page"))

    cost = settings["morale_buff_cost"]
    if country["treasury"] < cost:
        db.close()
        flash(f"國庫諸神幣不足，施放士氣激勵需要 {cost} 諸神幣")
        return redirect(url_for("game.countries_page"))

    db.execute(
        "UPDATE countries SET treasury = treasury - ?, morale_buff_expires_at = ? WHERE id = ?",
        (cost, window_end_str, country["id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "cast_morale_buff",
        detail=f"花費 {cost} 諸神幣施放士氣激勵", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash("士氣激勵已施放，本次國戰時段內全國六圍屬性提升")
    return redirect(url_for("game.countries_page"))


@game_bp.route("/country/siege_attack", methods=["POST"])
@character_required
def country_siege_attack():
    db = get_db()
    character = db.execute(
        "SELECT id, country_id, siege_attack_next_at FROM characters WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()
    country = db.execute("SELECT * FROM countries WHERE id = ?", (character["country_id"],)).fetchone()

    if country is None or country["general_character_id"] != character["id"]:
        db.close()
        flash("只有大將軍可以發動攻城武器")
        return redirect(url_for("game.countries_page"))

    remaining_cooldown = _cooldown_remaining_seconds(character["siege_attack_next_at"])
    if remaining_cooldown > 0:
        db.close()
        flash(f"還在攻城武器冷卻中，還要等 {_format_duration(remaining_cooldown)}")
        return redirect(url_for("game.countries_page"))

    target_tile = db.execute(
        "SELECT id, tile_type, country_id, name, defense_reduction_percent FROM map_tiles WHERE id = ?",
        (request.form.get("target_tile_id", ""),),
    ).fetchone()

    valid_target = (
        target_tile is not None
        and target_tile["tile_type"] in ("fortress", "town")
        and target_tile["country_id"] is not None
        and target_tile["country_id"] != country["id"]
    )
    if not valid_target:
        db.close()
        flash("請選擇一個有效的敵方要塞或城鎮")
        return redirect(url_for("game.countries_page"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    window_kind = _war_window_kind_for_tile_type(target_tile["tile_type"])
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
            f"現在不是國戰時段，無法對{kind_label}發動攻城武器。"
            f"開放時段：{_war_window_label(war_weekday, war_start_time, war_end_time)}（台灣時間）"
        )
        return redirect(url_for("game.countries_page"))

    cost = settings["siege_attack_cost"]
    if country["treasury"] < cost:
        db.close()
        flash(f"國庫諸神幣不足，發動攻城武器需要 {cost} 諸神幣")
        return redirect(url_for("game.countries_page"))

    floor_percent = settings["siege_attack_reduction_floor_percent"]
    new_reduction = min(
        floor_percent,
        target_tile["defense_reduction_percent"] + settings["siege_attack_reduction_percent"],
    )

    db.execute(
        "UPDATE map_tiles SET defense_reduction_percent = ? WHERE id = ?",
        (new_reduction, target_tile["id"]),
    )
    db.execute("UPDATE countries SET treasury = treasury - ? WHERE id = ?", (cost, country["id"]))
    db.execute(
        "UPDATE characters SET siege_attack_next_at = ? WHERE id = ?",
        (_next_action_at(settings["siege_attack_cooldown_seconds"]), character["id"]),
    )
    tile_name = tile_display_name(target_tile["name"], target_tile["tile_type"])
    log_activity(
        db, session["user_id"], session["username"], "siege_attack",
        detail=f"對{tile_name}發動攻城武器，防禦力削弱至剩餘 {100 - new_reduction}%（花費 {cost} 諸神幣）",
        ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"攻城武器發動成功，「{tile_name}」的防禦力已被削弱")
    return redirect(url_for("game.countries_page"))


@game_bp.route("/country/repair_defense", methods=["POST"])
@character_required
def country_repair_defense():
    db = get_db()
    character = db.execute(
        "SELECT id, country_id FROM characters WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    country = db.execute("SELECT * FROM countries WHERE id = ?", (character["country_id"],)).fetchone()

    is_officer = country is not None and character["id"] in (
        country["king_character_id"], country["advisor_character_id"], country["general_character_id"],
    )
    if not is_officer:
        db.close()
        flash("只有國王／參謀／大將軍可以修補防禦")
        return redirect(url_for("game.countries_page"))

    target_tile = db.execute(
        "SELECT id, tile_type, country_id, name, defense_reduction_percent FROM map_tiles WHERE id = ?",
        (request.form.get("target_tile_id", ""),),
    ).fetchone()

    valid_target = (
        target_tile is not None
        and target_tile["tile_type"] in ("fortress", "town")
        and target_tile["country_id"] == country["id"]
    )
    if not valid_target:
        db.close()
        flash("請選擇一個屬於自己國家的要塞或城鎮")
        return redirect(url_for("game.countries_page"))

    if target_tile["defense_reduction_percent"] <= 0:
        db.close()
        flash("這個據點目前沒有需要修補的防禦損傷")
        return redirect(url_for("game.countries_page"))

    settings = db.execute(
        "SELECT defense_repair_cost_per_percent FROM game_settings WHERE id = 1"
    ).fetchone()
    cost = target_tile["defense_reduction_percent"] * settings["defense_repair_cost_per_percent"]
    if country["treasury"] < cost:
        db.close()
        flash(f"國庫諸神幣不足，修補防禦需要 {cost} 諸神幣")
        return redirect(url_for("game.countries_page"))

    db.execute("UPDATE map_tiles SET defense_reduction_percent = 0 WHERE id = ?", (target_tile["id"],))
    db.execute("UPDATE countries SET treasury = treasury - ? WHERE id = ?", (cost, country["id"]))
    tile_name = tile_display_name(target_tile["name"], target_tile["tile_type"])
    log_activity(
        db, session["user_id"], session["username"], "repair_defense",
        detail=f"修補{tile_name}的防禦損傷（花費 {cost} 諸神幣）", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()
    flash(f"「{tile_name}」的防禦已修補完成")
    return redirect(url_for("game.countries_page"))
