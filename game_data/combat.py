import random

from db import ELEMENT_OVERCOMES
from game_data.skills import _roll_job_skill, _skill_damage_stat_value

# --- Combat formula tuning ------------------------------------------------
# Every percentage-based stat (減傷/爆擊率/命中率/閃避率) uses an x/(x+K) soft-cap
# curve: it keeps climbing but only asymptotically approaches its ceiling, so no
# stat combination can ever push it to a literal 100% (guaranteed unhittable /
# undodgeable / uncritable outcome).
STR_DAMAGE_RANGE = (0.85, 1.15)      # 1 STR point -> 0.85~1.15 raw damage, rolled per hit
DEF_REDUCTION_K = 120                # DEF/(DEF+K) -> reduction fraction, asymptotic to 1
DEF_REDUCTION_JITTER = (0.9, 1.1)    # per-hit jitter on the reduction fraction itself
DEF_REDUCTION_HARD_CAP = 0.90        # extra safety net alongside the asymptote
SPEED_PER_AGI = 1                    # 1 AGI -> 1 attack-speed point (speed IS raw AGI)
EXTRA_ATTACK_SPEED_STEP = 50         # every 50 AGI of lead = +1 attack per round
CRIT_CHANCE_K = 150                  # AGI/(AGI+K) -> crit chance, asymptotic
CRIT_CHANCE_HARD_CAP = 70            # percent
CRIT_DAMAGE_RANGE = (1.3, 1.7)       # crit multiplier rolled per crit
HIT_CHANCE_BASE = 90                 # percent, at LUK=0 (so monsters w/o LUK still swing)
HIT_CHANCE_MAX_BONUS = 10            # + up to this many percent, asymptotic w/ LUK
HIT_CHANCE_K = 100
DODGE_CHANCE_BASE = 5                # percent, baseline evasion even at LUK=0
DODGE_CHANCE_MAX_BONUS = 55          # + up to this many percent, asymptotic w/ LUK
DODGE_CHANCE_K = 150
GOLD_LUK_BONUS_PER_POINT = 0.05      # percent of currency reward per LUK point
GOLD_LUK_BONUS_CAP = 15              # percent
ELEMENT_OVERCOME_BONUS = 1.25        # attacker's element 剋 defender's -> +25% damage
ELEMENT_OVERCOME_PENALTY = 0.8       # attacker's element 被剋 by defender's -> -20% damage


def elemental_multiplier(attacker_element, defender_element):
    if not attacker_element or not defender_element or attacker_element == defender_element:
        return 1.0
    if ELEMENT_OVERCOMES.get(attacker_element) == defender_element:
        return ELEMENT_OVERCOME_BONUS
    if ELEMENT_OVERCOMES.get(defender_element) == attacker_element:
        return ELEMENT_OVERCOME_PENALTY
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


def _hit_chance_pct(attacker_luk):
    return HIT_CHANCE_BASE + HIT_CHANCE_MAX_BONUS * attacker_luk / (attacker_luk + HIT_CHANCE_K)


def _dodge_chance_pct(defender_luk):
    return DODGE_CHANCE_BASE + DODGE_CHANCE_MAX_BONUS * defender_luk / (defender_luk + DODGE_CHANCE_K)


def _crit_chance_pct(attacker_agi):
    return min(CRIT_CHANCE_HARD_CAP, 100 * attacker_agi / (attacker_agi + CRIT_CHANCE_K))


def _def_reduction_fraction(defender_def):
    return defender_def / (defender_def + DEF_REDUCTION_K)


def gold_luk_bonus_pct(luk):
    return min(GOLD_LUK_BONUS_CAP, luk * GOLD_LUK_BONUS_PER_POINT)


def derived_combat_stats(stats):
    """Same formulas as combat, minus the per-hit dice rolls -- for display
    on the character sheet (shows the range a stat translates into)."""
    reduction = _def_reduction_fraction(stats["def"])
    return {
        "damage_min": round(stats["str"] * STR_DAMAGE_RANGE[0]),
        "damage_max": round(stats["str"] * STR_DAMAGE_RANGE[1]),
        "reduction_min": round(min(DEF_REDUCTION_HARD_CAP, reduction * DEF_REDUCTION_JITTER[0]) * 100, 1),
        "reduction_max": round(min(DEF_REDUCTION_HARD_CAP, reduction * DEF_REDUCTION_JITTER[1]) * 100, 1),
        "speed": stats["agi"] * SPEED_PER_AGI,
        "crit_chance": round(_crit_chance_pct(stats["agi"]), 1),
        "crit_damage_min": CRIT_DAMAGE_RANGE[0],
        "crit_damage_max": CRIT_DAMAGE_RANGE[1],
        "hit_chance": round(_hit_chance_pct(stats["luk"]), 1),
        "dodge_chance": round(_dodge_chance_pct(stats["luk"]), 1),
        "gold_bonus": round(gold_luk_bonus_pct(stats["luk"]), 1),
    }


def _combat_hit(
    attacker_name, attacker_atk, attacker_agi, attacker_luk, attacker_element,
    defender_name, defender_def, defender_luk, defender_element,
    damage_multiplier=1.0, skill_name=None, attacker_independent_damage_percent=0,
):
    """attacker_independent_damage_percent (獨立傷害, from a fully-equipped
    秘境 火 set -- see character_special_effects) defaults to 0 so every
    pre-existing call site is bit-for-bit unchanged. When nonzero it adds a
    flat percentage of the ALREADY-mitigated damage on top; because it is
    computed after the defense reduction it bypasses no further mitigation,
    which is exactly what makes it "independent". It never turns a miss, a
    dodge or a 0 into damage -- those paths return before it applies."""
    if random.random() * 100 >= _hit_chance_pct(attacker_luk):
        return 0, f"{attacker_name} 的攻擊沒有命中"

    if random.random() * 100 < _dodge_chance_pct(defender_luk):
        return 0, f"{defender_name} 閃避了 {attacker_name} 的攻擊！"

    is_crit = random.random() * 100 < _crit_chance_pct(attacker_agi)
    crit_mult = random.uniform(*CRIT_DAMAGE_RANGE) if is_crit else 1.0
    elem_mult = elemental_multiplier(attacker_element, defender_element)
    raw_damage = attacker_atk * random.uniform(*STR_DAMAGE_RANGE) * crit_mult * elem_mult * damage_multiplier

    reduction = min(DEF_REDUCTION_HARD_CAP, _def_reduction_fraction(defender_def) * random.uniform(*DEF_REDUCTION_JITTER))
    damage = max(1, round(raw_damage * (1 - reduction)))
    independent_bonus = (
        round(damage * attacker_independent_damage_percent / 100)
        if attacker_independent_damage_percent else 0
    )
    damage += independent_bonus
    suffix = "（會心一擊！）" if is_crit else ""
    if independent_bonus:
        suffix += f"（獨立傷害 +{independent_bonus}）"
    if elem_mult > 1:
        suffix += "（屬性相剋！）"
    elif elem_mult < 1:
        suffix += "（屬性被剋）"
    verb = f"使出「{skill_name}」，攻擊" if skill_name else "攻擊"
    return damage, f"{attacker_name} {verb} {defender_name}，造成 {damage} 點傷害{suffix}"


BATTLE_ROUND_CAP = 15


def run_battle(
    player_name, player_stats, player_element, player_hp, monster, player_mp=0, usable_skills=(),
    player_independent_damage_percent=0,
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

    If BATTLE_ROUND_CAP is reached with both sides still alive, the fight
    ends in a timeout (`timed_out=True`) rather than a loss -- the attacker
    was never actually defeated, the round limit just ran out.

    player_independent_damage_percent applies ONLY to the "player" side's
    hits. The monster-shaped opponent (a real monster, an NPC defense tower,
    the bandit lord, or a garrisoned defender rendered monster-shaped) never
    gets one -- it has no equipment rows to carry a 秘境 set."""
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
                    monster["name"], monster["def"], monster.get("luk", 0), monster["element"],
                    damage_multiplier=skill["multiplier"], skill_name=skill["name"],
                    attacker_independent_damage_percent=player_independent_damage_percent,
                )
            else:
                dmg, line = _combat_hit(
                    player_name, player_stats["str"], player_stats["agi"], player_stats["luk"], player_element,
                    monster["name"], monster["def"], monster.get("luk", 0), monster["element"],
                    attacker_independent_damage_percent=player_independent_damage_percent,
                )
            m_hp = max(0, m_hp - dmg)
            log.append(f"{line}（{monster['name']} 剩餘 HP {m_hp}）")
        else:
            dmg, line = _combat_hit(
                monster["name"], monster["atk"], monster["agi"], monster.get("luk", 0), monster["element"],
                player_name, player_stats["def"], player_stats["luk"], player_element,
            )
            p_hp = max(0, p_hp - dmg)
            log.append(f"{line}（{player_name} 剩餘 HP {p_hp}）")

    timed_out = False
    for round_num in range(BATTLE_ROUND_CAP):
        if p_hp <= 0 or m_hp <= 0:
            break
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
        if round_num == BATTLE_ROUND_CAP - 1:
            timed_out = True

    return {
        "log": log, "won": m_hp <= 0 and p_hp > 0, "timed_out": timed_out,
        "player_hp": p_hp, "player_mp": p_mp, "monster_hp": m_hp,
    }


def run_pvp_duel(
    a_name, a_stats, a_element, a_skills, b_name, b_stats, b_element, b_skills,
    a_independent_damage_percent=0, b_independent_damage_percent=0,
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
    frozen snap_independent_damage_percent rather than their live gear."""
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
                    b_name, b_stats["def"], b_stats["luk"], b_element,
                    damage_multiplier=skill["multiplier"], skill_name=skill["name"],
                    attacker_independent_damage_percent=a_independent_damage_percent,
                )
            else:
                dmg, line = _combat_hit(
                    a_name, a_stats["str"], a_stats["agi"], a_stats["luk"], a_element,
                    b_name, b_stats["def"], b_stats["luk"], b_element,
                    attacker_independent_damage_percent=a_independent_damage_percent,
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
                    a_name, a_stats["def"], a_stats["luk"], a_element,
                    damage_multiplier=skill["multiplier"], skill_name=skill["name"],
                    attacker_independent_damage_percent=b_independent_damage_percent,
                )
            else:
                dmg, line = _combat_hit(
                    b_name, b_stats["str"], b_stats["agi"], b_stats["luk"], b_element,
                    a_name, a_stats["def"], a_stats["luk"], a_element,
                    attacker_independent_damage_percent=b_independent_damage_percent,
                )
            a_hp = max(0, a_hp - dmg)
            log.append(f"{line}（{a_name} 剩餘 HP {a_hp}）")

    timed_out = False
    for round_num in range(BATTLE_ROUND_CAP):
        if a_hp <= 0 or b_hp <= 0:
            break
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
        if round_num == BATTLE_ROUND_CAP - 1:
            timed_out = True

    return {
        "log": log, "a_hp": a_hp, "b_hp": b_hp, "a_mp": a_mp, "b_mp": b_mp,
        "timed_out": timed_out,
        "winner": "a" if b_hp <= 0 and a_hp > 0 else ("b" if a_hp <= 0 and b_hp > 0 else None),
    }
