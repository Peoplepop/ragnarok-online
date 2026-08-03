from db import HIDDEN_SET_EFFECT_BY_ELEMENT
from game_data.constants import STAT_LABELS

# Country-themed equipment sets (db.py's DEFAULT_SET_ITEMS): items carry a
# `set_element` field (that item's origin country's element, via a JOIN --
# see _fetch_equipped_items) when they belong to one of these sets. Wearing
# 2 or 3 pieces from the *same* country's set grants a flat bonus on top of
# each piece's own stat_bonus. The 4 elemental countries concentrate their
# bonus on that element's signature stat (matching how the set's own 3
# pieces all buff that one stat too); the balanced earth country (土) has no
# single signature stat, so its bonus spreads evenly across all four instead.
SET_SIGNATURE_STAT = {"金": "luk", "木": "def", "水": "agi", "火": "str"}
SET_BONUS_TIERS = {2: 15, 3: 40}
EARTH_SET_BONUS_TIERS = {2: 8, 3: 20}

# Reverse of SET_SIGNATURE_STAT, for badging skills by the element whose
# signature stat they use (see the character sheet's skill panels). 土 has
# no signature stat so it never appears as a value here -- skills never get
# a 土 badge, which is expected (see SET_SIGNATURE_STAT's own docstring).
STAT_ELEMENT = {stat: element for element, stat in SET_SIGNATURE_STAT.items()}

# Reverse of db.py's HIDDEN_SET_EFFECT_BY_ELEMENT (element -> (stat, effect_key)),
# for badging hidden/boss-set equipment pieces by element: those pieces carry
# no country_id (so no set_element from the countries join) but do carry
# special_effect_key, which uniquely identifies the element.
EFFECT_KEY_ELEMENT = {
    effect_key: element
    for element, (_, effect_key) in HIDDEN_SET_EFFECT_BY_ELEMENT.items()
    if effect_key
}


def item_badge_element(item):
    """The element (金/木/水/火/土) an item's UI badge should show, or None
    for gear with no element affiliation. Ordinary country-set gear carries
    set_element (via the countries LEFT JOIN in _fetch_equipped_items and
    friends); hidden/boss-set pieces carry no country instead, so their
    special_effect_key is reverse-mapped through EFFECT_KEY_ELEMENT."""
    if item is None:
        return None
    try:
        element = item["set_element"]
    except (IndexError, KeyError):
        element = None
    if element:
        return element
    try:
        effect_key = item["special_effect_key"]
    except (IndexError, KeyError):
        return None
    return EFFECT_KEY_ELEMENT.get(effect_key)

# Separate, smaller bonus for equipment whose origin country matches the
# *wearer's own* country -- unlike the set bonus above (which only cares
# whether equipped pieces match each other), this one triggers off a single
# piece and stacks on top of the set bonus when both conditions are met (a
# player wearing 2-3 pieces of their own country's set gets both). Each tier
# is kept below its same-count SET_BONUS_TIERS/EARTH_SET_BONUS_TIERS value
# per the "不會比套裝加成多" requirement.
OWN_ELEMENT_BONUS_TIERS = {1: 6, 2: 12, 3: 30}
EARTH_OWN_ELEMENT_BONUS_TIERS = {1: 3, 2: 6, 3: 15}


def _equipment_set_bonus(equipped_items):
    counts = {}
    for item in equipped_items:
        element = item["set_element"] if item is not None else None
        if element:
            counts[element] = counts.get(element, 0) + 1
    bonus = {}
    for element, count in counts.items():
        if count < 2:
            continue
        tier = 3 if count >= 3 else 2
        if element == "土":
            for stat in ("str", "def", "agi", "luk"):
                bonus[stat] = bonus.get(stat, 0) + EARTH_SET_BONUS_TIERS[tier]
        else:
            stat = SET_SIGNATURE_STAT.get(element)
            if stat:
                bonus[stat] = bonus.get(stat, 0) + SET_BONUS_TIERS[tier]
    return bonus


def _own_element_bonus(equipped_items, own_element):
    if not own_element:
        return {}
    count = sum(1 for item in equipped_items if item is not None and item["set_element"] == own_element)
    if count < 1:
        return {}
    tier = min(count, 3)
    bonus = {}
    if own_element == "土":
        for stat in ("str", "def", "agi", "luk"):
            bonus[stat] = bonus.get(stat, 0) + EARTH_OWN_ELEMENT_BONUS_TIERS[tier]
    else:
        stat = SET_SIGNATURE_STAT.get(own_element)
        if stat:
            bonus[stat] = bonus.get(stat, 0) + OWN_ELEMENT_BONUS_TIERS[tier]
    return bonus


def _active_set_summaries(equipped_items):
    """Human-readable version of _equipment_set_bonus for the character
    sheet: one entry per country-set with >=2 pieces equipped, naming the
    country, how many pieces (of 3) are worn, and what that grants."""
    counts = {}
    names = {}
    for item in equipped_items:
        element = item["set_element"] if item is not None else None
        if element:
            counts[element] = counts.get(element, 0) + 1
            names[element] = item["set_country_name"]
    summaries = []
    for element, count in counts.items():
        if count < 2:
            continue
        tier = 3 if count >= 3 else 2
        if element == "土":
            bonus_text = "、".join(f"{STAT_LABELS[s]} +{EARTH_SET_BONUS_TIERS[tier]}" for s in ("str", "def", "agi", "luk"))
        else:
            stat = SET_SIGNATURE_STAT.get(element)
            bonus_text = f"{STAT_LABELS[stat]} +{SET_BONUS_TIERS[tier]}" if stat else ""
        summaries.append({
            "country_name": names[element], "count": count, "bonus_text": bonus_text, "element": element,
        })
    return summaries


def _own_element_bonus_summary(equipped_items, own_element):
    """Human-readable version of _own_element_bonus for the character sheet,
    mirroring _active_set_summaries. Returns None when nothing applies."""
    if not own_element:
        return None
    count = sum(1 for item in equipped_items if item is not None and item["set_element"] == own_element)
    if count < 1:
        return None
    tier = min(count, 3)
    if own_element == "土":
        bonus_text = "、".join(f"{STAT_LABELS[s]} +{EARTH_OWN_ELEMENT_BONUS_TIERS[tier]}" for s in ("str", "def", "agi", "luk"))
    else:
        stat = SET_SIGNATURE_STAT.get(own_element)
        bonus_text = f"{STAT_LABELS[stat]} +{OWN_ELEMENT_BONUS_TIERS[tier]}" if stat else ""
    return {"count": count, "bonus_text": bonus_text, "element": own_element}


# --- 秘境套裝 special effects ----------------------------------------------
# The 10 hidden-ground legendary sets (db.py's HIDDEN_SET_ITEMS) each carry a
# special_effect_key + special_effect_percent on all 3 of their pieces, on
# top of the ordinary stat_bonus every item has. Unlike the country sets
# above -- which pay a partial bonus from just 2 matching pieces -- a hidden
# set's special effect is strictly ALL-OR-NOTHING: 1 or 2 pieces grant only
# their own flat stat_bonus and nothing else; the effect switches on solely
# at a full 3/3 of the exact same hidden_set_key.
#
# In practice a character can only ever complete one hidden set at a time
# (there are only 3 equip slots in the whole game), but character_special_effects
# deliberately sums across every completed set rather than assuming that, so
# adding a 4th slot later wouldn't silently change the rule.
HIDDEN_SET_PIECES_REQUIRED = 3
SPECIAL_EFFECT_LABELS = {
    "gold_rate": "金幣掉落率",
    "exp_rate": "經驗值獲得",
    "recovery_discount": "回復費用折扣",
    "independent_damage": "獨立傷害",
    "enemy_debuff": "怪物弱化",
}


def _hidden_set_counts(equipped_items):
    """hidden_set_key -> (pieces equipped, set display name, effect key,
    effect percent per piece). Ignores ordinary gear, whose hidden_set_key is
    NULL. Tolerates rows fetched without the hidden columns (e.g. a caller
    passing plain dicts) by treating them as ordinary items."""
    counts = {}
    for item in equipped_items:
        if item is None:
            continue
        try:
            key = item["hidden_set_key"]
        except (IndexError, KeyError):
            continue
        if not key:
            continue
        entry = counts.setdefault(key, {
            "count": 0,
            "set_name": item["hidden_set_name"],
            "effect_key": item["special_effect_key"],
            "effect_percent": item["special_effect_percent"] or 0,
        })
        entry["count"] += 1
    return counts


def character_special_effects(equipped_items):
    """{special_effect_key: total percent} for every hidden set the wearer has
    FULLY equipped (all 3 pieces). Returns {} for an ordinary loadout, and
    also for a 1/3 or 2/3 partial -- see the all-or-nothing note above.

    The five keys and where each is consumed:
      gold_rate          -- game_hunt / game_hunt_boss_room currency payout
      exp_rate           -- game_hunt / game_hunt_boss_room exp payout
      recovery_discount  -- game_recover's HP/MP heal bill
      enemy_debuff       -- scales the fought monster's hp/atk/def/agi down
                            (hunts only -- never towers, garrison defenders
                            or tournament opponents)
      independent_damage -- bonus post-mitigation damage in _combat_hit,
                            active in EVERY fight the character takes part in
    """
    effects = {}
    for entry in _hidden_set_counts(equipped_items).values():
        if entry["count"] < HIDDEN_SET_PIECES_REQUIRED or not entry["effect_key"]:
            continue
        effects[entry["effect_key"]] = effects.get(entry["effect_key"], 0) + entry["effect_percent"]
    return effects


def _hidden_set_summaries(equipped_items):
    """Human-readable hidden-set status for the character sheet, mirroring
    _active_set_summaries. Unlike that one this also reports PARTIAL progress
    (1/3, 2/3) with active=False, so a player who found one piece can see how
    far they still are from switching the special effect on."""
    summaries = []
    for entry in _hidden_set_counts(equipped_items).values():
        active = entry["count"] >= HIDDEN_SET_PIECES_REQUIRED and bool(entry["effect_key"])
        label = SPECIAL_EFFECT_LABELS.get(entry["effect_key"], entry["effect_key"] or "")
        summaries.append({
            "set_name": entry["set_name"],
            "count": entry["count"],
            "required": HIDDEN_SET_PIECES_REQUIRED,
            "active": active,
            "effect_key": entry["effect_key"],
            "effect_label": label,
            "effect_percent": entry["effect_percent"],
            "effect_text": f"{label} +{entry['effect_percent']}%" if label else "",
            "element": EFFECT_KEY_ELEMENT.get(entry["effect_key"]),
        })
    summaries.sort(key=lambda s: (-s["count"], s["set_name"] or ""))
    return summaries


def item_set_effect_text(item):
    """Short Chinese description of what an item's set grants at 2/3 (or, for
    a 秘境 piece, 3/3) pieces -- independent of what else is currently
    equipped, unlike _active_set_summaries/_hidden_set_summaries above (which
    only report bonuses already active in the CURRENT loadout). For showing
    inline next to gear in an inventory listing, so a player can see the set
    payoff before ever equipping the first piece. None for gear with no set
    affiliation at all."""
    if item is None:
        return None
    try:
        hidden_set_key = item["hidden_set_key"]
    except (IndexError, KeyError):
        hidden_set_key = None
    if hidden_set_key:
        label = SPECIAL_EFFECT_LABELS.get(item["special_effect_key"], item["special_effect_key"] or "")
        percent = item["special_effect_percent"] or 0
        return f"《{item['hidden_set_name']}》秘境套裝：集滿 {HIDDEN_SET_PIECES_REQUIRED} 件才生效 → {label} +{percent}%"

    try:
        set_element = item["set_element"]
    except (IndexError, KeyError):
        set_element = None
    if not set_element:
        return None
    try:
        set_country_name = item["set_country_name"]
    except (IndexError, KeyError):
        set_country_name = None
    prefix = f"《{set_country_name}》套裝" if set_country_name else f"{set_element}套裝"
    if set_element == "土":
        tiers = "、".join(f"{n}件 四維各+{EARTH_SET_BONUS_TIERS[n]}" for n in (2, 3))
    else:
        stat = SET_SIGNATURE_STAT.get(set_element)
        tiers = "、".join(f"{n}件 {STAT_LABELS[stat]}+{SET_BONUS_TIERS[n]}" for n in (2, 3)) if stat else ""
    return f"{prefix}：{tiers}"


def _fetch_equipped_items(db, character):
    """Equipped weapon/armor/accessory rows, each carrying a `set_element`
    field (the item's origin country's element, NULL for ordinary gear) so
    compute_final_stats can tally equipment-set bonuses. The bare `items.*`
    also brings along hidden_set_key/hidden_set_name/special_effect_key/
    special_effect_percent (all NULL on ordinary gear) for
    character_special_effects."""
    equipped_ids = [
        character["equipped_weapon_id"], character["equipped_armor_id"], character["equipped_accessory_id"],
    ]
    return [
        db.execute(
            """SELECT items.*, items.hidden_set_key, items.hidden_set_name,
                      items.special_effect_key, items.special_effect_percent,
                      countries.element AS set_element, countries.name AS set_country_name
               FROM items LEFT JOIN countries ON countries.id = items.country_id
               WHERE items.id = ?""",
            (item_id,),
        ).fetchone()
        for item_id in equipped_ids if item_id
    ]
