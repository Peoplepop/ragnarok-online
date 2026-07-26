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
        summaries.append({"country_name": names[element], "count": count, "bonus_text": bonus_text})
    return summaries


def _fetch_equipped_items(db, character):
    """Equipped weapon/armor/accessory rows, each carrying a `set_element`
    field (the item's origin country's element, NULL for ordinary gear) so
    compute_final_stats can tally equipment-set bonuses."""
    equipped_ids = [
        character["equipped_weapon_id"], character["equipped_armor_id"], character["equipped_accessory_id"],
    ]
    return [
        db.execute(
            """SELECT items.*, countries.element AS set_element, countries.name AS set_country_name
               FROM items LEFT JOIN countries ON countries.id = items.country_id
               WHERE items.id = ?""",
            (item_id,),
        ).fetchone()
        for item_id in equipped_ids if item_id
    ]
