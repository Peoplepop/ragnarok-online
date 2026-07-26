from game_data.constants import BASE_STATS, LEVEL_STAT_GROWTH
from game_data.equipment import _equipment_set_bonus
from game_data.jobs import job_stat_bonus_pct


def compute_final_stats(
    country, equipped_items=(), level=1, job_bonus=None, rebirth_bonus_percent=0, level_bonus_stats=None,
):
    """level_bonus_stats is the per-character accumulated total from the
    random job-weighted level-up rolls (see _roll_level_up_stat_points) --
    always supplied for real player characters via character_final_stats.
    Left as None here only for NPC-only callers (defense_tower_stats), which
    still fall back to the old flat LEVEL_STAT_GROWTH formula since NPCs are
    computed fresh at a fixed level, not leveled up one roll at a time."""
    job_bonus = job_bonus or {}
    equip_bonus = {}
    for item in equipped_items:
        if item:
            equip_bonus[item["stat"]] = equip_bonus.get(item["stat"], 0) + item["stat_bonus"]
    for stat, amount in _equipment_set_bonus(equipped_items).items():
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


def character_final_stats(character, equipped_items, settings):
    """Like compute_final_stats, but layers in the character's job-tier bonus
    and stacking rebirth bonus, then clamps every stat to its stat-floor
    snapshot (if any) so a promotion can never make a stat go down. Rebirth
    intentionally bypasses this floor -- see game_rebirth, which clears it."""
    job_bonus = job_stat_bonus_pct(character["job_class"], character["job_tier"])
    rebirth_bonus = character["rebirth_count"] * settings["rebirth_stat_bonus_percent"]
    level_bonus_stats = {key: character[f"level_bonus_{key}"] for key in BASE_STATS}
    stats = compute_final_stats(
        character, equipped_items, character["level"], job_bonus, rebirth_bonus, level_bonus_stats,
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


def _current_hp_mp(character, stats):
    """current_hp/current_mp of NULL means "untouched, full" -- battles and
    /game/recover are the only things that ever write a concrete number."""
    hp = character["current_hp"] if character["current_hp"] is not None else stats["hp"]
    mp = character["current_mp"] if character["current_mp"] is not None else stats["mp"]
    return max(0, min(hp, stats["hp"])), max(0, min(mp, stats["mp"]))
