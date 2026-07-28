MIN_USERNAME_LEN = 3
MIN_PASSWORD_LEN = 6
MIN_CHARACTER_NAME_LEN = 2
MAX_CHARACTER_NAME_LEN = 20
STAT_FIELDS = ["hp_bonus", "mp_bonus", "str_bonus", "def_bonus", "agi_bonus", "luk_bonus"]
IDLE_THRESHOLD_MINUTES = 15
ACTION_LABELS = {
    "register": "註冊",
    "login": "登入",
    "login_failed": "登入失敗",
    "logout": "登出",
    "auto_logout": "閒置自動登出",
    "character_create": "建立角色",
    "hunt": "打怪",
    "move": "移動",
    "shop_buy": "購買裝備",
    "shop_sell": "出售裝備",
    "equip": "裝備",
    "unequip": "卸下裝備",
    "recover": "回復",
    "bank_deposit": "銀行存款",
    "bank_withdraw": "銀行提款",
    "treasury_donate": "捐獻國庫",
    "conquer_win": "攻城成功",
    "conquer_loss": "攻城失敗",
    "promote_tier1": "一轉",
    "promote_tier2": "二轉",
    "promote_tier3": "三轉",
    "promote_tier4": "四轉",
    "rebirth": "轉生",
    "learn_skill": "學習技能",
    "skill_book_drop": "掉落技能書",
    "debug_reset": "（除錯）重置角色",
}

SHOP_TYPE_LABELS = {
    "weapon": "武器店",
    "armor": "防具店",
    "accessory": "飾品店",
}
SLOT_LABELS = {
    "weapon": "武器",
    "armor": "防具",
    "accessory": "飾品",
}
EQUIP_SLOT_COLUMNS = {
    "weapon": "equipped_weapon_id",
    "armor": "equipped_armor_id",
    "accessory": "equipped_accessory_id",
}

# Government seats are informational only for now -- no permissions are tied
# to holding one, just a name + a blurb of what the seat is meant to do.
GOVERNMENT_ROLES = [
    {"column": "king_character_id", "label": "國王", "description": "國家最高元首，代表國家對外決策"},
    {"column": "advisor_character_id", "label": "參謀", "description": "協助國政與外交，提供決策建議"},
    {"column": "general_character_id", "label": "大將軍", "description": "統帥軍隊，主導攻城與防衛行動"},
]

# Below this level, 升級軟糖 may still be used to skip grinding; past it,
# levelling only comes from killing monsters.

def tile_display_name(name, tile_type):
    return f"{name}要塞" if tile_type == "fortress" else name


HEX_SIZE = 42
ELEMENT_COLORS = {
    "金": "#f0c419",
    "木": "#4c8c5c",
    "水": "#3b7dc4",
    "火": "#c0453f",
    "土": "#8b5a2b",
}
NEUTRAL_TILE_COLOR = "#5a5a5a"
MOUNTAIN_TILE_COLOR = "#3e3830"

# Every character starts from the same base stats; country bonuses are a
# percentage applied on top (countries.*_bonus stores the percent, e.g. 1 = 1%).
BASE_STATS = {
    "hp": ("hp_bonus", 500),
    "mp": ("mp_bonus", 500),
    "str": ("str_bonus", 30),
    "def": ("def_bonus", 30),
    "agi": ("agi_bonus", 30),
    "luk": ("luk_bonus", 30),
}

# Flat stat growth per level above 1, so higher-level hunting grounds are
# actually harder/beatable -- gear alone was the only source of growth before.
LEVEL_STAT_GROWTH = {"hp": 5, "mp": 5, "str": 1, "def": 1, "agi": 1, "luk": 1}

STAT_LABELS = {"str": "力量", "def": "防禦", "agi": "敏捷", "luk": "幸運", "avg": "六圍平均"}
