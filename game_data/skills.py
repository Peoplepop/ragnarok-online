import random

from game_data.jobs import TIER2_JOBS, TIER3_JOBS, TIER4_JOB_BY_STAT, TIER4_TIE_JOB

# Skill tree: every job path (novice-by-element, then each 二轉/三轉/四轉 job)
# has 1-3 learnable skills gated by level, each a one-time currency purchase
# that's permanent from then on (rebirth never un-learns a skill). Tuning is
# keyed by (job_tier, slot) and deliberately monotonic across the *entire*
# ladder, not just per tier: mp_cost and multiplier only ever go up,
# trigger_chance only ever goes down, bottoming out at exactly 25% for the
# single 四轉 slot. A skill's damage is scaled off its job's own primary stat
# (or the country element's stat for the novice skill) instead of always STR.
NOVICE_SKILL_STAT_BY_ELEMENT = {"金": "luk", "木": "def", "水": "agi", "火": "str", "土": "avg"}
NOVICE_SKILL_NAMES = {
    "金": "淬鋒初擊", "木": "木刺初綻", "水": "水流初斬", "火": "火燄初綻", "土": "厚土初擊",
}

TIER2_SKILL_NAMES = {
    "鋒劍士": "鋒芒斬", "鐵衛劍師": "鐵壁震擊", "疾風俠客": "疾風連斬",
    "天機遊人": "天機一擲", "磐石陣師": "磐石震陣", "易數先生": "易數返煞",
}
TIER2_SKILL_NAMES_SLOT2 = {
    "鋒劍士": "破鋒連環斬", "鐵衛劍師": "千鈞壁壘", "疾風俠客": "迅雷追影",
    "天機遊人": "天機幻擊", "磐石陣師": "磐石連環擊", "易數先生": "易數迴天",
}

TIER3_SKILL_NAMES = {
    "裂地劍豪": "裂地劍氣", "煉體宗師": "煉體崩拳", "不動劍聖": "不動如山",
    "回天鐵壁": "回天一擊", "追風劍影": "追風縱影斬", "踏虛步影": "踏虛幻影襲",
    "奇門遁甲師": "奇門封陣", "天命劍仙": "天命劍雨", "不壞金身": "不壞金鐘",
    "龜甲寒鐵陣": "龜甲寒鐵擊", "五行卜算師": "五行逆卜", "太乙真人": "太乙護體咒",
}
TIER3_SKILL_NAMES_SLOT2 = {
    "裂地劍豪": "裂地崩山斬", "煉體宗師": "煉體天罡拳", "不動劍聖": "不動金剛劍",
    "回天鐵壁": "回天連環擊", "追風劍影": "追風破空斬", "踏虛步影": "踏虛連環襲",
    "奇門遁甲師": "奇門幻陣", "天命劍仙": "天命劍瀑", "不壞金身": "不壞羅漢拳",
    "龜甲寒鐵陣": "龜甲連環擊", "五行卜算師": "五行連環卜", "太乙真人": "太乙迴天咒",
}
TIER3_SKILL_NAMES_SLOT3 = {
    "裂地劍豪": "裂地滅世劍", "煉體宗師": "煉體不滅金身拳", "不動劍聖": "不動萬鈞式",
    "回天鐵壁": "回天定乾坤", "追風劍影": "追風絕影劍", "踏虛步影": "踏虛滅蹤斬",
    "奇門遁甲師": "奇門絕殺陣", "天命劍仙": "天命誅仙劍", "不壞金身": "不壞金剛體",
    "龜甲寒鐵陣": "龜甲玄冰陣", "五行卜算師": "五行滅命卜", "太乙真人": "太乙鎮魂咒",
}

TIER4_SKILL_NAMES = {
    "業火尊者": "業火焚天", "青木道尊": "青木不朽", "流水劍尊": "流水無痕劍",
    "流金尊者": "流金運轉劫", "厚土真尊": "厚土鎮世",
}

# 四轉's 2nd skill slot: NOT learnable with currency at all (see
# TIER_SLOT_TUNING[(4, 2)]'s "requires_skill_book" marker and
# _learnable_skills, which hardcodes tier4 to only ever offer slot 1). The
# only way in is a monster-dropped skill book, redeemed via
# /character/skill_book/use once the character is actually 四轉.
TIER4_SKILL_NAMES_SLOT2 = {
    "業火尊者": "業火燎原", "青木道尊": "青木蔽天", "流水劍尊": "流水穿石劍",
    "流金尊者": "流金逆天劫", "厚土真尊": "厚土封天",
}

# (job_tier, slot) -> tuning. slot counts up within a tier (1 = first learned).
TIER_SLOT_TUNING = {
    (0, 1): {"mp_cost": 15, "multiplier": 1.3, "trigger_chance": 65, "learn_level": 10, "learn_cost": 500},
    (2, 1): {"mp_cost": 20, "multiplier": 1.6, "trigger_chance": 55, "learn_level": 45, "learn_cost": 2000},
    (2, 2): {"mp_cost": 28, "multiplier": 1.9, "trigger_chance": 48, "learn_level": 60, "learn_cost": 4000},
    (3, 1): {"mp_cost": 35, "multiplier": 2.2, "trigger_chance": 40, "learn_level": 70, "learn_cost": 8000},
    (3, 2): {"mp_cost": 42, "multiplier": 2.5, "trigger_chance": 35, "learn_level": 90, "learn_cost": 14000},
    (3, 3): {"mp_cost": 50, "multiplier": 2.8, "trigger_chance": 30, "learn_level": 110, "learn_cost": 22000},
    # 四轉's 1st slot is learnable with currency same as every other tier's
    # skills. Its 2nd slot (added below) is deliberately NOT part of that
    # currency ladder -- it only ever comes from a monster-dropped skill
    # book (1/20000 per won hunt in the 究級打怪場). trigger_chance stays at
    # the ladder's 25% floor (a 2nd 四轉 slot doesn't go lower), while
    # mp_cost/multiplier still climb past slot 1's, keeping the whole ladder
    # monotonic end to end.
    (4, 1): {"mp_cost": 55, "multiplier": 3.2, "trigger_chance": 25, "learn_level": 121, "learn_cost": 40000},
    (4, 2): {
        "mp_cost": 65, "multiplier": 3.6, "trigger_chance": 25, "learn_level": 121,
        "learn_cost": None, "requires_skill_book": True,
    },
}


def _skill_key(job_class, slot):
    return f"{job_class}_{slot}"


def _novice_skill_key(element):
    return f"novice_{element}"


def _build_skill_catalog():
    catalog = {}
    for element, stat in NOVICE_SKILL_STAT_BY_ELEMENT.items():
        catalog[_novice_skill_key(element)] = {
            "key": _novice_skill_key(element), "name": NOVICE_SKILL_NAMES[element], "stat": stat,
            "job_tier": 0, "slot": 1, "job_class": None, "element": element,
            **TIER_SLOT_TUNING[(0, 1)],
        }
    for job, info in TIER2_JOBS.items():
        for slot, names in ((1, TIER2_SKILL_NAMES), (2, TIER2_SKILL_NAMES_SLOT2)):
            catalog[_skill_key(job, slot)] = {
                "key": _skill_key(job, slot), "name": names[job], "stat": info["primary"],
                "job_tier": 2, "slot": slot, "job_class": job,
                **TIER_SLOT_TUNING[(2, slot)],
            }
    for job, info in TIER3_JOBS.items():
        for slot, names in ((1, TIER3_SKILL_NAMES), (2, TIER3_SKILL_NAMES_SLOT2), (3, TIER3_SKILL_NAMES_SLOT3)):
            catalog[_skill_key(job, slot)] = {
                "key": _skill_key(job, slot), "name": names[job], "stat": info["primary"],
                "job_tier": 3, "slot": slot, "job_class": job,
                **TIER_SLOT_TUNING[(3, slot)],
            }
    tier4_jobs = dict(TIER4_JOB_BY_STAT)
    for job in list(tier4_jobs.values()) + [TIER4_TIE_JOB]:
        stat = next((k for k, v in TIER4_JOB_BY_STAT.items() if v == job), "avg")
        catalog[_skill_key(job, 1)] = {
            "key": _skill_key(job, 1), "name": TIER4_SKILL_NAMES[job], "stat": stat,
            "job_tier": 4, "slot": 1, "job_class": job,
            **TIER_SLOT_TUNING[(4, 1)],
        }
        catalog[_skill_key(job, 2)] = {
            "key": _skill_key(job, 2), "name": TIER4_SKILL_NAMES_SLOT2[job], "stat": stat,
            "job_tier": 4, "slot": 2, "job_class": job,
            **TIER_SLOT_TUNING[(4, 2)],
        }
    return catalog


SKILL_CATALOG = _build_skill_catalog()

# The 5 monster-drop-only 四轉 slot-2 skill keys, used by the ultimate hunting
# ground's skill-book drop roll to pick which book a won hunt hands out.
TIER4_SLOT2_SKILL_KEYS = [_skill_key(job, 2) for job in TIER4_SKILL_NAMES_SLOT2]


def _skill_damage_stat_value(stats, stat):
    if stat == "avg":
        return round((stats["str"] + stats["def"] + stats["agi"] + stats["luk"]) / 4)
    return stats[stat]


def _current_lineage_job_classes(character):
    """The job_class(es) whose tier2/tier3 skills are learnable/usable *right
    now*, based purely on the character's current job -- once you promote past
    a tier its learn window is gone (whatever you learned stays learned, but
    you can't pick up something you skipped). None means "not applicable"."""
    tier = character["job_tier"]
    if tier == 2:
        return character["job_class"], None
    if tier == 3:
        return TIER3_JOBS[character["job_class"]]["parent"], character["job_class"]
    return None, None


def _learnable_skills(character, learned_keys):
    """Skills the character could learn right now: high enough level, not
    already learned, and on their current job lineage (novice skill is always
    on-lineage since every path starts from it)."""
    level = character["level"]
    tier2_job, tier3_job = _current_lineage_job_classes(character)
    candidates = [SKILL_CATALOG[_novice_skill_key(character["element"])]]
    if tier2_job:
        candidates += [SKILL_CATALOG[_skill_key(tier2_job, 1)], SKILL_CATALOG[_skill_key(tier2_job, 2)]]
    if tier3_job:
        candidates += [
            SKILL_CATALOG[_skill_key(tier3_job, s)] for s in (1, 2, 3)
        ]
    if character["job_tier"] == 4:
        candidates.append(SKILL_CATALOG[_skill_key(character["job_class"], 1)])
    return [
        s for s in candidates
        if s["key"] not in learned_keys and level >= s["learn_level"]
    ]


def _usable_skill_keys(character, learned_keys):
    """Which of the character's already-learned skills can fire in combat
    right now. 四轉 is a deliberate exception to the "own tree only" rule --
    by the time you're four-zhuan you've mastered 3 different 三轉 trees, so
    every skill you've ever learned (any lineage, any tier) becomes usable.
    Below 四轉, only the current lineage's novice/二轉/三轉 skills count --
    a skill learned in a past life under a different job tree stays learned
    but sits unusable until (if ever) you reach 四轉 again."""
    if character["job_tier"] == 4:
        return set(learned_keys)
    allowed = {_novice_skill_key(character["element"])}
    tier2_job, tier3_job = _current_lineage_job_classes(character)
    if tier2_job:
        allowed.update({_skill_key(tier2_job, 1), _skill_key(tier2_job, 2)})
    if tier3_job:
        allowed.update({_skill_key(tier3_job, s) for s in (1, 2, 3)})
    return learned_keys & allowed


def _ordered_usable_skills(usable_keys):
    """Strongest (highest tier, then highest slot) first, so combat tries the
    hardest-hitting known skill before falling back to a cheaper/likelier
    one, and only resorts to a plain attack if every known skill whiffs."""
    skills = [SKILL_CATALOG[k] for k in usable_keys if k in SKILL_CATALOG]
    skills.sort(key=lambda s: (s["job_tier"], s["slot"]), reverse=True)
    return skills


def _learned_skill_keys(db, character_id):
    return {
        row["skill_key"] for row in db.execute(
            "SELECT skill_key FROM character_skills WHERE character_id = ?", (character_id,)
        )
    }


def _character_equipped_skill_keys(db, character_id):
    row = db.execute(
        "SELECT equipped_skill_1, equipped_skill_2 FROM characters WHERE id = ?", (character_id,)
    ).fetchone()
    return [row["equipped_skill_1"], row["equipped_skill_2"]]


def _equipped_combat_skills(character, learned_keys, equipped_keys):
    """A character can know far more skills than they can fight with -- only
    the <=2 skills actually configured into characters.equipped_skill_1/2
    ("已配置技能") are ever tried in combat; everything else learned just sits
    in the "skill library" inert. Still runs the equipped keys back through
    _usable_skill_keys defensively: an equipped skill that's since become
    lineage-locked (there's no proactive clearing of stale equip slots on
    promotion) must not fire."""
    usable = _usable_skill_keys(character, learned_keys)
    ordered_keys = [k for k in equipped_keys if k and k in usable]
    return _ordered_usable_skills(ordered_keys)


def _character_usable_skills(db, character):
    """DB-fetching wrapper around _equipped_combat_skills -- every run_battle
    call site in app.py goes through this one function, so combat everywhere
    (hunts, boss room, conquest, garrison duels, bandit lord) only ever tries
    the character's equipped loadout, never their full learned skill set."""
    learned_keys = _learned_skill_keys(db, character["character_id"])
    equipped_keys = _character_equipped_skill_keys(db, character["character_id"])
    return _equipped_combat_skills(character, learned_keys, equipped_keys)


def _roll_job_skill(ordered_skills, current_mp):
    """Tries the character's known, currently-usable skills strongest first;
    the first one that's both affordable and wins its trigger roll (tier-
    scaled, floor 25% for 四轉) fires. None means every known skill either
    couldn't be afforded or missed its roll, so the caller falls back to a
    plain attack."""
    for skill in ordered_skills:
        if current_mp < skill["mp_cost"]:
            continue
        if random.random() * 100 < skill["trigger_chance"]:
            return skill
    return None
