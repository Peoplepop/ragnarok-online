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
SPEED_PER_AGI = 5                    # 1 AGI -> 5 attack-speed points
EXTRA_ATTACK_SPEED_STEP = 50         # every 50 speed points of lead = +1 attack per round
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
    damage_multiplier=1.0, skill_name=None,
):
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
    suffix = "（會心一擊！）" if is_crit else ""
    if elem_mult > 1:
        suffix += "（屬性相剋！）"
    elif elem_mult < 1:
        suffix += "（屬性被剋）"
    verb = f"使出「{skill_name}」，攻擊" if skill_name else "攻擊"
    return damage, f"{attacker_name} {verb} {defender_name}，造成 {damage} 點傷害{suffix}"


BATTLE_ROUND_CAP = 60


def run_battle(player_name, player_stats, player_element, player_hp, monster, player_mp=0, usable_skills=()):
    """Resolves an entire fight in one shot. Turn order and extra attacks are
    driven purely by attack speed (AGI*SPEED_PER_AGI): whoever is faster always
    goes first each round, and gets +1 extra attack per EXTRA_ATTACK_SPEED_STEP
    of speed lead. Monsters have no LUK column so they use 0 (baseline hit/dodge
    only), but they do roll crits off their own AGI and carry their own
    element for the Wu Xing damage multiplier. Each player attack independently
    tries the character's known skills (strongest first) before falling back
    to a plain hit."""
    log = []
    p_hp, m_hp, p_mp = player_hp, monster["hp"], player_mp

    player_speed = player_stats["agi"] * SPEED_PER_AGI
    monster_speed = monster["agi"] * SPEED_PER_AGI
    if player_speed >= monster_speed:
        faster, slower = "player", "monster"
        speed_lead = player_speed - monster_speed
    else:
        faster, slower = "monster", "player"
        speed_lead = monster_speed - player_speed
    extra_attacks = speed_lead // EXTRA_ATTACK_SPEED_STEP

    def attack_once(attacker):
        nonlocal p_hp, m_hp, p_mp
        if attacker == "player":
            skill = _roll_job_skill(usable_skills, p_mp)
            if skill:
                p_mp -= skill["mp_cost"]
                atk_value = _skill_damage_stat_value(player_stats, skill["stat"])
                dmg, line = _combat_hit(
                    player_name, atk_value, player_stats["agi"], player_stats["luk"], player_element,
                    monster["name"], monster["def"], 0, monster["element"],
                    damage_multiplier=skill["multiplier"], skill_name=skill["name"],
                )
            else:
                dmg, line = _combat_hit(
                    player_name, player_stats["str"], player_stats["agi"], player_stats["luk"], player_element,
                    monster["name"], monster["def"], 0, monster["element"],
                )
            m_hp = max(0, m_hp - dmg)
            log.append(f"{line}（{monster['name']} 剩餘 HP {m_hp}）")
        else:
            dmg, line = _combat_hit(
                monster["name"], monster["atk"], monster["agi"], 0, monster["element"],
                player_name, player_stats["def"], player_stats["luk"], player_element,
            )
            p_hp = max(0, p_hp - dmg)
            log.append(f"{line}（{player_name} 剩餘 HP {p_hp}）")

    for _round in range(BATTLE_ROUND_CAP):
        if p_hp <= 0 or m_hp <= 0:
            break
        for _ in range(1 + extra_attacks):
            attack_once(faster)
            if p_hp <= 0 or m_hp <= 0:
                break
        if p_hp <= 0 or m_hp <= 0:
            break
        attack_once(slower)

    return {"log": log, "won": m_hp <= 0 and p_hp > 0, "player_hp": p_hp, "player_mp": p_mp, "monster_hp": m_hp}
