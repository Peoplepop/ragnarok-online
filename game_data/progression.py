import random

from db import LEVEL_CAP
from game_data.jobs import TIER1_JOBS, TIER2_JOBS, TIER3_JOBS, TIER4_JOBS

# Growth rate is tier-dependent (initiate/二轉/三轉/四轉), deliberately
# non-increasing band to band. Bands compound continuously across their
# boundary -- each new band picks up exactly where the previous one's cost
# left off (same magnitude at the first level of a band, then that band's
# own rate takes over from the next level on), so there's no discontinuity.
EXP_TIER_BANDS = [
    (1, 29, "exp_growth_novice_percent"),
    (30, 69, "exp_growth_tier2_percent"),
    (70, 119, "exp_growth_tier3_percent"),
    (120, LEVEL_CAP - 1, "exp_growth_tier4_percent"),
]

# Any character that hasn't reached 四轉 yet (job_tier < 4) stops gaining EXP
# at this level -- promoting/rebirthing onward (and eventually 四轉) is
# required to keep climbing toward LEVEL_CAP. Originally this only gated
# job_tier == 3, on the assumption that a 初心者/一轉/二轉 character would
# naturally promote once eligible -- but nothing actually stops a player from
# just not promoting and grinding straight through 120 while still low-tier,
# skipping the intended rebirth/mastery loop entirely. Applying the cap to
# every pre-四轉 tier closes that. 四轉 itself keeps going all the way to
# LEVEL_CAP.
TIER3_LEVEL_CAP = 120


def exp_required_for_level(level, settings, force_one=False):
    """EXP needed to advance from `level` to `level + 1`. force_one is the
    admin-testing override (session.is_admin) that always needs just 1 EXP,
    regardless of level."""
    if force_one:
        return 1
    anchor = settings["exp_base"]
    for start, end, field in EXP_TIER_BANDS:
        rate = settings[field]
        if level <= end:
            return round(anchor * (1 + rate / 100) ** (level - start))
        anchor *= (1 + rate / 100) ** (end - start)
    return round(anchor)


# Every level-up distributes exactly 10 points across hp/mp/str/def/agi/luk
# (never more than LEVEL_UP_STAT_POINT_CAP in any single stat, never fewer
# than 0), weighted toward the character's current job's specialty stat(s)
# so a level-up "feels like" that job -- a str/def build rolls more str/def,
# not a uniform spread. HP/MP points are worth more raw stat per point than
# the other four, matching the game's existing 10s-vs-1s stat granularity.
LEVEL_UP_TOTAL_POINTS = 10
LEVEL_UP_STAT_POINT_CAP = 5
LEVEL_UP_POINT_VALUE = {"hp": 10, "mp": 10, "str": 1, "def": 1, "agi": 1, "luk": 1}
LEVEL_UP_PRIMARY_WEIGHT = 3
LEVEL_UP_SECONDARY_WEIGHT = 2
LEVEL_UP_BASE_WEIGHT = 1


def _job_primary_secondary(job_class, job_tier):
    if job_tier == 1:
        info = TIER1_JOBS.get(job_class)
    elif job_tier == 2:
        info = TIER2_JOBS.get(job_class)
    elif job_tier == 3:
        info = TIER3_JOBS.get(job_class)
    elif job_tier == 4:
        info = TIER4_JOBS.get(job_class)
        return (info["dominant_stat"], None) if info else (None, None)
    else:
        info = None
    return (info["primary"], info["secondary"]) if info else (None, None)


def _roll_level_up_stat_points(job_class, job_tier):
    """Returns {stat: points} (0-5 each, summing to LEVEL_UP_TOTAL_POINTS) --
    still just points, not raw stat amounts; multiply by LEVEL_UP_POINT_VALUE
    to get the actual stat increase."""
    primary, secondary = _job_primary_secondary(job_class, job_tier)
    weights = {key: LEVEL_UP_BASE_WEIGHT for key in LEVEL_UP_POINT_VALUE}
    if primary:
        weights[primary] += LEVEL_UP_PRIMARY_WEIGHT
    if secondary:
        weights[secondary] += LEVEL_UP_SECONDARY_WEIGHT
    points = {key: 0 for key in weights}
    for _ in range(LEVEL_UP_TOTAL_POINTS):
        available = [key for key in weights if points[key] < LEVEL_UP_STAT_POINT_CAP]
        chosen = random.choices(available, weights=[weights[k] for k in available], k=1)[0]
        points[chosen] += 1
    return points


def apply_exp(level, exp, gained, settings, force_one=False, job_class=None, job_tier=0):
    """Add `gained` EXP, cascading through as many level-ups as it covers.
    Returns (new_level, new_exp, stat_gain) where stat_gain is the raw
    {stat: amount} total accumulated from a random job-weighted roll on each
    level gained (see _roll_level_up_stat_points) -- summed across every
    level-up in this call, since one big EXP gain can still cross several
    level thresholds if force_one or a huge kill allows it, even though
    overflow itself is discarded rather than carried. Capped at LEVEL_CAP
    (or, while still below 四轉/job_tier 4, at the lower TIER3_LEVEL_CAP --
    see that constant); extra EXP past the applicable cap is discarded.
    Overflow past what a level-up consumes is also discarded (not carried
    into the next
    level's counter) -- every level always starts counting from 0, so a
    single force_one level-up can only ever advance one level, not cascade
    through many."""
    exp += gained
    stat_gain = {key: 0 for key in LEVEL_UP_POINT_VALUE}
    effective_cap = TIER3_LEVEL_CAP if job_tier < 4 else LEVEL_CAP
    while level < effective_cap:
        needed = exp_required_for_level(level, settings, force_one)
        if exp < needed:
            break
        exp = 0
        level += 1
        for stat, points in _roll_level_up_stat_points(job_class, job_tier).items():
            stat_gain[stat] += points * LEVEL_UP_POINT_VALUE[stat]
    if level >= effective_cap:
        level, exp = effective_cap, 0
    return level, exp, stat_gain
