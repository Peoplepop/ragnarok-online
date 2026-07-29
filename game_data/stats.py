from db import LEVEL_CAP
from game_data.constants import BASE_STATS, LEVEL_STAT_GROWTH
from game_data.equipment import _equipment_set_bonus, _own_element_bonus
from game_data.jobs import job_stat_bonus_pct


def compute_final_stats(
    country, equipped_items=(), level=1, job_bonus=None, rebirth_bonus_percent=0, level_bonus_stats=None,
    own_element=None,
):
    """level_bonus_stats is the per-character accumulated total from the
    random job-weighted level-up rolls (see _roll_level_up_stat_points) --
    always supplied for real player characters via character_final_stats.
    Left as None here only for NPC-only callers (defense_tower_stats), which
    still fall back to the old flat LEVEL_STAT_GROWTH formula since NPCs are
    computed fresh at a fixed level, not leveled up one roll at a time.

    own_element is the wearer's own country's element (None for callers that
    don't care, e.g. NPC-only callers with no gear). It stacks on top of
    _equipment_set_bonus rather than replacing it -- see _own_element_bonus."""
    job_bonus = job_bonus or {}
    equip_bonus = {}
    for item in equipped_items:
        if item:
            equip_bonus[item["stat"]] = equip_bonus.get(item["stat"], 0) + item["stat_bonus"]
    for stat, amount in _equipment_set_bonus(equipped_items).items():
        equip_bonus[stat] = equip_bonus.get(stat, 0) + amount
    for stat, amount in _own_element_bonus(equipped_items, own_element).items():
        equip_bonus[stat] = equip_bonus.get(stat, 0) + amount
    if level_bonus_stats is None:
        level_bonus = max(0, level - 1)
        level_bonus_stats = {key: LEVEL_STAT_GROWTH[key] * level_bonus for key in BASE_STATS}
    return {
        key: round(base * (1 + (country[bonus_field] + job_bonus.get(key, 0) + rebirth_bonus_percent) / 100))
        + equip_bonus.get(key, 0)
        + level_bonus_stats[key]
        for key, (bonus_field, base) in BASE_STATS.items()
    }


STAT_FLOOR_COLUMNS = {
    "hp": "stat_floor_hp", "mp": "stat_floor_mp", "str": "stat_floor_str",
    "def": "stat_floor_def", "agi": "stat_floor_agi", "luk": "stat_floor_luk",
}


def character_final_stats(character, equipped_items, settings, king_war_defense_bonus=False):
    """Like compute_final_stats, but layers in the character's job-tier bonus
    and stacking rebirth bonus, then clamps every stat to its stat-floor
    snapshot (if any) so a promotion can never make a stat go down. Rebirth
    intentionally bypasses this floor -- see game_rebirth, which clears it.

    king_war_defense_bonus defaults to False so every pre-existing call site
    is completely unaffected; callers pass True only when they've already
    established the character IS the reigning king of their own country AND
    the current moment is inside a war window (town or fortress) -- see the
    office-challenge/usurpation feature. When True, it folds
    game_settings.king_war_defense_bonus_percent into the same additive
    percentage-bonus term job/country/rebirth bonuses already share, applied
    only to def, rather than bolting on a separate multiply pass."""
    job_bonus = job_stat_bonus_pct(character["job_class"], character["job_tier"])
    if king_war_defense_bonus:
        job_bonus = dict(job_bonus)
        job_bonus["def"] = job_bonus.get("def", 0) + settings["king_war_defense_bonus_percent"]
    rebirth_bonus = character["rebirth_count"] * settings["rebirth_stat_bonus_percent"]
    level_bonus_stats = {key: character[f"level_bonus_{key}"] for key in BASE_STATS}
    stats = compute_final_stats(
        character, equipped_items, character["level"], job_bonus, rebirth_bonus, level_bonus_stats,
        own_element=character["element"],
    )
    for key, col in STAT_FLOOR_COLUMNS.items():
        floor = character[col]
        if floor is not None:
            stats[key] = max(stats[key], floor)
    return stats


def defense_tower_stats(country, tile_type, settings):
    """Builds a monster-shaped dict for a town/fortress's defenders, reusing
    compute_final_stats at a fixed high level (no gear) so territory combat
    goes through the exact same run_battle/_combat_hit path as hunting."""
    level = settings["fortress_defense_level"] if tile_type == "fortress" else settings["town_defense_level"]
    s = compute_final_stats(country, [], level)
    return {
        "name": f"{country['name']}守軍",
        "hp": s["hp"], "atk": s["str"], "def": s["def"], "agi": s["agi"],
        "element": country["element"],
    }


# --- Bandit lord (neutral-tile guardian) -----------------------------------
# Every neutral map tile is guarded by the same single fixed NPC -- unlike
# the per-tile generated hunting-ground roster, there's exactly one "山賊領主"
# stat profile reused everywhere. Its atk/def/agi are computed the same way
# defense_tower_stats builds a town/fortress's NPC defenders (compute_final_stats
# at a fixed level, no gear), but with an all-zero bonus dict since a bandit
# isn't aligned to any country -- then only its HP is scaled way up, because
# (unlike every other fight in this game) bandit_hp persists across separate
# /game/conquer actions and never regenerates: depleting it is meant to take
# several repeat attacks, not one lucky roll.
#
# BANDIT_HP_MULTIPLIER was picked by simulating a representative max-level
# solo attacker (Lv200, 四轉業火尊者, 轉生 x3, a country str bonus + a 3-piece
# equipment set) against this profile with run_battle: at x50 raw HP, that
# attacker depleted it solo in ~9-12 repeated actions across 300 simulated
# trials (average ~10.15) -- comfortably inside the "5-20 actions" target and
# matching the original x70/60-round-cap tuning's ~9-12 actions (avg ~10.7)
# almost exactly. Recomputed after the round-mechanics rework (15-round cap,
# both sides' attack counts now scale off their OWN speed instead of only
# the faster side getting extra attacks off the speed lead) -- that rework
# drastically raised per-action damage output, so the old x70 value alone
# would have let a strong attacker solo it in ~14 actions instead; x50 was
# re-derived from scratch against the new run_battle to land back in the
# original target band.
_BANDIT_LORD_NAME = "山賊領主"
_BANDIT_LORD_ZERO_BONUS = {
    "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 0,
}
BANDIT_HP_MULTIPLIER = 50


def _bandit_lord_stats(settings=None):
    """Builds the fixed monster-shaped stat profile for the neutral-tile
    bandit lord: name, atk/def/agi, and its MAX hp (settings is accepted but
    currently unused -- kept for signature symmetry with defense_tower_stats
    in case a future admin-tunable multiplier is added to game_settings).
    Callers resolving a fight against a specific tile must overwrite the
    returned dict's "hp" key with that tile's persisted (possibly already-
    damaged) map_tiles.bandit_hp instead of using this max value directly."""
    s = compute_final_stats(_BANDIT_LORD_ZERO_BONUS, [], LEVEL_CAP)
    return {
        "name": _BANDIT_LORD_NAME,
        "hp": s["hp"] * BANDIT_HP_MULTIPLIER,
        "atk": s["str"], "def": s["def"], "agi": s["agi"],
        "element": "",
    }


def _current_hp_mp(character, stats):
    """current_hp/current_mp of NULL means "untouched, full" -- battles and
    /game/recover are the only things that ever write a concrete number."""
    hp = character["current_hp"] if character["current_hp"] is not None else stats["hp"]
    mp = character["current_mp"] if character["current_mp"] is not None else stats["mp"]
    return max(0, min(hp, stats["hp"])), max(0, min(mp, stats["mp"]))
