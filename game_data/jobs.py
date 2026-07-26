# Job tree: 3 base philosophies, each already forked into two 二轉 jobs (one
# leaning each of the family's two stats); each 二轉 job forks again into two
# 三轉 jobs that blend in a third stat. Not stored in its own table -- job_class
# is just a string written into an existing characters column, so this is
# pure business-logic configuration living next to compute_final_stats.
# 一轉 (job_tier=1): the 3 root philosophies, promotable straight out of
# novice at level 10. Each forks into 2 job_tier=2 jobs (below) at level 30.
TIER1_JOBS = {
    "劍修":   {"primary": "str", "secondary": "def"},
    "游俠":   {"primary": "agi", "secondary": "luk"},
    "玄陣師": {"primary": "def", "secondary": "luk"},
}

TIER2_JOBS = {
    "鋒劍士":   {"family": "劍修",   "primary": "str", "secondary": "def"},
    "鐵衛劍師": {"family": "劍修",   "primary": "def", "secondary": "str"},
    "疾風俠客": {"family": "游俠",   "primary": "agi", "secondary": "luk"},
    "天機遊人": {"family": "游俠",   "primary": "luk", "secondary": "agi"},
    "磐石陣師": {"family": "玄陣師", "primary": "def", "secondary": "luk"},
    "易數先生": {"family": "玄陣師", "primary": "luk", "secondary": "def"},
}

TIER2_CHILDREN_BY_FAMILY = {}
for _name, _info in TIER2_JOBS.items():
    TIER2_CHILDREN_BY_FAMILY.setdefault(_info["family"], []).append(_name)

TIER3_JOBS = {
    "裂地劍豪":   {"parent": "鋒劍士",   "primary": "str", "secondary": "agi"},
    "煉體宗師":   {"parent": "鋒劍士",   "primary": "str", "secondary": "def"},
    "不動劍聖":   {"parent": "鐵衛劍師", "primary": "def", "secondary": "luk"},
    "回天鐵壁":   {"parent": "鐵衛劍師", "primary": "def", "secondary": "str"},
    "追風劍影":   {"parent": "疾風俠客", "primary": "agi", "secondary": "str"},
    "踏虛步影":   {"parent": "疾風俠客", "primary": "agi", "secondary": "def"},
    "奇門遁甲師": {"parent": "天機遊人", "primary": "luk", "secondary": "def"},
    "天命劍仙":   {"parent": "天機遊人", "primary": "luk", "secondary": "agi"},
    "不壞金身":   {"parent": "磐石陣師", "primary": "def", "secondary": "str"},
    "龜甲寒鐵陣": {"parent": "磐石陣師", "primary": "def", "secondary": "luk"},
    "五行卜算師": {"parent": "易數先生", "primary": "luk", "secondary": "agi"},
    "太乙真人":   {"parent": "易數先生", "primary": "luk", "secondary": "def"},
}

TIER3_CHILDREN_BY_PARENT = {}
for _name, _info in TIER3_JOBS.items():
    TIER3_CHILDREN_BY_PARENT.setdefault(_info["parent"], []).append(_name)

# 四轉 is deterministic, not truly random: sum each mastered 三轉 job's
# (primary weight 2 + secondary weight 1) across the character's 3 masteries,
# take the highest-scoring stat; a tie among stats falls to the earth job.
TIER4_JOB_BY_STAT = {"str": "業火尊者", "def": "青木道尊", "agi": "流水劍尊", "luk": "流金尊者"}
TIER4_TIE_JOB = "厚土真尊"

JOB_TIER_LABELS = {0: "初心者", 1: "一轉", 2: "二轉", 3: "三轉", 4: "四轉"}


def job_stat_bonus_pct(job_class, job_tier):
    """{stat: percent} bonus from the character's current job -- only ever
    touches str/def/agi/luk, never hp/mp (unlike the rebirth bonus)."""
    if job_tier == 1:
        info = TIER1_JOBS.get(job_class)
        return {info["primary"]: 5, info["secondary"]: 2} if info else {}
    if job_tier == 2:
        info = TIER2_JOBS.get(job_class)
        return {info["primary"]: 10, info["secondary"]: 5} if info else {}
    if job_tier == 3:
        info = TIER3_JOBS.get(job_class)
        return {info["primary"]: 15, info["secondary"]: 6} if info else {}
    if job_tier == 4:
        bonus = {"str": 8, "def": 8, "agi": 8, "luk": 8}
        dominant = next((k for k, v in TIER4_JOB_BY_STAT.items() if v == job_class), None)
        if dominant:
            bonus[dominant] += 20
        return bonus
    return {}


def _resolve_tier4_job(db, character_id):
    """Deterministic (not random) 四轉 outcome: tally primary(x2)/secondary(x1)
    stat weight across the character's 3 mastered 三轉 jobs, take the argmax
    stat; a tie among stats falls to the earth job."""
    tally = {"str": 0, "def": 0, "agi": 0, "luk": 0}
    for row in db.execute(
        "SELECT job_name FROM job_masteries WHERE character_id = ?", (character_id,)
    ):
        info = TIER3_JOBS.get(row["job_name"])
        if info:
            tally[info["primary"]] += 2
            tally[info["secondary"]] += 1
    best = max(tally.values())
    winners = [stat for stat, score in tally.items() if score == best]
    if len(winners) != 1:
        return TIER4_TIE_JOB
    return TIER4_JOB_BY_STAT[winners[0]]


def _process_job_progression(db, character, old_level, new_level):
    """Fires once per crossing into level 120 while in a 三轉 job. Registers
    (idempotently) mastery of the current job. 四轉 itself is no longer an
    automatic side effect of this crossing -- it's now an explicit player
    action (see character_promote_tier4 in app.py), so this function no
    longer returns anything for callers to merge into their UPDATE."""
    if character["job_tier"] != 3 or new_level < 120 or old_level >= 120:
        return None
    db.execute(
        "INSERT OR IGNORE INTO job_masteries (character_id, job_name) VALUES (?, ?)",
        (character["character_id"], character["job_class"]),
    )
    return None
