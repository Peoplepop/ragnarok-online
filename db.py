import os
import sqlite3

from werkzeug.security import generate_password_hash

from map_layout import generate_layout

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "game.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "Gss#12345678"

DEFAULT_COUNTRIES = [
    {
        "name": "百鍊流金國", "element": "金",
        "description": "初始幸運值較高，閃避與命中俱佳",
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 15,
    },
    {
        "name": "翡翠靈木國", "element": "木",
        "description": "防禦與生命力驚人，減傷效果顯著",
        "hp_bonus": 8, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 15, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "蔚藍千泉國", "element": "水",
        "description": "身法飄逸，擅長先發制人與連續攻擊",
        "hp_bonus": 0, "mp_bonus": 0, "str_bonus": 0, "def_bonus": 0, "agi_bonus": 15, "luk_bonus": 0,
    },
    {
        "name": "紅蓮業火國", "element": "火",
        "description": "烈焰焚天，魔力與傷害兼備",
        "hp_bonus": 0, "mp_bonus": 8, "str_bonus": 15, "def_bonus": 0, "agi_bonus": 0, "luk_bonus": 0,
    },
    {
        "name": "萬物母育國", "element": "土",
        "description": "厚德載物，六圍均衡發展",
        "hp_bonus": 6, "mp_bonus": 6, "str_bonus": 6, "def_bonus": 6, "agi_bonus": 6, "luk_bonus": 6,
    },
]

# The bonuses DEFAULT_COUNTRIES originally shipped with (all a flat 1%, which
# rounds away to nothing until stats are fairly large) -- kept here so
# _upgrade_country_bonuses can safely retarget already-seeded rows to the
# stronger values above without clobbering any bonus an admin has since
# hand-edited in /admin.
LEGACY_DEFAULT_COUNTRY_BONUSES = {
    "百鍊流金國": (0, 0, 0, 0, 0, 1),
    "翡翠靈木國": (1, 0, 0, 1, 0, 0),
    "蔚藍千泉國": (0, 0, 0, 0, 1, 0),
    "紅蓮業火國": (0, 1, 1, 0, 0, 0),
    "萬物母育國": (1, 1, 1, 1, 1, 1),
}

# 五行相剋 (Wu Xing destructive cycle): key overcomes value.
ELEMENT_OVERCOMES = {"金": "木", "木": "土", "土": "水", "水": "火", "火": "金"}

LEVEL_CAP = 200

# The level cap used to be 1000 before the job/rebirth tier system replaced
# the flat exp curve -- kept so _upgrade_hunting_ground_bounds can recognize
# and bump an old "ultimate" hunting ground seeded under the old cap.
LEGACY_ULTIMATE_MAX_LEVEL = 1000

DEFAULT_HUNTING_GROUNDS = [
    {"tier": "beginner", "name": "初級打怪場", "min_level": 1, "max_level": 30, "monster_exp": 10},
    {"tier": "intermediate", "name": "中級打怪場", "min_level": 31, "max_level": 70, "monster_exp": 20},
    {"tier": "advanced", "name": "高級打怪場", "min_level": 71, "max_level": 120, "monster_exp": 40},
    {"tier": "ultimate", "name": "究級打怪場", "min_level": 121, "max_level": LEVEL_CAP, "monster_exp": 80},
]

DEFAULT_ITEMS = [
    {"shop_type": "weapon", "name": "木劍", "price": 20000, "stat": "str", "stat_bonus": 2, "country_name": None},
    {"shop_type": "weapon", "name": "鐵劍", "price": 35000, "stat": "str", "stat_bonus": 8, "country_name": None},
    {"shop_type": "weapon", "name": "秘銀劍", "price": 55000, "stat": "str", "stat_bonus": 20, "country_name": None},
    {"shop_type": "armor", "name": "布甲", "price": 20000, "stat": "def", "stat_bonus": 2, "country_name": None},
    {"shop_type": "armor", "name": "鐵甲", "price": 35000, "stat": "def", "stat_bonus": 8, "country_name": None},
    {"shop_type": "armor", "name": "龍鱗甲", "price": 55000, "stat": "def", "stat_bonus": 20, "country_name": None},
    {"shop_type": "accessory", "name": "銅戒指", "price": 20000, "stat": "luk", "stat_bonus": 2, "country_name": None},
    {"shop_type": "accessory", "name": "銀戒指", "price": 35000, "stat": "luk", "stat_bonus": 8, "country_name": None},
    {"shop_type": "accessory", "name": "金戒指", "price": 55000, "stat": "luk", "stat_bonus": 20, "country_name": None},
]

# Country-themed equipment sets: only sold in that country's own fortress
# shop (items.country_id), priced well above the top regular tier (800) per
# the "套裝必須比一般裝備貴" requirement. The 4 elemental countries stack all
# 3 pieces onto their own signature stat (so the set rewards committing to
# one stat hard); the balanced earth country instead spreads its 3 pieces
# across str/def/luk like ordinary gear, matching its "六圍均衡" theme. Set
# bonuses for wearing 2 or 3 pieces together are computed at combat-stat
# time in app.py (SET_BONUS_TIERS / EARTH_SET_BONUS_TIERS), not stored here.
SET_ITEM_PRICE = 70000
SET_ITEM_STAT_BONUS = 26
DEFAULT_SET_ITEMS = [
    {"shop_type": "weapon", "name": "流金劍", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "armor", "name": "流金鎧", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "accessory", "name": "流金墜飾", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "weapon", "name": "靈木劍", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "armor", "name": "靈木鎧", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "accessory", "name": "靈木墜飾", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "weapon", "name": "千泉劍", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "armor", "name": "千泉鎧", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "accessory", "name": "千泉墜飾", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "weapon", "name": "業火劍", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "armor", "name": "業火鎧", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "accessory", "name": "業火墜飾", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "weapon", "name": "母育劍", "stat": "str", "country_name": "萬物母育國"},
    {"shop_type": "armor", "name": "母育鎧", "stat": "def", "country_name": "萬物母育國"},
    {"shop_type": "accessory", "name": "母育墜飾", "stat": "luk", "country_name": "萬物母育國"},
]
for _set_item in DEFAULT_SET_ITEMS:
    _set_item["price"] = SET_ITEM_PRICE
    _set_item["stat_bonus"] = SET_ITEM_STAT_BONUS
DEFAULT_ITEMS = DEFAULT_ITEMS + DEFAULT_SET_ITEMS

# 官職套裝 (office-seat regalia): 3 more per-country tiers above the ordinary
# DEFAULT_SET_ITEMS, seeded so every country's 國王/參謀/大將軍 seat starts
# with a real NPC officeholder already dressed in the appropriate set (see
# _seed_npc_officials in app.py) -- but these are ordinary purchasable shop
# items like everything else above (same country-scoped fortress-shop
# convention via items.country_id), anyone can buy and wear them, the NPCs
# just happen to start wearing them. Because they carry the same country_id
# as that country's own DEFAULT_SET_ITEMS, they count toward the exact same
# _equipment_set_bonus tally (grouped by country element) as the ordinary
# set -- there's no separate "regalia set" bonus mechanic.
GENERAL_SET_ITEM_PRICE = 85000
GENERAL_SET_ITEM_STAT_BONUS = 34
ADVISOR_SET_ITEM_PRICE = 85000
ADVISOR_SET_ITEM_STAT_BONUS = 34
KING_SET_ITEM_PRICE = 100000
KING_SET_ITEM_STAT_BONUS = 44

# 大將軍套裝 (General): attack-focused, always buffs str regardless of country.
DEFAULT_GENERAL_SET_ITEMS = [
    {"shop_type": "weapon", "name": "流金戰劍", "stat": "str", "country_name": "百鍊流金國"},
    {"shop_type": "armor", "name": "流金戰甲", "stat": "str", "country_name": "百鍊流金國"},
    {"shop_type": "accessory", "name": "流金戰印", "stat": "str", "country_name": "百鍊流金國"},
    {"shop_type": "weapon", "name": "靈木戰劍", "stat": "str", "country_name": "翡翠靈木國"},
    {"shop_type": "armor", "name": "靈木戰甲", "stat": "str", "country_name": "翡翠靈木國"},
    {"shop_type": "accessory", "name": "靈木戰印", "stat": "str", "country_name": "翡翠靈木國"},
    {"shop_type": "weapon", "name": "千泉戰劍", "stat": "str", "country_name": "蔚藍千泉國"},
    {"shop_type": "armor", "name": "千泉戰甲", "stat": "str", "country_name": "蔚藍千泉國"},
    {"shop_type": "accessory", "name": "千泉戰印", "stat": "str", "country_name": "蔚藍千泉國"},
    {"shop_type": "weapon", "name": "業火戰劍", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "armor", "name": "業火戰甲", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "accessory", "name": "業火戰印", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "weapon", "name": "母育戰劍", "stat": "str", "country_name": "萬物母育國"},
    {"shop_type": "armor", "name": "母育戰甲", "stat": "str", "country_name": "萬物母育國"},
    {"shop_type": "accessory", "name": "母育戰印", "stat": "str", "country_name": "萬物母育國"},
]
for _set_item in DEFAULT_GENERAL_SET_ITEMS:
    _set_item["price"] = GENERAL_SET_ITEM_PRICE
    _set_item["stat_bonus"] = GENERAL_SET_ITEM_STAT_BONUS
DEFAULT_ITEMS = DEFAULT_ITEMS + DEFAULT_GENERAL_SET_ITEMS

# 參謀套裝 (Advisor): defense-focused, always buffs def regardless of country.
DEFAULT_ADVISOR_SET_ITEMS = [
    {"shop_type": "weapon", "name": "流金策劍", "stat": "def", "country_name": "百鍊流金國"},
    {"shop_type": "armor", "name": "流金策鎧", "stat": "def", "country_name": "百鍊流金國"},
    {"shop_type": "accessory", "name": "流金策珮", "stat": "def", "country_name": "百鍊流金國"},
    {"shop_type": "weapon", "name": "靈木策劍", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "armor", "name": "靈木策鎧", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "accessory", "name": "靈木策珮", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "weapon", "name": "千泉策劍", "stat": "def", "country_name": "蔚藍千泉國"},
    {"shop_type": "armor", "name": "千泉策鎧", "stat": "def", "country_name": "蔚藍千泉國"},
    {"shop_type": "accessory", "name": "千泉策珮", "stat": "def", "country_name": "蔚藍千泉國"},
    {"shop_type": "weapon", "name": "業火策劍", "stat": "def", "country_name": "紅蓮業火國"},
    {"shop_type": "armor", "name": "業火策鎧", "stat": "def", "country_name": "紅蓮業火國"},
    {"shop_type": "accessory", "name": "業火策珮", "stat": "def", "country_name": "紅蓮業火國"},
    {"shop_type": "weapon", "name": "母育策劍", "stat": "def", "country_name": "萬物母育國"},
    {"shop_type": "armor", "name": "母育策鎧", "stat": "def", "country_name": "萬物母育國"},
    {"shop_type": "accessory", "name": "母育策珮", "stat": "def", "country_name": "萬物母育國"},
]
for _set_item in DEFAULT_ADVISOR_SET_ITEMS:
    _set_item["price"] = ADVISOR_SET_ITEM_PRICE
    _set_item["stat_bonus"] = ADVISOR_SET_ITEM_STAT_BONUS
DEFAULT_ITEMS = DEFAULT_ITEMS + DEFAULT_ADVISOR_SET_ITEMS

# 國王套裝 (King): the best set, buffs the country's own signature stat (the
# 4 elemental countries concentrate all 3 pieces on their one signature stat,
# same as their ordinary DEFAULT_SET_ITEMS; 萬物母育國 spreads it exactly like
# its existing 母育劍(str)/母育鎧(def)/母育墜飾(luk) set does).
DEFAULT_KING_SET_ITEMS = [
    {"shop_type": "weapon", "name": "流金御劍", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "armor", "name": "流金御鎧", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "accessory", "name": "流金御冠", "stat": "luk", "country_name": "百鍊流金國"},
    {"shop_type": "weapon", "name": "靈木御劍", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "armor", "name": "靈木御鎧", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "accessory", "name": "靈木御冠", "stat": "def", "country_name": "翡翠靈木國"},
    {"shop_type": "weapon", "name": "千泉御劍", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "armor", "name": "千泉御鎧", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "accessory", "name": "千泉御冠", "stat": "agi", "country_name": "蔚藍千泉國"},
    {"shop_type": "weapon", "name": "業火御劍", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "armor", "name": "業火御鎧", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "accessory", "name": "業火御冠", "stat": "str", "country_name": "紅蓮業火國"},
    {"shop_type": "weapon", "name": "母育御劍", "stat": "str", "country_name": "萬物母育國"},
    {"shop_type": "armor", "name": "母育御鎧", "stat": "def", "country_name": "萬物母育國"},
    {"shop_type": "accessory", "name": "母育御冠", "stat": "luk", "country_name": "萬物母育國"},
]
for _set_item in DEFAULT_KING_SET_ITEMS:
    _set_item["price"] = KING_SET_ITEM_PRICE
    _set_item["stat_bonus"] = KING_SET_ITEM_STAT_BONUS
DEFAULT_ITEMS = DEFAULT_ITEMS + DEFAULT_KING_SET_ITEMS

# --- 秘境套裝 (hidden-ground legendary sets) --------------------------------
# 10 sets = 5 elements x 2 hidden grounds, 3 pieces each (weapon/armor/
# accessory), 30 rows total. Same stat convention as every country set above:
# the 4 elemental sets put all 3 pieces on that element's signature stat
# (SET_SIGNATURE_STAT in game_data/equipment.py), and the 土 set spreads its
# pieces across str/def/luk exactly like 母育劍/母育鎧/母育墜飾 does.
#
# These are NOT purchasable: country_id stays NULL (not nation-scoped),
# price is 0, and game_shop's listing query excludes every row with a
# non-NULL hidden_set_key. The one and only way to obtain a piece is the
# post-win drop roll on a hidden-ground fight (see game_hunt).
#
# stat_bonus is set well above the current best-in-slot: the top purchasable
# tier is 國王套裝 at KING_SET_ITEM_STAT_BONUS (44), so 太極 pieces sit at 60
# and 無極 pieces at 80. Each piece additionally carries a special_effect_key
# that only activates at a full 3/3 of the SAME hidden_set_key.
TAIJI_SET_ITEM_STAT_BONUS = 60
WUJI_SET_ITEM_STAT_BONUS = 80
TAIJI_SPECIAL_EFFECT_PERCENT = 8
WUJI_SPECIAL_EFFECT_PERCENT = 15
HIDDEN_LOOT_ITEM_PRICE = 0

# element -> (signature stat, special_effect_key). The 土 entry's stat is None
# because that set spreads across three different stats instead (see
# _HIDDEN_EARTH_SLOT_STATS).
HIDDEN_SET_EFFECT_BY_ELEMENT = {
    "金": ("luk", "gold_rate"),
    "木": ("def", "exp_rate"),
    "水": ("agi", "recovery_discount"),
    "火": ("str", "independent_damage"),
    "土": (None, "enemy_debuff"),
}
_HIDDEN_EARTH_SLOT_STATS = {"weapon": "str", "armor": "def", "accessory": "luk"}
_HIDDEN_SLOT_ORDER = ("weapon", "armor", "accessory")

# (map_prefix, element) -> (hidden_set_key, set display name, per-slot piece names)
_HIDDEN_SET_DEFS = [
    ("taiji", "金", "taiji_metal", "白虎鑄金套裝", ("白虎鑄金劍", "白虎鑄金鎧", "白虎鑄金符")),
    ("taiji", "木", "taiji_wood", "青龍蒼木套裝", ("青龍蒼木劍", "青龍蒼木鎧", "青龍蒼木符")),
    ("taiji", "水", "taiji_water", "玄武流水套裝", ("玄武流水劍", "玄武流水鎧", "玄武流水符")),
    ("taiji", "火", "taiji_fire", "朱雀丹火套裝", ("朱雀丹火劍", "朱雀丹火鎧", "朱雀丹火符")),
    ("taiji", "土", "taiji_earth", "黃龍厚土套裝", ("黃龍厚土劍", "黃龍厚土鎧", "黃龍厚土符")),
    ("wuji", "金", "wuji_metal", "白帝鑄金套裝", ("白帝鑄金劍", "白帝鑄金鎧", "白帝鑄金符")),
    ("wuji", "木", "wuji_wood", "青帝蒼木套裝", ("青帝蒼木劍", "青帝蒼木鎧", "青帝蒼木符")),
    ("wuji", "水", "wuji_water", "黑帝玄水套裝", ("黑帝玄水劍", "黑帝玄水鎧", "黑帝玄水符")),
    ("wuji", "火", "wuji_fire", "赤帝烈火套裝", ("赤帝烈火劍", "赤帝烈火鎧", "赤帝烈火符")),
    ("wuji", "土", "wuji_earth", "黃帝厚土套裝", ("黃帝厚土劍", "黃帝厚土鎧", "黃帝厚土符")),
]


def _build_hidden_set_items():
    items = []
    for map_prefix, element, set_key, set_name, piece_names in _HIDDEN_SET_DEFS:
        signature_stat, effect_key = HIDDEN_SET_EFFECT_BY_ELEMENT[element]
        if map_prefix == "taiji":
            stat_bonus, effect_percent = TAIJI_SET_ITEM_STAT_BONUS, TAIJI_SPECIAL_EFFECT_PERCENT
        else:
            stat_bonus, effect_percent = WUJI_SET_ITEM_STAT_BONUS, WUJI_SPECIAL_EFFECT_PERCENT
        for slot, piece_name in zip(_HIDDEN_SLOT_ORDER, piece_names):
            items.append({
                # shop_type doubles as this codebase's equip-SLOT key
                # (EQUIP_SLOT_COLUMNS / SLOT_LABELS / the inventory grouping in
                # game_shop and character_page), so a hidden piece still has to
                # be a real weapon/armor/accessory to be wearable at all --
                # "unpurchasable" is enforced by hidden_set_key instead.
                "shop_type": slot,
                "name": piece_name,
                "price": HIDDEN_LOOT_ITEM_PRICE,
                "stat": signature_stat or _HIDDEN_EARTH_SLOT_STATS[slot],
                "stat_bonus": stat_bonus,
                "country_name": None,
                "hidden_set_key": set_key,
                "hidden_set_name": set_name,
                "special_effect_key": effect_key,
                "special_effect_percent": effect_percent,
                "hidden_map": map_prefix,
            })
    return items


HIDDEN_SET_ITEMS = _build_hidden_set_items()
DEFAULT_ITEMS = DEFAULT_ITEMS + HIDDEN_SET_ITEMS

# map prefix -> its 5 hidden_set_key values. The drop roll selects uniformly
# over the 15 item rows (5 sets x 3 slots) carrying these keys, so it needs
# the exact key list rather than a LIKE pattern (a hidden_set_key contains an
# underscore, which is a LIKE wildcard).
HIDDEN_SET_KEYS_BY_MAP = {}
for _map_prefix, _element, _set_key, _set_name, _piece_names in _HIDDEN_SET_DEFS:
    HIDDEN_SET_KEYS_BY_MAP.setdefault(_map_prefix, []).append(_set_key)

ITEM_HIDDEN_COLUMNS = (
    "hidden_set_key", "hidden_set_name", "special_effect_key", "special_effect_percent",
)

# Monster roster, generated rather than hand-typed: every 5-level bracket
# within a hunting ground gets 2 regular monsters (two long-running species
# per tier, escalating through an adjective ladder as the bracket climbs),
# plus exactly one 守衛怪 (guardian) and one 魔王 (boss, at the tier's milestone
# level: 30/70/120/200) per tier. Regular-monster stats interpolate linearly
# from a tier's low anchor (its first bracket) to its high anchor (its last
# bracket, i.e. the milestone level); guardian = high anchor x1.2, boss = high
# anchor x1.5 (per design: "魔王比一般[milestone]級怪物的各項屬性再多50%").
_MONSTER_TIER_CONFIG = [
    {
        "tier": "beginner", "min_level": 1, "max_level": 30, "brackets": 6,
        "low": {"hp": 70, "atk": 14, "def": 6, "agi": 10, "currency_reward": 15, "exp_reward": 9},
        "high": {"hp": 220, "atk": 24, "def": 10, "agi": 18, "currency_reward": 70, "exp_reward": 15},
        "species": [("野狼", "木"), ("山豬", "土")],
        "adjectives": ["弱小", "普通", "精壯", "兇猛", "兇暴", "狂暴"],
        "guardian": {"name": "荒野守衛犀", "element": "土"},
        "boss": {"name": "荒原狼王", "element": "木"},
    },
    {
        "tier": "intermediate", "min_level": 31, "max_level": 70, "brackets": 8,
        "low": {"hp": 170, "atk": 27, "def": 14, "agi": 19, "currency_reward": 38, "exp_reward": 18},
        "high": {"hp": 480, "atk": 42, "def": 22, "agi": 30, "currency_reward": 160, "exp_reward": 28},
        "species": [("蜥蜴", "火"), ("遊魂", "水")],
        "adjectives": ["幼年", "普通", "精壯", "兇猛", "猛烈", "兇暴", "狂暴", "嗜血"],
        "guardian": {"name": "熔岩守衛犬", "element": "火"},
        "boss": {"name": "熔岩巨蠍王", "element": "火"},
    },
    {
        "tier": "advanced", "min_level": 71, "max_level": 120, "brackets": 10,
        "low": {"hp": 320, "atk": 50, "def": 25, "agi": 30, "currency_reward": 75, "exp_reward": 36},
        "high": {"hp": 800, "atk": 75, "def": 38, "agi": 48, "currency_reward": 320, "exp_reward": 54},
        "species": [("巨魔", "金"), ("劍靈", "水")],
        "adjectives": ["幼年", "普通", "精壯", "兇猛", "猛烈", "兇暴", "狂暴", "嗜血", "煞氣", "修羅化"],
        "guardian": {"name": "幽冥守衛靈", "element": "水"},
        "boss": {"name": "深淵魔狼王", "element": "水"},
    },
    {
        "tier": "ultimate", "min_level": 121, "max_level": LEVEL_CAP, "brackets": 16,
        "low": {"hp": 620, "atk": 85, "def": 42, "agi": 48, "currency_reward": 160, "exp_reward": 72},
        "high": {"hp": 1400, "atk": 130, "def": 65, "agi": 75, "currency_reward": 650, "exp_reward": 104},
        "species": [("巨龍裔", "金"), ("石像鬼", "土")],
        "adjectives": [
            "幼年", "普通", "精壯", "兇猛", "猛烈", "兇暴", "狂暴", "嗜血",
            "煞氣", "修羅化", "半神化", "神威", "天怒", "滅世", "混沌", "終焉",
        ],
        "guardian": {"name": "虛空守衛神", "element": "土"},
        "boss": {"name": "終焉魔神", "element": "火"},
    },
]

GUARDIAN_STAT_MULT = 1.2
GUARDIAN_CURRENCY_MULT = 2.5
BOSS_STAT_MULT = 1.5
BOSS_CURRENCY_MULT = 5.0
_STAT_KEYS = ("hp", "atk", "def", "agi")


def _build_default_monsters():
    monsters = []
    for cfg in _MONSTER_TIER_CONFIG:
        n = cfg["brackets"]
        low, high = cfg["low"], cfg["high"]
        for i, adjective in enumerate(cfg["adjectives"]):
            t = i / (n - 1) if n > 1 else 0
            stats = {k: round(low[k] + (high[k] - low[k]) * t) for k in _STAT_KEYS}
            currency = round(low["currency_reward"] + (high["currency_reward"] - low["currency_reward"]) * t)
            exp = round(low["exp_reward"] + (high["exp_reward"] - low["exp_reward"]) * t)
            level_min = cfg["min_level"] + i * 5
            level_max = level_min + 4
            for species, element in cfg["species"]:
                monsters.append({
                    "tier": cfg["tier"], "name": f"{adjective}{species}", "is_boss": 0, "is_guardian": 0,
                    "level_min": level_min, "level_max": level_max,
                    "hp": stats["hp"], "atk": stats["atk"], "def": stats["def"], "agi": stats["agi"],
                    "currency_reward": currency, "exp_reward": exp, "element": element,
                })
        guardian_stats = {k: round(high[k] * GUARDIAN_STAT_MULT) for k in _STAT_KEYS}
        monsters.append({
            "tier": cfg["tier"], "name": cfg["guardian"]["name"], "is_boss": 0, "is_guardian": 1,
            "level_min": None, "level_max": None,
            "hp": guardian_stats["hp"], "atk": guardian_stats["atk"],
            "def": guardian_stats["def"], "agi": guardian_stats["agi"],
            "currency_reward": round(high["currency_reward"] * GUARDIAN_CURRENCY_MULT),
            "exp_reward": high["exp_reward"],
            "element": cfg["guardian"]["element"],
        })
        boss_stats = {k: round(high[k] * BOSS_STAT_MULT) for k in _STAT_KEYS}
        monsters.append({
            "tier": cfg["tier"], "name": cfg["boss"]["name"], "is_boss": 1, "is_guardian": 0,
            "level_min": None, "level_max": None,
            "hp": boss_stats["hp"], "atk": boss_stats["atk"],
            "def": boss_stats["def"], "agi": boss_stats["agi"],
            "currency_reward": round(high["currency_reward"] * BOSS_CURRENCY_MULT),
            "exp_reward": high["exp_reward"],
            "element": cfg["boss"]["element"],
        })
    return monsters


DEFAULT_MONSTERS = _build_default_monsters()

# --- 秘境 (hidden grounds) --------------------------------------------------
# Two extra hunting_grounds rows flagged is_hidden=1, each holding exactly ONE
# monster that is neither a 守衛怪 nor a 魔王 (is_guardian=0 AND is_boss=0) --
# game_hunt treats a hidden encounter as its own third branch rather than
# reusing either flag, and no 魔王房間 follow-up chain exists for them.
#
# min_level/max_level are informational only (nothing filters on them for a
# hidden ground, since the encounter is a random interrupt rather than a
# player-chosen destination); they're set to the level band the fight is
# actually tuned for, and they also decide where the ground sorts in the
# admin-only forced-monster dropdown (ORDER BY hunting_grounds.min_level).
HIDDEN_HUNTING_GROUNDS = [
    {"tier": "taiji_hidden", "name": "太極秘境", "min_level": 100, "max_level": LEVEL_CAP,
     "monster_exp": 0, "is_hidden": 1},
    {"tier": "wuji_hidden", "name": "無極秘境", "min_level": 120, "max_level": LEVEL_CAP,
     "monster_exp": 0, "is_hidden": 1},
]

# Stats below were NOT hand-guessed -- they were calibrated the same way
# BANDIT_HP_MULTIPLIER was (see _bandit_lord_stats in game_data/stats.py), by
# running repeated run_battle simulations against representative test
# characters built through the real character_final_stats path (real country
# bonuses, real job bonuses, real rebirth stacking, real equipped 國王套裝 gear
# with its set + own-element bonuses, real level-by-level accumulated
# level_bonus_* rolls, real equipped skills) and moving hp/atk/def/agi until
# the intended win-rate story held. Six test builds were used: a 三轉 Lv100 in
# ordinary shop gear, four differently-specced well-built 三轉 Lv100s (str /
# agi / def / luk leaning, each in its own country's 國王套裝 with its two
# strongest 三轉 skills), a maxed 三轉 Lv140 with 3 rebirths, plus 四轉 Lv150
# and Lv200. 200-300 fights each. Measured result (see the report in the
# feature's verification notes):
#   陰陽尊者 (太極秘境): the four well-built 三轉 Lv100 archetypes averaged
#     ~60% wins -- genuinely winnable but never a formality, and the spread
#     across archetypes (a str-leaning build ~20%, an agi-leaning one ~100%)
#     is the combat engine's own pre-existing attacks-per-round bias, not
#     something this monster introduces. The same character in ordinary shop
#     gear won 0/200, so the wall really is "go grind gear and skills".
#   混沌天尊 (無極秘境): every 三轉 Lv100 build won 0/150, and even the maxed
#     三轉 Lv140 with 3 rebirths and full 國王套裝 won under 1%. 四轉 Lv150 wins
#     about half its attempts and 四轉 Lv200 essentially always -- i.e. it is
#     a 四轉-only fight, exactly as specified.
# The DEF gap between the two (150 vs 220) is what makes 混沌天尊 unreachable
# rather than merely slow: at 220 DEF a 三轉's damage is cut hard enough that
# it dies to the 450 ATK long before the round cap, so failure is a real
# defeat and not a no-loss timeout (neither monster produced any timeout in
# any of the simulated fights).
#
# element is deliberately '' (neutral): neither is aligned to a Wu Xing side,
# so no country gets a 相剋 advantage or penalty against them.
#
# Rewards are flat values chosen to be the best PvE payout in the game by a
# wide margin: the 究級 魔王 pays 3250 currency and 104 base exp (x5 via
# boss_exp_multiplier = 520 effective). A hidden monster gets NO multiplier
# (it is neither boss nor guardian), so these raw numbers are what's paid.
HIDDEN_MONSTERS = [
    {
        "tier": "taiji_hidden", "name": "陰陽尊者", "is_boss": 0, "is_guardian": 0,
        "level_min": None, "level_max": None,
        "hp": 27000, "atk": 240, "def": 150, "agi": 90,
        "currency_reward": 20000, "exp_reward": 3000, "element": "",
    },
    {
        "tier": "wuji_hidden", "name": "混沌天尊", "is_boss": 0, "is_guardian": 0,
        "level_min": None, "level_max": None,
        "hp": 38000, "atk": 450, "def": 220, "agi": 130,
        "currency_reward": 33000, "exp_reward": 5000, "element": "",
    },
]

# tier -> the hidden ground a fired trigger sends the hunter to, and which
# game_settings columns gate it. Single source of truth shared by game_hunt's
# trigger roll, its drop roll, and the tests.
HIDDEN_GROUND_TIERS = {
    "taiji": {
        "tier": "taiji_hidden", "trigger_setting": "hidden_taiji_trigger_percent",
        "drop_setting": "hidden_taiji_drop_percent", "banner": "⚡ 意外踏入太極秘境！",
    },
    "wuji": {
        "tier": "wuji_hidden", "trigger_setting": "hidden_wuji_trigger_percent",
        "drop_setting": "hidden_wuji_drop_percent", "banner": "⚡ 意外踏入無極秘境！",
    },
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_is_admin_column(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    if "is_npc" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_npc INTEGER NOT NULL DEFAULT 0")


def _ensure_session_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
    if "last_login_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
    if "last_seen_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
    if "is_online" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_online INTEGER NOT NULL DEFAULT 0")
    if "is_locked" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0")


def _ensure_user_avatar_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)")]
    if "avatar_key" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_key TEXT NOT NULL DEFAULT 'avatar_01'")
    if "avatar_custom_filename" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_custom_filename TEXT")


def _ensure_character_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(characters)")]
    if "current_tile_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN current_tile_id INTEGER")
    if "currency" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN currency INTEGER NOT NULL DEFAULT 1000")
    if "level" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
    if "exp" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN exp INTEGER NOT NULL DEFAULT 0")
    if "next_action_at" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN next_action_at TEXT")
    if "equipped_weapon_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_weapon_id INTEGER")
    if "equipped_armor_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_armor_id INTEGER")
    if "equipped_accessory_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_accessory_id INTEGER")
    if "current_hp" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN current_hp INTEGER")
    if "current_mp" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN current_mp INTEGER")
    if "battles_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN battles_count INTEGER NOT NULL DEFAULT 0")
    if "wins_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN wins_count INTEGER NOT NULL DEFAULT 0")
    if "pvp_battles_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN pvp_battles_count INTEGER NOT NULL DEFAULT 0")
    if "pvp_wins_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN pvp_wins_count INTEGER NOT NULL DEFAULT 0")
    if "bank_balance" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN bank_balance INTEGER NOT NULL DEFAULT 0")
    if "job_class" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN job_class TEXT NOT NULL DEFAULT '初心者'")
    if "job_tier" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN job_tier INTEGER NOT NULL DEFAULT 0")
    if "rebirth_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN rebirth_count INTEGER NOT NULL DEFAULT 0")
    for stat_col in ("stat_floor_hp", "stat_floor_mp", "stat_floor_str",
                     "stat_floor_def", "stat_floor_agi", "stat_floor_luk"):
        if stat_col not in cols:
            conn.execute(f"ALTER TABLE characters ADD COLUMN {stat_col} INTEGER")
    if "pending_boss_monster_id" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN pending_boss_monster_id INTEGER")
    for bonus_col in ("level_bonus_hp", "level_bonus_mp", "level_bonus_str",
                      "level_bonus_def", "level_bonus_agi", "level_bonus_luk"):
        if bonus_col not in cols:
            conn.execute(f"ALTER TABLE characters ADD COLUMN {bonus_col} INTEGER NOT NULL DEFAULT 0")
    if "equipped_skill_1" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_skill_1 TEXT")
    if "equipped_skill_2" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN equipped_skill_2 TEXT")
    if "is_npc" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN is_npc INTEGER NOT NULL DEFAULT 0")
    if "rename_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN rename_count INTEGER NOT NULL DEFAULT 0")
    if "avatar_change_count" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN avatar_change_count INTEGER NOT NULL DEFAULT 0")
    if "tutorial_seen" not in cols:
        # DEFAULT 1 backfills every EXISTING character as already-seen (an
        # established veteran shouldn't suddenly get nagged with the
        # tutorial prompt after this migration runs) -- _create_character()
        # explicitly passes tutorial_seen=0 on INSERT so only genuinely new
        # characters get prompted.
        conn.execute("ALTER TABLE characters ADD COLUMN tutorial_seen INTEGER NOT NULL DEFAULT 1")
    if "name" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN name TEXT")
        conn.execute(
            """UPDATE characters SET name = (
                   SELECT username FROM users WHERE users.id = characters.user_id
               ) WHERE name IS NULL"""
        )
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_characters_name ON characters(name)")
        except sqlite3.IntegrityError:
            pass
    if "contribution" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN contribution INTEGER NOT NULL DEFAULT 0")
    if "donated_today" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN donated_today INTEGER NOT NULL DEFAULT 0")
    if "donated_today_date" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN donated_today_date TEXT")
    if "garrison_cooldown_until" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN garrison_cooldown_until TEXT")
    if "income_claimed_at" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN income_claimed_at TEXT")
    if "siege_attack_next_at" not in cols:
        conn.execute("ALTER TABLE characters ADD COLUMN siege_attack_next_at TEXT")


def _ensure_country_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(countries)")]
    if "treasury" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN treasury INTEGER NOT NULL DEFAULT 0")
    if "king_character_id" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN king_character_id INTEGER")
    if "advisor_character_id" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN advisor_character_id INTEGER")
    if "general_character_id" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN general_character_id INTEGER")
    if "pending_challenge_seat" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN pending_challenge_seat TEXT")
    if "pending_challenge_character_id" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN pending_challenge_character_id INTEGER")
    if "pending_challenge_authorized_at" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN pending_challenge_authorized_at TEXT")
    if "morale_buff_expires_at" not in cols:
        conn.execute("ALTER TABLE countries ADD COLUMN morale_buff_expires_at TEXT")


def _ensure_monster_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(monsters)")]
    if "element" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN element TEXT NOT NULL DEFAULT ''")
    if "is_guardian" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN is_guardian INTEGER NOT NULL DEFAULT 0")
    if "level_min" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN level_min INTEGER")
    if "level_max" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN level_max INTEGER")
    if "exp_reward" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN exp_reward INTEGER NOT NULL DEFAULT 0")


def _ensure_item_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(items)")]
    if "country_id" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN country_id INTEGER")
    # All four are nullable with no default: every ordinary (purchasable) item
    # keeps them NULL, so an existing items table is migrated without touching
    # a single existing row's meaning.
    if "hidden_set_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN hidden_set_key TEXT")
    if "hidden_set_name" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN hidden_set_name TEXT")
    if "special_effect_key" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN special_effect_key TEXT")
    if "special_effect_percent" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN special_effect_percent INTEGER")


def _ensure_hunting_ground_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(hunting_grounds)")]
    if "is_hidden" not in cols:
        conn.execute("ALTER TABLE hunting_grounds ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0")


def _ensure_tournament_registration_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(tournament_registrations)")]
    if not cols:
        return
    if "snap_independent_damage_percent" not in cols:
        conn.execute(
            "ALTER TABLE tournament_registrations "
            "ADD COLUMN snap_independent_damage_percent INTEGER NOT NULL DEFAULT 0"
        )
    if "snap_avatar_key" not in cols:
        conn.execute(
            "ALTER TABLE tournament_registrations ADD COLUMN snap_avatar_key TEXT NOT NULL DEFAULT 'avatar_01'"
        )
    if "snap_avatar_custom_filename" not in cols:
        conn.execute(
            "ALTER TABLE tournament_registrations ADD COLUMN snap_avatar_custom_filename TEXT"
        )


def _ensure_map_tile_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(map_tiles)")]
    if "mayor_character_id" not in cols:
        conn.execute("ALTER TABLE map_tiles ADD COLUMN mayor_character_id INTEGER")
    if "bandit_hp" not in cols:
        # NULL means "not yet damaged, full HP" -- lazily initialized to the
        # bandit lord's max HP the first time anyone attacks that neutral
        # tile (see _bandit_lord_stats/game_conquer), rather than eagerly
        # seeded for every neutral tile at map-generation time.
        conn.execute("ALTER TABLE map_tiles ADD COLUMN bandit_hp INTEGER")
    if "defense_reduction_percent" not in cols:
        conn.execute(
            "ALTER TABLE map_tiles ADD COLUMN defense_reduction_percent INTEGER NOT NULL DEFAULT 0"
        )


def _upgrade_country_bonuses(conn):
    """One-time retarget of countries seeded with the old flat 1% bonuses to
    the new differentiated values in DEFAULT_COUNTRIES -- skips any country an
    admin has since hand-edited (its bonuses no longer match the legacy set)."""
    rows = conn.execute(
        "SELECT id, name, hp_bonus, mp_bonus, str_bonus, def_bonus, agi_bonus, luk_bonus FROM countries"
    ).fetchall()
    by_name = {c["name"]: c for c in DEFAULT_COUNTRIES}
    for row in rows:
        legacy = LEGACY_DEFAULT_COUNTRY_BONUSES.get(row["name"])
        target = by_name.get(row["name"])
        if legacy is None or target is None:
            continue
        current = (
            row["hp_bonus"], row["mp_bonus"], row["str_bonus"],
            row["def_bonus"], row["agi_bonus"], row["luk_bonus"],
        )
        if current == legacy:
            conn.execute(
                """UPDATE countries SET hp_bonus = ?, mp_bonus = ?, str_bonus = ?,
                       def_bonus = ?, agi_bonus = ?, luk_bonus = ? WHERE id = ?""",
                (
                    target["hp_bonus"], target["mp_bonus"], target["str_bonus"],
                    target["def_bonus"], target["agi_bonus"], target["luk_bonus"], row["id"],
                ),
            )


def _upgrade_monster_elements(conn):
    """One-time backfill of the element column for monsters seeded before it
    existed (rows left with the '' default)."""
    by_name = {m["name"]: m["element"] for m in DEFAULT_MONSTERS}
    for row in conn.execute("SELECT id, name, element FROM monsters"):
        if not row["element"] and row["name"] in by_name:
            conn.execute(
                "UPDATE monsters SET element = ? WHERE id = ?", (by_name[row["name"]], row["id"])
            )


def _insert_item(conn, i, country_ids_by_name):
    """Single INSERT path shared by the empty-table seed and the add-only
    upgrade, so the hidden_set_*/special_effect_* columns can never be filled
    in on one path and silently skipped on the other. Ordinary items simply
    have no such keys in their dict and store NULL."""
    conn.execute(
        """INSERT INTO items
           (shop_type, name, price, stat, stat_bonus, country_id,
            hidden_set_key, hidden_set_name, special_effect_key, special_effect_percent)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            i["shop_type"], i["name"], i["price"], i["stat"], i["stat_bonus"],
            country_ids_by_name.get(i["country_name"]),
            i.get("hidden_set_key"), i.get("hidden_set_name"),
            i.get("special_effect_key"), i.get("special_effect_percent"),
        ),
    )


def _upgrade_items(conn, country_ids_by_name):
    """Add-only: inserts any DEFAULT_ITEMS row (the country equipment sets,
    plus the 30 秘境 legendary pieces) that isn't already present by exact
    name. Unlike the monster roster this never deletes existing rows -- items
    can be sitting in a character's inventory or equipped slot, so removing
    one would dangle a foreign key."""
    existing_names = {row["name"] for row in conn.execute("SELECT name FROM items")}
    for i in DEFAULT_ITEMS:
        if i["name"] in existing_names:
            continue
        _insert_item(conn, i, country_ids_by_name)


def _seed_hidden_grounds(conn):
    """Add-only seed of the two 秘境 hunting_grounds rows. Runs on every
    startup for both a fresh and a long-lived DB (the ordinary grounds' seed
    is guarded by "table is empty", which would never fire again on an
    existing install), and touches nothing that already exists -- an admin
    who renamed 太極秘境 keeps their name."""
    existing_tiers = {row["tier"] for row in conn.execute("SELECT tier FROM hunting_grounds")}
    for g in HIDDEN_HUNTING_GROUNDS:
        if g["tier"] in existing_tiers:
            continue
        conn.execute(
            """INSERT INTO hunting_grounds (tier, name, min_level, max_level, monster_exp, is_hidden)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (g["tier"], g["name"], g["min_level"], g["max_level"], g["monster_exp"], g["is_hidden"]),
        )


def _seed_hidden_monsters(conn):
    """Add-only seed of the one monster each 秘境 holds, matched by exact name
    so it is never duplicated and never overwrites a tuned existing row. Must
    run after _seed_hidden_grounds (it needs their ids) and after
    _rebuild_monster_roster (which wipes and regenerates the ordinary roster)."""
    ground_ids = {
        row["tier"]: row["id"] for row in conn.execute("SELECT id, tier FROM hunting_grounds")
    }
    existing_names = {row["name"] for row in conn.execute("SELECT name FROM monsters")}
    for m in HIDDEN_MONSTERS:
        if m["name"] in existing_names or m["tier"] not in ground_ids:
            continue
        conn.execute(
            """INSERT INTO monsters
               (hunting_ground_id, name, is_boss, is_guardian, level_min, level_max,
                hp, atk, def, agi, currency_reward, exp_reward, element)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ground_ids[m["tier"]], m["name"], m["is_boss"], m["is_guardian"],
                m["level_min"], m["level_max"],
                m["hp"], m["atk"], m["def"], m["agi"],
                m["currency_reward"], m["exp_reward"], m["element"],
            ),
        )


def _rebuild_monster_roster(conn):
    """One-time full replace of the monsters table with the level-bracketed
    roster (2 named monsters per 5-level bracket + 1 守衛怪 + 1 魔王 per tier).
    Runs again if the table predates is_guardian (old flat roster) OR predates
    exp_reward (every generated row has a positive exp_reward, so a stray 0
    means the column was just added and never backfilled). No admin UI ever
    edits monsters directly, so a full wipe+reseed is safe here (unlike the
    legacy-value-check pattern used for country bonuses)."""
    has_guardian = conn.execute(
        "SELECT COUNT(*) AS c FROM monsters WHERE is_guardian = 1"
    ).fetchone()["c"]
    has_unset_exp = conn.execute(
        "SELECT COUNT(*) AS c FROM monsters WHERE exp_reward = 0"
    ).fetchone()["c"]
    if has_guardian and not has_unset_exp:
        return
    # Scoped to the ordinary (non-hidden) grounds: the 秘境 monsters are not
    # part of the generated roster and must survive a regeneration, or a
    # legacy DB would lose them the moment this one-time rebuild fires.
    conn.execute(
        """DELETE FROM monsters WHERE hunting_ground_id IN (
               SELECT id FROM hunting_grounds WHERE is_hidden = 0
           )"""
    )
    ground_ids = {
        row["tier"]: row["id"] for row in conn.execute("SELECT id, tier FROM hunting_grounds")
    }
    for m in DEFAULT_MONSTERS:
        conn.execute(
            """INSERT INTO monsters
               (hunting_ground_id, name, is_boss, is_guardian, level_min, level_max,
                hp, atk, def, agi, currency_reward, exp_reward, element)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ground_ids[m["tier"]], m["name"], m["is_boss"], m["is_guardian"],
                m["level_min"], m["level_max"],
                m["hp"], m["atk"], m["def"], m["agi"], m["currency_reward"], m["exp_reward"], m["element"],
            ),
        )


def _upgrade_hunting_ground_bounds(conn):
    """One-time bump of the ultimate tier's max_level from the old LEVEL_CAP
    (1000) to the new one (200, once the job/rebirth tier system replaced the
    flat exp curve) -- skipped if an admin already customized it."""
    row = conn.execute(
        "SELECT id, max_level FROM hunting_grounds WHERE tier = 'ultimate'"
    ).fetchone()
    if row and row["max_level"] == LEGACY_ULTIMATE_MAX_LEVEL:
        conn.execute("UPDATE hunting_grounds SET max_level = ? WHERE id = ?", (LEVEL_CAP, row["id"]))


def _ensure_game_settings_columns(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(game_settings)")]
    if "sell_back_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN sell_back_percent REAL NOT NULL DEFAULT 75")
    if "boss_encounter_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_encounter_percent REAL NOT NULL DEFAULT 15")
    if "boss_exp_multiplier" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_exp_multiplier REAL NOT NULL DEFAULT 5")
    if "shop_tax_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN shop_tax_percent REAL NOT NULL DEFAULT 5")
    if "heal_cost_per_point" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN heal_cost_per_point REAL NOT NULL DEFAULT 1")
    if "town_defense_level" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN town_defense_level INTEGER NOT NULL DEFAULT 500")
    if "fortress_defense_level" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN fortress_defense_level INTEGER NOT NULL DEFAULT 1000")
    if "exp_growth_novice_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_novice_percent REAL NOT NULL DEFAULT 6.6")
    if "exp_growth_tier2_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_tier2_percent REAL NOT NULL DEFAULT 6.0")
    if "exp_growth_tier3_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_tier3_percent REAL NOT NULL DEFAULT 0.8")
    if "exp_growth_tier4_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN exp_growth_tier4_percent REAL NOT NULL DEFAULT 0.8")
    if "rebirth_stat_bonus_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN rebirth_stat_bonus_percent REAL NOT NULL DEFAULT 15")
    if "guardian_encounter_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN guardian_encounter_percent REAL NOT NULL DEFAULT 2")
    if "boss_room_trigger_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN boss_room_trigger_percent REAL NOT NULL DEFAULT 50")
    if "guardian_exp_multiplier" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN guardian_exp_multiplier REAL NOT NULL DEFAULT 2")
    if "stat_reroll_cost" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN stat_reroll_cost INTEGER NOT NULL DEFAULT 100000")
    if "war_town_weekday" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN war_town_weekday INTEGER NOT NULL DEFAULT 3")
    if "war_town_start_time" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN war_town_start_time TEXT NOT NULL DEFAULT '20:00'")
    if "war_town_end_time" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN war_town_end_time TEXT NOT NULL DEFAULT '21:00'")
    if "war_fortress_weekday" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN war_fortress_weekday INTEGER NOT NULL DEFAULT 5")
    if "war_fortress_start_time" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN war_fortress_start_time TEXT NOT NULL DEFAULT '20:00'")
    if "war_fortress_end_time" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN war_fortress_end_time TEXT NOT NULL DEFAULT '21:30'")
    if "king_weekly_income_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN king_weekly_income_percent INTEGER NOT NULL DEFAULT 5")
    if "official_weekly_income_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN official_weekly_income_percent INTEGER NOT NULL DEFAULT 3")
    if "king_war_defense_bonus_percent" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN king_war_defense_bonus_percent INTEGER NOT NULL DEFAULT 5")
    if "office_challenge_aura_bonus_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN office_challenge_aura_bonus_percent INTEGER NOT NULL DEFAULT 20"
        )
    if "morale_buff_cost" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN morale_buff_cost INTEGER NOT NULL DEFAULT 30000")
    if "morale_buff_bonus_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN morale_buff_bonus_percent INTEGER NOT NULL DEFAULT 5"
        )
    if "siege_attack_cost" not in cols:
        conn.execute("ALTER TABLE game_settings ADD COLUMN siege_attack_cost INTEGER NOT NULL DEFAULT 30000")
    if "siege_attack_reduction_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN siege_attack_reduction_percent INTEGER NOT NULL DEFAULT 15"
        )
    if "siege_attack_reduction_floor_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN siege_attack_reduction_floor_percent "
            "INTEGER NOT NULL DEFAULT 70"
        )
    if "siege_attack_cooldown_seconds" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN siege_attack_cooldown_seconds INTEGER NOT NULL DEFAULT 900"
        )
    if "defense_repair_cost_per_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN defense_repair_cost_per_percent "
            "INTEGER NOT NULL DEFAULT 5000"
        )
    # 天下武道大會 (weekly PvP tournament). The three tournament_* TABLES need
    # no migration function of their own -- init_db() re-runs the whole of
    # schema.sql, whose CREATE TABLE IF NOT EXISTS statements add them to an
    # existing DB (same as how garrisons/trades were introduced). Only these
    # game_settings COLUMNS need explicit ALTERs.
    if "tournament_registration_fee" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN tournament_registration_fee INTEGER NOT NULL DEFAULT 5000"
        )
    if "tournament_treasury_cut_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN tournament_treasury_cut_percent INTEGER NOT NULL DEFAULT 10"
        )
    if "tournament_registration_deadline_weekday" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN tournament_registration_deadline_weekday "
            "INTEGER NOT NULL DEFAULT 6"
        )
    if "tournament_registration_deadline_time" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN tournament_registration_deadline_time "
            "TEXT NOT NULL DEFAULT '20:00'"
        )
    if "tournament_start_weekday" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN tournament_start_weekday INTEGER NOT NULL DEFAULT 7"
        )
    if "tournament_start_time" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN tournament_start_time TEXT NOT NULL DEFAULT '14:00'"
        )
    # 秘境 interrupt/drop chances -- percentages, same convention as
    # guardian_encounter_percent (0.05 = 1/2000 hunts, 0.02 = 1/5000).
    if "hidden_taiji_trigger_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN hidden_taiji_trigger_percent REAL NOT NULL DEFAULT 0.05"
        )
    if "hidden_wuji_trigger_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN hidden_wuji_trigger_percent REAL NOT NULL DEFAULT 0.02"
        )
    if "hidden_taiji_drop_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN hidden_taiji_drop_percent REAL NOT NULL DEFAULT 50"
        )
    if "hidden_wuji_drop_percent" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN hidden_wuji_drop_percent REAL NOT NULL DEFAULT 30"
        )
    if "avatar_change_base_cost" not in cols:
        conn.execute(
            "ALTER TABLE game_settings ADD COLUMN avatar_change_base_cost INTEGER NOT NULL DEFAULT 5000"
        )


def init_db():
    conn = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    _ensure_is_admin_column(conn)
    _ensure_session_columns(conn)
    _ensure_user_avatar_columns(conn)
    _ensure_character_columns(conn)
    _ensure_country_columns(conn)
    _ensure_monster_columns(conn)
    _ensure_item_columns(conn)
    _ensure_hunting_ground_columns(conn)
    _ensure_map_tile_columns(conn)
    _ensure_game_settings_columns(conn)
    _ensure_tournament_registration_columns(conn)
    conn.commit()
    conn.close()


def log_activity(conn, user_id, username, action, detail="", ip_address=None):
    conn.execute(
        """INSERT INTO activity_log (user_id, username, action, detail, ip_address)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, username, action, detail, ip_address),
    )


def seed_defaults():
    conn = get_db()

    if conn.execute("SELECT COUNT(*) AS c FROM countries").fetchone()["c"] == 0:
        for c in DEFAULT_COUNTRIES:
            conn.execute(
                """INSERT INTO countries
                   (name, element, description, hp_bonus, mp_bonus, str_bonus, def_bonus, agi_bonus, luk_bonus)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    c["name"], c["element"], c["description"],
                    c["hp_bonus"], c["mp_bonus"], c["str_bonus"],
                    c["def_bonus"], c["agi_bonus"], c["luk_bonus"],
                ),
            )
    else:
        _upgrade_country_bonuses(conn)

    admin = conn.execute(
        "SELECT id FROM users WHERE username = ?", (DEFAULT_ADMIN_USERNAME,)
    ).fetchone()
    if admin is None:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
            (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
        )

    map_regenerated = _seed_map_tiles(conn)
    if map_regenerated:
        conn.execute("UPDATE characters SET current_tile_id = NULL")
    _backfill_character_positions(conn)

    if conn.execute("SELECT COUNT(*) AS c FROM game_settings").fetchone()["c"] == 0:
        conn.execute("INSERT INTO game_settings (id) VALUES (1)")

    if conn.execute("SELECT COUNT(*) AS c FROM site_visits").fetchone()["c"] == 0:
        conn.execute("INSERT INTO site_visits (id, total_views) VALUES (1, 0)")

    if conn.execute("SELECT COUNT(*) AS c FROM hunting_grounds").fetchone()["c"] == 0:
        for g in DEFAULT_HUNTING_GROUNDS:
            conn.execute(
                """INSERT INTO hunting_grounds (tier, name, min_level, max_level, monster_exp)
                   VALUES (?, ?, ?, ?, ?)""",
                (g["tier"], g["name"], g["min_level"], g["max_level"], g["monster_exp"]),
            )
    else:
        _upgrade_hunting_ground_bounds(conn)
    _seed_hidden_grounds(conn)

    # is_npc = 0 guard: NPC officeholders (see _seed_npc_officials in app.py)
    # deliberately seed the King seat above LEVEL_CAP (level 220, a legendary
    # exception to the normal player cap) -- without this guard, this clamp
    # would silently knock it back down to LEVEL_CAP on every subsequent app
    # restart, since seed_defaults() runs on every startup and this query
    # runs before _seed_npc_officials sees the row (which only inserts once).
    conn.execute(
        "UPDATE characters SET level = ?, exp = 0 WHERE level > ? AND is_npc = 0", (LEVEL_CAP, LEVEL_CAP)
    )

    country_ids_by_name = {
        row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM countries")
    }
    if conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"] == 0:
        for i in DEFAULT_ITEMS:
            _insert_item(conn, i, country_ids_by_name)
    else:
        _upgrade_items(conn, country_ids_by_name)

    if conn.execute("SELECT COUNT(*) AS c FROM monsters").fetchone()["c"] == 0:
        ground_ids = {
            row["tier"]: row["id"]
            for row in conn.execute("SELECT id, tier FROM hunting_grounds")
        }
        for m in DEFAULT_MONSTERS:
            conn.execute(
                """INSERT INTO monsters
                   (hunting_ground_id, name, is_boss, is_guardian, level_min, level_max,
                    hp, atk, def, agi, currency_reward, exp_reward, element)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ground_ids[m["tier"]], m["name"], m["is_boss"], m["is_guardian"],
                    m["level_min"], m["level_max"],
                    m["hp"], m["atk"], m["def"], m["agi"], m["currency_reward"], m["exp_reward"], m["element"],
                ),
            )
    else:
        _upgrade_monster_elements(conn)
        _rebuild_monster_roster(conn)
    _seed_hidden_monsters(conn)

    conn.commit()
    conn.close()


def _seed_map_tiles(conn):
    layout = generate_layout()
    country_ids = [
        row["id"] for row in conn.execute("SELECT id FROM countries ORDER BY id")
    ]

    desired = sorted(
        (
            t["q"], t["r"], t["tile_type"], t["name"],
            country_ids[t["country_index"]] if t["country_index"] is not None else None,
        )
        for t in layout
    )
    current = sorted(
        (row["q"], row["r"], row["tile_type"], row["name"], row["country_id"])
        for row in conn.execute("SELECT q, r, tile_type, name, country_id FROM map_tiles")
    )
    if desired == current:
        return False

    conn.execute("DELETE FROM map_tiles")

    for tile in layout:
        country_id = (
            country_ids[tile["country_index"]] if tile["country_index"] is not None else None
        )
        conn.execute(
            "INSERT INTO map_tiles (q, r, tile_type, name, country_id) VALUES (?, ?, ?, ?, ?)",
            (tile["q"], tile["r"], tile["tile_type"], tile["name"], country_id),
        )
    return True


def _backfill_character_positions(conn):
    conn.execute(
        """UPDATE characters
           SET current_tile_id = (
               SELECT map_tiles.id FROM map_tiles
               WHERE map_tiles.country_id = characters.country_id
                 AND map_tiles.tile_type = 'fortress'
               LIMIT 1
           )
           WHERE current_tile_id IS NULL"""
    )
