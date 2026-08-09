import itertools
from collections import Counter, defaultdict

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

JOB_TIER_LABELS = {0: "初心者", 1: "一轉", 2: "二轉", 3: "三轉", 4: "四轉"}

# ---------------------------------------------------------------------------
# 四轉 (job_tier=4): 200 jobs, deterministically resolved from a character's
# first 4 mastered 三轉 jobs (job_masteries, ordered mastered_at ASC, id ASC
# -- see _resolve_tier4_job). There are exactly C(12,4)=495 possible 4-job
# combinations of the 12 TIER3_JOBS; every one of them must resolve to
# exactly one of the 200 TIER4_JOB_ORDER names, with no gaps and no
# ambiguity. That resolution is derived below (_build_tier4_lookup), not
# hand-typed as a 495-row table, specifically so it can never silently drift
# out of sync with TIER3_JOBS, and so its coverage of all 495 combos is
# provable by re-running the derivation (see tests) instead of eyeballed.
#
# Design, in three steps:
#   1. Tally each combo's primary(x2)/secondary(x1) stat weight across its 4
#      jobs -- same arithmetic the old 3-mastery resolver used. There are
#      112 distinct exact (str,def,agi,luk) tallies reachable this way.
#   2. Group combos by their exact tally, then apportion TIER4_JOB_ORDER's
#      200 slots across those 112 groups via the largest-remainder method
#      (weighted by group size) -- richer tallies, i.e. tallies more combos
#      funnel into, earn more sub-variant jobs, everyone else gets exactly
#      one.
#   3. Where a tally-group earns more than 1 job, split it by
#      _tier4_spread_pattern (how the 4 jobs spread across the 3 root 一轉
#      philosophies -- a thematically real distinction: "mastered 4 jobs
#      from one lineage" reads differently than "touched all three"),
#      merging/splitting that natural partition until it exactly matches
#      the apportioned count.
#
# TIER4_JOB_ORDER is the one hand-authored artifact in this whole system: a
# flat list of 200 names in the exact canonical bucket order step 2/3
# produce (sorted by tally tuple, then sub-bucket index -- see
# _build_tier4_lookup). The 5 pre-existing jobs (see NPC_OFFICIAL_ROSTER in
# app.py, which hardcodes these 5 names onto NPC kings and must never break)
# sit at the position each one's stat already resolved to under the old
# single-stat-argmax system, so their meaning is unchanged: 業火尊者 only
# ever comes from the single most str-dominant tally, 青木道尊 the most
# def-dominant, 流水劍尊 the most agi-dominant, 流金尊者 the most
# luk-dominant, and 厚土真尊 the single most perfectly-balanced 4-way tie.
TIER4_JOB_ORDER = [
    '流金尊者', '鑄金尊者', '煉金神尊', '赤金聖皇', '紫金尊神',
    '玄金劍神', '寒金神皇', '瑞金聖王', '祥金帝君', '金烽尊主',
    '金曜靈皇', '金輝道皇', '金曦戰尊', '金霜道君', '寰宇尊者',
    '蒼木尊者', '乾坤神尊', '古木神尊', '金瀾真尊', '清流尊者',
    '金潮天尊', '鎏金聖君', '寒流神尊', '激流聖皇', '鎔金御皇',
    '鑠金法君', '金羽尊仙', '金翎道尊', '紫微法皇', '天地聖皇',
    '湍流尊神', '飛瀑劍神', '星命天君', '命盤尊帝', '卦靈劍聖',
    '玄卦仙尊', '四方尊神', '八荒劍神', '中央神皇', '黃庭聖王',
    '坤靈帝君', '后土尊主', '社稷靈皇', '山河道皇', '九州戰尊',
    '老藤聖皇', '深林尊神', '幽林劍神', '密林神皇', '森羅聖王',
    '林嵐帝君', '松柏尊主', '古柏靈皇', '蒼松道皇', '翠柏戰尊',
    '竹影道君', '寒竹真尊', '墨竹天尊', '楠木聖君', '古楠御皇',
    '樟木法君', '易卦王尊', '懸瀑神皇', '問卦聖尊', '推演宗主',
    '推命天皇', '寰中道君', '環宇真尊', '演卦劍皇', '卦數武尊',
    '天數法王', '氣數尊皇', '劫數御尊', '命數玄尊', '運數尊聖',
    '造化尊王', '碧波聖王', '滄波帝君', '滄浪尊主', '煙波靈皇',
    '天造尊師', '化機劍尊', '機緣靈尊', '太一天尊', '無極聖君',
    '老榕尊仙', '槐蔭道尊', '古槐法皇', '垂柳天君', '渾元御皇',
    '混元法君', '太極尊仙', '寒柳尊帝', '楓林劍聖', '丹楓仙尊',
    '梧桐王尊', '古桐聖尊', '荊棘宗主', '藤蔓天皇', '古藤劍皇',
    '青苔武尊', '蒼崖法王', '雲杉尊皇', '巨木御尊', '神木玄尊',
    '虯木尊聖', '虯藤尊王', '蒼藤尊師', '墨林劍尊', '幽篁靈尊',
    '青木道尊', '雲水道皇', '道樞道尊', '樞極法皇', '流水劍尊',
    '緣數帝尊', '福緣法尊', '流雲戰尊', '迅雷道君', '驚雷真尊',
    '閃電天尊', '祿星靈君', '瑞星仙君', '吉星尊者', '厚土真尊',
    '中樞天君', '寰極尊帝', '瞬影聖君', '流光御皇', '逐浪法君',
    '萬象劍聖', '元始仙尊', '洪荒王尊', '寒篁帝尊', '篁影法尊',
    '竹徑靈君', '磐石仙君', '巨巖尊者', '磐礎神尊', '厚壁聖皇',
    '混沌聖尊', '雄關尊神', '壁壘劍神', '城壘神皇', '鎮嶽聖王',
    '岳鎮帝君', '盤石尊主', '砥柱靈皇', '中流道皇', '定海戰尊',
    '鎮海道君', '靖嶽真尊', '寧嶽天尊', '安嶽聖君', '固嶽御皇',
    '穩岳法君', '巍峨尊仙', '巍嶽道尊', '太初宗主', '元和天皇',
    '沖和劍皇', '中和武尊', '圓融法王', '圓通尊皇', '炎陽尊者',
    '烈焰神尊', '丹霞聖皇', '赤霄尊神', '烽火劍神', '踏浪尊仙',
    '周天御尊', '大千玄尊', '寰界尊聖', '四海尊王', '五嶽尊師',
    '六合劍尊', '八方靈尊', '雄峰法皇', '絕壁天君', '懸崖尊帝',
    '千仞劍聖', '萬仞仙尊', '崇嶽王尊', '泰嶽聖尊', '華嶽宗主',
    '衡嶽天皇', '炙陽神皇', '熔岩聖王', '九野帝尊', '朱雀帝君',
    '焰嵐尊主', '灼日靈皇', '烈日道皇', '焱海戰尊', '炎獄道君',
    '十方法尊', '萬方靈君', '萬靈仙君', '業火尊者', '焚淵真尊',
]
_TIER4_STATS = ("str", "def", "agi", "luk")


def _tier4_family(tier3_job):
    """A 三轉 job's root 一轉 philosophy, looked up through its 二轉 parent --
    derived, never hand-duplicated, so it can't drift from TIER2_JOBS/
    TIER3_JOBS if either ever changes."""
    return TIER2_JOBS[TIER3_JOBS[tier3_job]["parent"]]["family"]


def _tier4_stat_tally(combo):
    """Same primary(x2)/secondary(x1) weighting the old 3-mastery resolver
    used, just always over exactly 4 jobs now (see the module comment above
    for why every character eligible to click 四轉 already has >=4 rows in
    job_masteries)."""
    tally = {stat: 0 for stat in _TIER4_STATS}
    for job in combo:
        info = TIER3_JOBS[job]
        tally[info["primary"]] += 2
        tally[info["secondary"]] += 1
    return tally


def _tier4_element(tally):
    """argmax stat, or None on any tie -- identical semantics to the old
    single-stat resolver, generalized here to pick which of the 200 jobs'
    "dominant_stat" a bucket gets (None = balanced/tie, same meaning
    厚土真尊 already carries today)."""
    best = max(tally.values())
    winners = [stat for stat, score in tally.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def _tier4_spread_pattern(combo):
    """How the combo's 4 jobs spread across the 3 root 一轉 philosophies,
    sorted so e.g. (2,1,1) means "touched all three families" regardless of
    which specific ones -- the differentiator used to split a same-tally
    group of combos into distinct named sub-variants (see
    _build_tier4_lookup)."""
    counts = Counter(_tier4_family(job) for job in combo)
    return tuple(sorted((counts.get(family, 0) for family in TIER1_JOBS), reverse=True))


def _tier4_rebalance(combo_list, target):
    """Partitions combo_list (every combo here shares one exact stat tally)
    into exactly `target` non-empty parts. Starts from the natural
    _tier4_spread_pattern grouping, then repeatedly merges the two smallest
    parts (if there are more parts than target) or splits the single
    largest part in half (if there are fewer), each step ordered by the
    parts' own sorted(combo) tuples -- fully deterministic, so re-running
    this always reproduces the identical partition bit for bit."""
    parts_map = defaultdict(list)
    for combo in combo_list:
        parts_map[_tier4_spread_pattern(combo)].append(combo)
    parts = [sorted(v) for _, v in sorted(parts_map.items())]

    while len(parts) > target:
        parts.sort(key=lambda p: (len(p), p[0]))
        a, b = parts.pop(0), parts.pop(0)
        parts.append(sorted(a + b))
        parts.sort(key=lambda p: p[0])
    while len(parts) < target:
        parts.sort(key=lambda p: (-len(p), p[0]))
        largest = parts.pop(0)
        mid = len(largest) // 2
        parts.append(largest[:mid])
        parts.append(largest[mid:])
        parts.sort(key=lambda p: p[0])

    parts.sort(key=lambda p: p[0])
    return parts


def _build_tier4_lookup():
    """Builds (TIER4_JOBS, TIER4_JOB_BY_COMBO) once at import time -- see the
    module comment above for the 3-step design. Raises loudly on any
    invariant violation (apportionment not summing to len(TIER4_JOB_ORDER),
    bucket count mismatch, incomplete combo coverage) rather than silently
    resolving some combo to the wrong job or crashing deep inside a request;
    matches this codebase's existing fail-fast style for seeded/derived data
    (see app.py's _seed_npc_officials skill-key check)."""
    combos = list(itertools.combinations(sorted(TIER3_JOBS), 4))

    groups = defaultdict(list)
    for combo in combos:
        tally = _tier4_stat_tally(combo)
        groups[tuple(tally[s] for s in _TIER4_STATS)].append(combo)
    for tup in groups:
        groups[tup].sort()
    group_keys = sorted(groups)

    total_target = len(TIER4_JOB_ORDER)
    sizes = {tup: len(groups[tup]) for tup in group_keys}
    weight = {tup: sizes[tup] - 1 for tup in group_keys}  # splittable extra capacity
    total_weight = sum(weight.values())
    remaining = total_target - len(group_keys)
    if total_weight <= 0 or remaining < 0:
        raise RuntimeError("tier4 apportionment has no room to reach TIER4_JOB_ORDER's length")

    quotas = {tup: remaining * weight[tup] / total_weight for tup in group_keys}
    extra = {tup: int(quotas[tup]) for tup in group_keys}
    shortfall = remaining - sum(extra.values())
    # give the shortfall to the largest fractional remainders (largest-
    # remainder / Hare apportionment), ties broken by canonical tally order
    for tup in sorted(group_keys, key=lambda t: (quotas[t] - extra[t]), reverse=True)[:shortfall]:
        extra[tup] += 1
    target_buckets = {tup: 1 + extra[tup] for tup in group_keys}

    if sum(target_buckets.values()) != total_target:
        raise RuntimeError("tier4 apportionment did not sum to TIER4_JOB_ORDER's length")

    buckets = []  # canonical order: position i here <-> TIER4_JOB_ORDER[i]
    for tup in group_keys:
        for part in _tier4_rebalance(groups[tup], target_buckets[tup]):
            buckets.append((tup, part))

    if len(buckets) != len(TIER4_JOB_ORDER):
        raise RuntimeError(
            f"tier4 bucket count ({len(buckets)}) doesn't match TIER4_JOB_ORDER "
            f"({len(TIER4_JOB_ORDER)}) -- name list and derivation are out of sync"
        )

    jobs = {}
    by_combo = {}
    for (tally_tuple, combo_list), job_name in zip(buckets, TIER4_JOB_ORDER):
        tally = dict(zip(_TIER4_STATS, tally_tuple))
        jobs[job_name] = {"dominant_stat": _tier4_element(tally)}
        for combo in combo_list:
            by_combo[frozenset(combo)] = job_name

    if len(by_combo) != len(combos):
        raise RuntimeError("tier4 combo coverage incomplete -- some 4-job combo has no resolved job")

    return jobs, by_combo


# TIER4_JOBS: {job_name: {"dominant_stat": stat_or_None}} -- the single
# source of truth every other tier4 consumer (job_stat_bonus_pct,
# _build_skill_catalog's tier4 loop, blueprints/admin.py's reference page)
# reads from, instead of each independently re-deriving or hardcoding "the
# tier4 jobs". TIER4_JOB_BY_COMBO: {frozenset of 4 job names: job_name},
# used only by _resolve_tier4_job below.
TIER4_JOBS, TIER4_JOB_BY_COMBO = _build_tier4_lookup()


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
        info = TIER4_JOBS.get(job_class)
        dominant = info["dominant_stat"] if info else None
        if dominant:
            bonus[dominant] += 20
        return bonus
    return {}


def _resolve_tier4_job(db, character_id):
    """Deterministic (not random) 四轉 outcome: the character's first 4
    mastered 三轉 jobs, in the chronological order they were mastered
    (mastered_at ASC, id ASC), key straight into TIER4_JOB_BY_COMBO. A
    player who keeps rebirthing past their first 4 masteries before ever
    clicking 四轉 (job_masteries has no upper bound -- character_rebirth only
    requires job_tier==3 and level>=120, not a mastery-count ceiling) still
    resolves off only those first 4; any masteries earned after that are
    simply never consulted here, by design (a player's *first* 4 masteries
    decide their fate)."""
    rows = db.execute(
        """SELECT job_name FROM job_masteries WHERE character_id = ?
           ORDER BY mastered_at ASC, id ASC LIMIT 4""",
        (character_id,),
    ).fetchall()
    combo = frozenset(row["job_name"] for row in rows)
    return TIER4_JOB_BY_COMBO[combo]


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
