import random

from db import ELEMENT_OVERCOMES
from game_data.skills import _roll_job_skill, _skill_damage_stat_value

# --- Combat formula tuning ------------------------------------------------
# Every percentage-based stat (減傷/爆擊率/命中率/閃避率) uses an x/(x+K) soft-cap
# curve: it keeps climbing but only asymptotically approaches its ceiling, so no
# stat combination can ever push it to a literal 100% (guaranteed unhittable /
# undodgeable / uncritable outcome).
#
# The formula's tunable knobs (STR damage range, DEF reduction, crit, hit,
# dodge, elemental bonus/penalty) live in the game_settings DB row and are
# admin-editable -- see db.py's _ensure_game_settings_columns and
# templates/admin_settings.html's "戰鬥傷害公式" section. Every function below
# that used to read a module-level constant for one of those now takes the
# `settings` row (sqlite3.Row from `SELECT * FROM game_settings`) instead.
SPEED_PER_AGI = 1                    # 1 AGI -> 1 attack-speed point (speed IS raw AGI)
EXTRA_ATTACK_SPEED_STEP = 50         # every 50 AGI of lead = +1 attack per round
GOLD_LUK_BONUS_PER_POINT = 0.05      # percent of currency reward per LUK point
GOLD_LUK_BONUS_CAP = 15              # percent


def elemental_multiplier(attacker_element, defender_element, settings):
    if not attacker_element or not defender_element or attacker_element == defender_element:
        return 1.0
    if ELEMENT_OVERCOMES.get(attacker_element) == defender_element:
        return 1 + settings["element_overcome_bonus_percent"] / 100
    if ELEMENT_OVERCOMES.get(defender_element) == attacker_element:
        return 1 - settings["element_overcome_penalty_percent"] / 100
    return 1.0


WU_XING_ELEMENTS = ("金", "木", "水", "火", "土")


def _resolve_battle_element(monster):
    """A monster-shaped dict with a real element keeps it untouched. One with
    a blank element gets a fresh random Wu Xing pick for THIS fight only (the
    caller must not persist the result) -- UNLESS it carries "element_neutral":
    True, the explicit per-monster marker for the handful of endgame/neutral
    fights (陰陽尊者, 混沌天尊, 山賊領主) that are deliberately unaligned and must
    stay that way regardless of who fights them."""
    element = monster.get("element")
    if element or monster.get("element_neutral"):
        return element
    return random.choice(WU_XING_ELEMENTS)


def _hit_chance_pct(attacker_luk, settings):
    return settings["hit_chance_base_percent"] + settings["hit_chance_max_bonus_percent"] * attacker_luk / (
        attacker_luk + settings["hit_chance_k"]
    )


def _dodge_chance_pct(defender_luk, settings):
    return settings["dodge_chance_base_percent"] + settings["dodge_chance_max_bonus_percent"] * defender_luk / (
        defender_luk + settings["dodge_chance_k"]
    )


def _crit_chance_pct(attacker_agi, settings):
    return min(
        settings["crit_chance_hard_cap_percent"],
        100 * attacker_agi / (attacker_agi + settings["crit_chance_k"]),
    )


def _def_reduction_fraction(defender_def, settings):
    return defender_def / (defender_def + settings["def_reduction_k"])


def gold_luk_bonus_pct(luk):
    return min(GOLD_LUK_BONUS_CAP, luk * GOLD_LUK_BONUS_PER_POINT)


def derived_combat_stats(stats, settings):
    """Same formulas as combat, minus the per-hit dice rolls -- for display
    on the character sheet (shows the range a stat translates into)."""
    reduction = _def_reduction_fraction(stats["def"], settings)
    def_reduction_hard_cap = settings["def_reduction_hard_cap_percent"] / 100
    return {
        "damage_min": round(stats["str"] * settings["str_damage_range_min"]),
        "damage_max": round(stats["str"] * settings["str_damage_range_max"]),
        "reduction_min": round(
            min(def_reduction_hard_cap, reduction * settings["def_reduction_jitter_min"]) * 100, 1
        ),
        "reduction_max": round(
            min(def_reduction_hard_cap, reduction * settings["def_reduction_jitter_max"]) * 100, 1
        ),
        "speed": stats["agi"] * SPEED_PER_AGI,
        "crit_chance": round(_crit_chance_pct(stats["agi"], settings), 1),
        "crit_damage_min": settings["crit_damage_min"],
        "crit_damage_max": settings["crit_damage_max"],
        "hit_chance": round(_hit_chance_pct(stats["luk"], settings), 1),
        "dodge_chance": round(_dodge_chance_pct(stats["luk"], settings), 1),
        "gold_bonus": round(gold_luk_bonus_pct(stats["luk"]), 1),
    }


def _combat_hit(
    attacker_name, attacker_atk, attacker_agi, attacker_luk, attacker_element,
    defender_name, defender_def, defender_luk, defender_element, settings,
    damage_multiplier=1.0, skill_name=None, attacker_independent_damage_percent=0,
    defender_damage_reduction_percent=0,
):
    """attacker_independent_damage_percent (獨立傷害, from a fully-equipped
    秘境 火 set, or an admin-managed item's own 獨立傷害 effect -- see
    character_special_effects) defaults to 0 so every pre-existing call site
    is bit-for-bit unchanged. When nonzero it adds a flat percentage of the
    ALREADY-mitigated damage on top; because it is computed after the defense
    reduction it bypasses no further mitigation, which is exactly what makes
    it "independent". It never turns a miss, a dodge or a 0 into damage --
    those paths return before it applies.

    defender_damage_reduction_percent (減傷%, admin-managed items only -- no
    legacy hidden-set equivalent) defaults to 0 the same way, and applies
    AFTER the independent-damage addition above, cutting the final damage
    value by that percent and flooring at 1 (same floor as the existing
    max(1, ...) pattern below) -- it can reduce a hit but never fully negate
    it."""
    if random.random() * 100 >= _hit_chance_pct(attacker_luk, settings):
        return 0, f"{attacker_name} 的攻擊沒有命中"

    if random.random() * 100 < _dodge_chance_pct(defender_luk, settings):
        return 0, f"{defender_name} 閃避了 {attacker_name} 的攻擊！"

    is_crit = random.random() * 100 < _crit_chance_pct(attacker_agi, settings)
    crit_mult = random.uniform(settings["crit_damage_min"], settings["crit_damage_max"]) if is_crit else 1.0
    elem_mult = elemental_multiplier(attacker_element, defender_element, settings)
    raw_damage = attacker_atk * random.uniform(
        settings["str_damage_range_min"], settings["str_damage_range_max"]
    ) * crit_mult * elem_mult * damage_multiplier

    def_reduction_hard_cap = settings["def_reduction_hard_cap_percent"] / 100
    reduction = min(
        def_reduction_hard_cap,
        _def_reduction_fraction(defender_def, settings)
        * random.uniform(settings["def_reduction_jitter_min"], settings["def_reduction_jitter_max"]),
    )
    damage = max(1, round(raw_damage * (1 - reduction)))
    independent_bonus = (
        round(damage * attacker_independent_damage_percent / 100)
        if attacker_independent_damage_percent else 0
    )
    damage += independent_bonus
    reduction_amount = 0
    if defender_damage_reduction_percent:
        reduced = max(1, round(damage * (1 - defender_damage_reduction_percent / 100)))
        reduction_amount = damage - reduced
        damage = reduced
    suffix = "（會心一擊！）" if is_crit else ""
    if independent_bonus:
        suffix += f"（獨立傷害 +{independent_bonus}）"
    if reduction_amount:
        suffix += f"（減傷 -{reduction_amount}）"
    if elem_mult > 1:
        suffix += "（屬性相剋！）"
    elif elem_mult < 1:
        suffix += "（屬性被剋）"
    verb = f"使出「{skill_name}」，攻擊" if skill_name else "攻擊"
    return damage, f"{attacker_name} {verb} {defender_name}，造成 {damage} 點傷害{suffix}"


# Fallback only -- the real cap always comes from settings["battle_round_cap"]
# (admin-configurable, see blueprints/admin.py's admin_update_game_settings),
# this just protects against a settings row that somehow predates the column.
DEFAULT_BATTLE_ROUND_CAP = 15


def run_battle(
    player_name, player_stats, player_element, player_hp, monster, settings, player_mp=0, usable_skills=(),
    player_independent_damage_percent=0, player_damage_reduction_percent=0,
):
    """Resolves an entire fight in one shot. Turn order is driven purely by
    attack speed (AGI*SPEED_PER_AGI): whoever is faster always goes first each
    round. The faster side's attack count per round is based on the speed GAP
    to its opponent: 1 + gap // EXTRA_ATTACK_SPEED_STEP (gap = own_speed -
    opponent_speed). The slower side always gets exactly 1 attack per round,
    regardless of how large the gap is. The faster side fires its full
    attack allotment first (checking for a death after every hit), then the
    slower side fires its own full allotment -- only after BOTH sides have
    completed their attacks does the round counter advance. Real
    monsters-table monsters carry their own LUK (via monster.get("luk", 0))
    and roll LUK-based hit/dodge like a player; other monster-shaped
    opponents built ad hoc elsewhere (NPC defense towers, the bandit lord,
    the king-as-monster dict, garrisoned defenders rendered monster-shaped)
    have no "luk" key in their dict, so the same .get(...) call falls back to
    0 (baseline hit/dodge only) for them. All of them roll crits off their
    own AGI and carry their own element for the Wu Xing damage multiplier.
    Each player attack independently tries the character's known skills
    (strongest first) before falling back to a plain hit.

    If the round cap (settings["battle_round_cap"]) is reached with both
    sides still alive, the fight ends in a timeout (`timed_out=True`) rather
    than a loss -- the attacker was never actually defeated, the round limit
    just ran out.

    player_independent_damage_percent applies ONLY to the "player" side's
    hits. The monster-shaped opponent (a real monster, an NPC defense tower,
    the bandit lord, or a garrisoned defender rendered monster-shaped) never
    gets one -- it has no equipment rows to carry a 秘境 set.

    player_damage_reduction_percent similarly applies ONLY to the "player"
    side, and only when the MONSTER attacks (i.e. the player is the
    defender) -- the monster-shaped opponent has no gear to carry a
    減傷% effect either."""
    log = []
    p_hp, m_hp, p_mp = player_hp, monster["hp"], player_mp

    player_speed = player_stats["agi"] * SPEED_PER_AGI
    monster_speed = monster["agi"] * SPEED_PER_AGI
    if player_speed >= monster_speed:
        faster, slower = "player", "monster"
    else:
        faster, slower = "monster", "player"
    speed_gap = abs(player_speed - monster_speed)
    attacks_per_round = {faster: 1 + speed_gap // EXTRA_ATTACK_SPEED_STEP, slower: 1}

    def attack_once(attacker):
        nonlocal p_hp, m_hp, p_mp
        if attacker == "player":
            skill = _roll_job_skill(usable_skills, p_mp)
            if skill:
                p_mp -= skill["mp_cost"]
                atk_value = _skill_damage_stat_value(player_stats, skill["stat"])
                dmg, line = _combat_hit(
                    player_name, atk_value, player_stats["agi"], player_stats["luk"], player_element,
                    monster["name"], monster["def"], monster.get("luk", 0), monster["element"], settings,
                    damage_multiplier=skill["multiplier"], skill_name=skill["name"],
                    attacker_independent_damage_percent=player_independent_damage_percent,
                )
            else:
                dmg, line = _combat_hit(
                    player_name, player_stats["str"], player_stats["agi"], player_stats["luk"], player_element,
                    monster["name"], monster["def"], monster.get("luk", 0), monster["element"], settings,
                    attacker_independent_damage_percent=player_independent_damage_percent,
                )
            m_hp = max(0, m_hp - dmg)
            log.append(f"{line}（{monster['name']} 剩餘 HP {m_hp}）")
        else:
            dmg, line = _combat_hit(
                monster["name"], monster["atk"], monster["agi"], monster.get("luk", 0), monster["element"],
                player_name, player_stats["def"], player_stats["luk"], player_element, settings,
                defender_damage_reduction_percent=player_damage_reduction_percent,
            )
            p_hp = max(0, p_hp - dmg)
            log.append(f"{line}（{player_name} 剩餘 HP {p_hp}）")

    round_cap = settings["battle_round_cap"] if "battle_round_cap" in settings.keys() else DEFAULT_BATTLE_ROUND_CAP
    timed_out = False
    round_num = 0
    for round_num in range(round_cap):
        if p_hp <= 0 or m_hp <= 0:
            break
        # A plain "第 N 回合" line, recognized and styled (bold gold) by
        # battle.html's JS via its own roundMarkerRe -- deliberately just
        # another log entry rather than a separate parallel list, so no
        # other render_template call site needs to change.
        log.append(f"第 {round_num + 1} 回合")
        for _ in range(attacks_per_round[faster]):
            attack_once(faster)
            if p_hp <= 0 or m_hp <= 0:
                break
        if p_hp <= 0 or m_hp <= 0:
            break
        for _ in range(attacks_per_round[slower]):
            attack_once(slower)
            if p_hp <= 0 or m_hp <= 0:
                break
        if p_hp <= 0 or m_hp <= 0:
            break
        if round_num == round_cap - 1:
            timed_out = True

    return {
        "log": log, "won": m_hp <= 0 and p_hp > 0, "timed_out": timed_out,
        "player_hp": p_hp, "player_mp": p_mp, "monster_hp": m_hp,
        "rounds": round_num + 1, "round_cap": round_cap,
    }


def run_pvp_duel(
    a_name, a_stats, a_element, a_skills, b_name, b_stats, b_element, b_skills, settings,
    a_independent_damage_percent=0, b_independent_damage_percent=0,
    a_damage_reduction_percent=0, b_damage_reduction_percent=0,
):
    """Symmetric two-player duel for 天下武道大會 -- unlike run_battle (player
    vs monster-shaped opponent, where only the "player" side gets luk-based
    hit/dodge or skills), both sides here are full player-shaped combatants:
    each rolls its own equipped skills off its own MP pool, and each passes
    its own real LUK to _combat_hit on BOTH the attacker and the defender
    side. That asymmetry is an accepted simplification for PvE and for
    conquest garrison duels (see game_conquer), but it would be a genuine
    fairness bug in a head-to-head tournament where both combatants are real
    players. The per-hit primitive _combat_hit was already fully symmetric --
    only run_battle's orchestration loop hardcoded the asymmetry -- so this
    mirrors that loop's round-cap / speed / attacks-per-round structure
    exactly and changes nothing else.

    Always starts both sides at their full snapshotted hp/mp -- tournament
    games never carry damage across separate games (including between the
    individual games of a best-of-3 final).

    "winner" is None exactly when neither side was knocked out, i.e. when
    timed_out is True; the caller applies the tournament's remaining-HP%
    tiebreak in that case.

    Attack count per round follows run_battle's gap-based rule: the faster
    side gets 1 + speed_gap // EXTRA_ATTACK_SPEED_STEP, the slower side
    always gets exactly 1.

    Both sides get their own 獨立傷害 percent here (symmetric, like every
    other stat in this function) -- the tournament reads each registrant's
    frozen snap_independent_damage_percent rather than their live gear.
    a_damage_reduction_percent/b_damage_reduction_percent mirror this exactly
    (frozen snap_damage_reduction_percent), applied on whichever side is the
    DEFENDER of a given hit."""
    log = []
    a_hp, b_hp = a_stats["hp"], b_stats["hp"]
    a_mp, b_mp = a_stats["mp"], b_stats["mp"]

    a_speed = a_stats["agi"] * SPEED_PER_AGI
    b_speed = b_stats["agi"] * SPEED_PER_AGI
    if a_speed >= b_speed:
        faster, slower = "a", "b"
    else:
        faster, slower = "b", "a"
    speed_gap = abs(a_speed - b_speed)
    attacks_per_round = {faster: 1 + speed_gap // EXTRA_ATTACK_SPEED_STEP, slower: 1}

    def attack_once(attacker):
        nonlocal a_hp, b_hp, a_mp, b_mp
        if attacker == "a":
            skill = _roll_job_skill(a_skills, a_mp)
            if skill:
                a_mp -= skill["mp_cost"]
                atk_value = _skill_damage_stat_value(a_stats, skill["stat"])
                dmg, line = _combat_hit(
                    a_name, atk_value, a_stats["agi"], a_stats["luk"], a_element,
                    b_name, b_stats["def"], b_stats["luk"], b_element, settings,
                    damage_multiplier=skill["multiplier"], skill_name=skill["name"],
                    attacker_independent_damage_percent=a_independent_damage_percent,
                    defender_damage_reduction_percent=b_damage_reduction_percent,
                )
            else:
                dmg, line = _combat_hit(
                    a_name, a_stats["str"], a_stats["agi"], a_stats["luk"], a_element,
                    b_name, b_stats["def"], b_stats["luk"], b_element, settings,
                    attacker_independent_damage_percent=a_independent_damage_percent,
                    defender_damage_reduction_percent=b_damage_reduction_percent,
                )
            b_hp = max(0, b_hp - dmg)
            log.append(f"{line}（{b_name} 剩餘 HP {b_hp}）")
        else:
            skill = _roll_job_skill(b_skills, b_mp)
            if skill:
                b_mp -= skill["mp_cost"]
                atk_value = _skill_damage_stat_value(b_stats, skill["stat"])
                dmg, line = _combat_hit(
                    b_name, atk_value, b_stats["agi"], b_stats["luk"], b_element,
                    a_name, a_stats["def"], a_stats["luk"], a_element, settings,
                    damage_multiplier=skill["multiplier"], skill_name=skill["name"],
                    attacker_independent_damage_percent=b_independent_damage_percent,
                    defender_damage_reduction_percent=a_damage_reduction_percent,
                )
            else:
                dmg, line = _combat_hit(
                    b_name, b_stats["str"], b_stats["agi"], b_stats["luk"], b_element,
                    a_name, a_stats["def"], a_stats["luk"], a_element, settings,
                    attacker_independent_damage_percent=b_independent_damage_percent,
                    defender_damage_reduction_percent=a_damage_reduction_percent,
                )
            a_hp = max(0, a_hp - dmg)
            log.append(f"{line}（{a_name} 剩餘 HP {a_hp}）")

    round_cap = settings["battle_round_cap"] if "battle_round_cap" in settings.keys() else DEFAULT_BATTLE_ROUND_CAP
    timed_out = False
    round_num = 0
    for round_num in range(round_cap):
        if a_hp <= 0 or b_hp <= 0:
            break
        log.append(f"第 {round_num + 1} 回合")
        for _ in range(attacks_per_round[faster]):
            attack_once(faster)
            if a_hp <= 0 or b_hp <= 0:
                break
        if a_hp <= 0 or b_hp <= 0:
            break
        for _ in range(attacks_per_round[slower]):
            attack_once(slower)
            if a_hp <= 0 or b_hp <= 0:
                break
        if a_hp <= 0 or b_hp <= 0:
            break
        if round_num == round_cap - 1:
            timed_out = True

    return {
        "log": log, "a_hp": a_hp, "b_hp": b_hp, "a_mp": a_mp, "b_mp": b_mp,
        "timed_out": timed_out,
        "winner": "a" if b_hp <= 0 and a_hp > 0 else ("b" if a_hp <= 0 and b_hp > 0 else None),
        "rounds": round_num + 1, "round_cap": round_cap,
    }
