import random

from game_data.jobs import TIER2_JOBS, TIER3_JOBS, TIER4_JOBS
from game_data.equipment import STAT_ELEMENT

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

# All 200 TIER4_JOBS' 1st skill (currency-learnable, see TIER_SLOT_TUNING
# (4, 1)). Each name is [job's own 2-char epithet][element-appropriate 2-3
# char action word] -- e.g. 業火尊者's "業火" + "焚天" -- generated from a
# per-element action-word pool (see the project's tier4 job/skill design
# notes) so every one of the 600 tier4 skill names across all 3 slots is
# unique both within tier4 and against every novice/二轉/三轉 skill name,
# never a placeholder. The 5 pre-existing jobs keep their original names
# verbatim (業火焚天/青木不朽/流水無痕劍/流金運轉劫/厚土鎮世).
TIER4_SKILL_NAMES = {
    "流金尊者": "流金運轉劫", "鑄金尊者": "鑄金轉運劫",
    "煉金神尊": "煉金運轉劫", "赤金聖皇": "赤金轉運劫",
    "紫金尊神": "紫金問天劫", "玄金劍神": "玄金逆命劫",
    "寒金神皇": "寒金轉運劫", "瑞金聖王": "瑞金算命劫",
    "祥金帝君": "祥金演命劫", "金烽尊主": "金烽測運劫",
    "金曜靈皇": "金曜逆命劫", "金輝道皇": "金輝演命劫",
    "金曦戰尊": "金曦逆命劫", "金霜道君": "金霜推演劫",
    "寰宇尊者": "寰宇寧世", "蒼木尊者": "蒼木堅守",
    "乾坤神尊": "乾坤撫世", "古木神尊": "古木不倒",
    "金瀾真尊": "金瀾推演劫", "清流尊者": "清流破浪劍",
    "金潮天尊": "金潮轉運劫", "鎏金聖君": "鎏金測運劫",
    "寒流神尊": "寒流流影劍", "激流聖皇": "激流踏波劍",
    "鎔金御皇": "鎔金算命劫", "鑠金法君": "鑠金推演劫",
    "金羽尊仙": "金羽運轉劫", "金翎道尊": "金翎定數劫",
    "紫微法皇": "紫微定數劫", "天地聖皇": "天地載世",
    "湍流尊神": "湍流逐風劍", "飛瀑劍神": "飛瀑無痕劍",
    "星命天君": "星命問天劫", "命盤尊帝": "命盤轉運劫",
    "卦靈劍聖": "卦靈卜運劫", "玄卦仙尊": "玄卦問天劫",
    "四方尊神": "四方撫世", "八荒劍神": "八荒安世",
    "中央神皇": "中央護世", "黃庭聖王": "黃庭鎮世",
    "坤靈帝君": "坤靈鎮世", "后土尊主": "后土安世",
    "社稷靈皇": "社稷載世", "山河道皇": "山河定世",
    "九州戰尊": "九州安世", "老藤聖皇": "老藤不折",
    "深林尊神": "深林不倒", "幽林劍神": "幽林堅守",
    "密林神皇": "密林常固", "森羅聖王": "森羅不移",
    "林嵐帝君": "林嵐不移", "松柏尊主": "松柏永昌",
    "古柏靈皇": "古柏永固", "蒼松道皇": "蒼松不倒",
    "翠柏戰尊": "翠柏永固", "竹影道君": "竹影不倒",
    "寒竹真尊": "寒竹不移", "墨竹天尊": "墨竹不移",
    "楠木聖君": "楠木不倒", "古楠御皇": "古楠常青",
    "樟木法君": "樟木不折", "易卦王尊": "易卦逆命劫",
    "懸瀑神皇": "懸瀑馭風劍", "問卦聖尊": "問卦算命劫",
    "推演宗主": "推演卜運劫", "推命天皇": "推命逆命劫",
    "寰中道君": "寰中應世", "環宇真尊": "環宇護世",
    "演卦劍皇": "演卦演命劫", "卦數武尊": "卦數演命劫",
    "天數法王": "天數逆命劫", "氣數尊皇": "氣數算命劫",
    "劫數御尊": "劫數算命劫", "命數玄尊": "命數轉運劫",
    "運數尊聖": "運數轉運劫", "造化尊王": "造化演命劫",
    "碧波聖王": "碧波凌波劍", "滄波帝君": "滄波無痕劍",
    "滄浪尊主": "滄浪逐風劍", "煙波靈皇": "煙波凌波劍",
    "天造尊師": "天造逆命劫", "化機劍尊": "化機逆命劫",
    "機緣靈尊": "機緣卜運劫", "太一天尊": "太一承世",
    "無極聖君": "無極衡世", "老榕尊仙": "老榕堅守",
    "槐蔭道尊": "槐蔭永固", "古槐法皇": "古槐不倒",
    "垂柳天君": "垂柳常固", "渾元御皇": "渾元載世",
    "混元法君": "混元承世", "太極尊仙": "太極護世",
    "寒柳尊帝": "寒柳不移", "楓林劍聖": "楓林常青",
    "丹楓仙尊": "丹楓堅守", "梧桐王尊": "梧桐不折",
    "古桐聖尊": "古桐不朽", "荊棘宗主": "荊棘不朽",
    "藤蔓天皇": "藤蔓常固", "古藤劍皇": "古藤常青",
    "青苔武尊": "青苔不朽", "蒼崖法王": "蒼崖不朽",
    "雲杉尊皇": "雲杉常固", "巨木御尊": "巨木堅守",
    "神木玄尊": "神木堅守", "虯木尊聖": "虯木常固",
    "虯藤尊王": "虯藤永固", "蒼藤尊師": "蒼藤不摧",
    "墨林劍尊": "墨林不折", "幽篁靈尊": "幽篁不朽",
    "青木道尊": "青木不朽", "雲水道皇": "雲水追影劍",
    "道樞道尊": "道樞護世", "樞極法皇": "樞極護世",
    "流水劍尊": "流水無痕劍", "緣數帝尊": "緣數推演劫",
    "福緣法尊": "福緣演命劫", "流雲戰尊": "流雲凌波劍",
    "迅雷道君": "迅雷追影劍", "驚雷真尊": "驚雷凌波劍",
    "閃電天尊": "閃電逐風劍", "祿星靈君": "祿星演命劫",
    "瑞星仙君": "瑞星推演劫", "吉星尊者": "吉星演命劫",
    "厚土真尊": "厚土鎮世", "中樞天君": "中樞應世",
    "寰極尊帝": "寰極護世", "瞬影聖君": "瞬影馭風劍",
    "流光御皇": "流光凌浪劍", "逐浪法君": "逐浪無痕劍",
    "萬象劍聖": "萬象護世", "元始仙尊": "元始鎮世",
    "洪荒王尊": "洪荒承世", "寒篁帝尊": "寒篁不移",
    "篁影法尊": "篁影不倒", "竹徑靈君": "竹徑不摧",
    "磐石仙君": "磐石不折", "巨巖尊者": "巨巖常青",
    "磐礎神尊": "磐礎堅守", "厚壁聖皇": "厚壁常固",
    "混沌聖尊": "混沌應世", "雄關尊神": "雄關不朽",
    "壁壘劍神": "壁壘不折", "城壘神皇": "城壘常青",
    "鎮嶽聖王": "鎮嶽常固", "岳鎮帝君": "岳鎮不移",
    "盤石尊主": "盤石常固", "砥柱靈皇": "砥柱不摧",
    "中流道皇": "中流常青", "定海戰尊": "定海常固",
    "鎮海道君": "鎮海永昌", "靖嶽真尊": "靖嶽永昌",
    "寧嶽天尊": "寧嶽不倒", "安嶽聖君": "安嶽不朽",
    "固嶽御皇": "固嶽常固", "穩岳法君": "穩岳堅守",
    "巍峨尊仙": "巍峨不移", "巍嶽道尊": "巍嶽不朽",
    "太初宗主": "太初寧世", "元和天皇": "元和寧世",
    "沖和劍皇": "沖和定世", "中和武尊": "中和寧世",
    "圓融法王": "圓融定世", "圓通尊皇": "圓通安世",
    "炎陽尊者": "炎陽燎空", "烈焰神尊": "烈焰焚空",
    "丹霞聖皇": "丹霞燎原", "赤霄尊神": "赤霄炙天",
    "烽火劍神": "烽火焚野", "踏浪尊仙": "踏浪踏波劍",
    "周天御尊": "周天護世", "大千玄尊": "大千衡世",
    "寰界尊聖": "寰界承世", "四海尊王": "四海衡世",
    "五嶽尊師": "五嶽護世", "六合劍尊": "六合寧世",
    "八方靈尊": "八方承世", "雄峰法皇": "雄峰堅守",
    "絕壁天君": "絕壁不摧", "懸崖尊帝": "懸崖不朽",
    "千仞劍聖": "千仞不折", "萬仞仙尊": "萬仞不摧",
    "崇嶽王尊": "崇嶽常青", "泰嶽聖尊": "泰嶽不折",
    "華嶽宗主": "華嶽常青", "衡嶽天皇": "衡嶽不朽",
    "炙陽神皇": "炙陽焚天", "熔岩聖王": "熔岩灼天",
    "九野帝尊": "九野寧世", "朱雀帝君": "朱雀燎空",
    "焰嵐尊主": "焰嵐焚野", "灼日靈皇": "灼日燎原",
    "烈日道皇": "烈日灼天", "焱海戰尊": "焱海焚天",
    "炎獄道君": "炎獄灼世", "十方法尊": "十方承世",
    "萬方靈君": "萬方寧世", "萬靈仙君": "萬靈承世",
    "業火尊者": "業火焚天", "焚淵真尊": "焚淵炙世",
}

# 四轉's 2nd skill slot: NOT learnable with currency at all (see
# TIER_SLOT_TUNING[(4, 2)]'s "requires_skill_book" marker and
# _learnable_skills, which hardcodes tier4 to only ever offer slot 1). The
# only way in is a monster-dropped skill book, redeemed via
# /character/skill_book/use once the character is actually 四轉. With 200
# jobs now (was 5), TIER4_SLOT2_SKILL_KEYS below grows to 200 keys too --
# a won ultimate-hunting-ground roll picks uniformly among all 200 possible
# books now, not just 5, since that list is derived from this dict rather
# than hand-maintained separately.
TIER4_SKILL_NAMES_SLOT2 = {
    "流金尊者": "流金逆天劫", "鑄金尊者": "鑄金斷命劫",
    "煉金神尊": "煉金逆天劫", "赤金聖皇": "赤金斷命劫",
    "紫金尊神": "紫金改命劫", "玄金劍神": "玄金奪命劫",
    "寒金神皇": "寒金斷命劫", "瑞金聖王": "瑞金逆運劫",
    "祥金帝君": "祥金劫運劫", "金烽尊主": "金烽亂命劫",
    "金曜靈皇": "金曜奪命劫", "金輝道皇": "金輝劫運劫",
    "金曦戰尊": "金曦奪命劫", "金霜道君": "金霜移數劫",
    "寰宇尊者": "寰宇包宇", "蒼木尊者": "蒼木庇世",
    "乾坤神尊": "乾坤括宇", "古木神尊": "古木鎮野",
    "金瀾真尊": "金瀾移數劫", "清流尊者": "清流裂波劍",
    "金潮天尊": "金潮斷命劫", "鎏金聖君": "鎏金亂命劫",
    "寒流神尊": "寒流貫日劍", "激流聖皇": "激流斬浪劍",
    "鎔金御皇": "鎔金逆運劫", "鑠金法君": "鑠金移數劫",
    "金羽尊仙": "金羽逆天劫", "金翎道尊": "金翎換命劫",
    "紫微法皇": "紫微換命劫", "天地聖皇": "天地括地",
    "湍流尊神": "湍流驚濤劍", "飛瀑劍神": "飛瀑穿石劍",
    "星命天君": "星命改命劫", "命盤尊帝": "命盤斷命劫",
    "卦靈劍聖": "卦靈破數劫", "玄卦仙尊": "玄卦改命劫",
    "四方尊神": "四方括宇", "八荒劍神": "八荒括天",
    "中央神皇": "中央覆天", "黃庭聖王": "黃庭封天",
    "坤靈帝君": "坤靈封天", "后土尊主": "后土括天",
    "社稷靈皇": "社稷括地", "山河道皇": "山河包天",
    "九州戰尊": "九州括天", "老藤聖皇": "老藤衛土",
    "深林尊神": "深林鎮野", "幽林劍神": "幽林庇世",
    "密林神皇": "密林護世", "森羅聖王": "森羅障天",
    "林嵐帝君": "林嵐障天", "松柏尊主": "松柏封域",
    "古柏靈皇": "古柏遮天", "蒼松道皇": "蒼松鎮野",
    "翠柏戰尊": "翠柏遮天", "竹影道君": "竹影鎮野",
    "寒竹真尊": "寒竹障天", "墨竹天尊": "墨竹障天",
    "楠木聖君": "楠木鎮野", "古楠御皇": "古楠覆地",
    "樟木法君": "樟木衛土", "易卦王尊": "易卦奪命劫",
    "懸瀑神皇": "懸瀑碎影劍", "問卦聖尊": "問卦逆運劫",
    "推演宗主": "推演破數劫", "推命天皇": "推命奪命劫",
    "寰中道君": "寰中承宇", "環宇真尊": "環宇覆天",
    "演卦劍皇": "演卦劫運劫", "卦數武尊": "卦數劫運劫",
    "天數法王": "天數奪命劫", "氣數尊皇": "氣數逆運劫",
    "劫數御尊": "劫數逆運劫", "命數玄尊": "命數斷命劫",
    "運數尊聖": "運數斷命劫", "造化尊王": "造化劫運劫",
    "碧波聖王": "碧波碎浪劍", "滄波帝君": "滄波穿石劍",
    "滄浪尊主": "滄浪驚濤劍", "煙波靈皇": "煙波碎浪劍",
    "天造尊師": "天造奪命劫", "化機劍尊": "化機奪命劫",
    "機緣靈尊": "機緣破數劫", "太一天尊": "太一承天",
    "無極聖君": "無極涵宇", "老榕尊仙": "老榕庇世",
    "槐蔭道尊": "槐蔭遮天", "古槐法皇": "古槐鎮野",
    "垂柳天君": "垂柳護世", "渾元御皇": "渾元括地",
    "混元法君": "混元承天", "太極尊仙": "太極覆天",
    "寒柳尊帝": "寒柳障天", "楓林劍聖": "楓林覆地",
    "丹楓仙尊": "丹楓庇世", "梧桐王尊": "梧桐衛土",
    "古桐聖尊": "古桐蔽天", "荊棘宗主": "荊棘蔽天",
    "藤蔓天皇": "藤蔓護世", "古藤劍皇": "古藤覆地",
    "青苔武尊": "青苔蔽天", "蒼崖法王": "蒼崖蔽天",
    "雲杉尊皇": "雲杉護世", "巨木御尊": "巨木庇世",
    "神木玄尊": "神木庇世", "虯木尊聖": "虯木護世",
    "虯藤尊王": "虯藤遮天", "蒼藤尊師": "蒼藤屏山",
    "墨林劍尊": "墨林衛土", "幽篁靈尊": "幽篁蔽天",
    "青木道尊": "青木蔽天", "雲水道皇": "雲水破空劍",
    "道樞道尊": "道樞覆天", "樞極法皇": "樞極覆天",
    "流水劍尊": "流水穿石劍", "緣數帝尊": "緣數移數劫",
    "福緣法尊": "福緣劫運劫", "流雲戰尊": "流雲碎浪劍",
    "迅雷道君": "迅雷破空劍", "驚雷真尊": "驚雷碎浪劍",
    "閃電天尊": "閃電驚濤劍", "祿星靈君": "祿星劫運劫",
    "瑞星仙君": "瑞星移數劫", "吉星尊者": "吉星劫運劫",
    "厚土真尊": "厚土封天", "中樞天君": "中樞承宇",
    "寰極尊帝": "寰極覆天", "瞬影聖君": "瞬影碎影劍",
    "流光御皇": "流光裂風劍", "逐浪法君": "逐浪穿石劍",
    "萬象劍聖": "萬象覆天", "元始仙尊": "元始封天",
    "洪荒王尊": "洪荒承天", "寒篁帝尊": "寒篁障天",
    "篁影法尊": "篁影鎮野", "竹徑靈君": "竹徑屏山",
    "磐石仙君": "磐石衛土", "巨巖尊者": "巨巖覆地",
    "磐礎神尊": "磐礎庇世", "厚壁聖皇": "厚壁護世",
    "混沌聖尊": "混沌承宇", "雄關尊神": "雄關蔽天",
    "壁壘劍神": "壁壘衛土", "城壘神皇": "城壘覆地",
    "鎮嶽聖王": "鎮嶽護世", "岳鎮帝君": "岳鎮障天",
    "盤石尊主": "盤石護世", "砥柱靈皇": "砥柱屏山",
    "中流道皇": "中流覆地", "定海戰尊": "定海護世",
    "鎮海道君": "鎮海封域", "靖嶽真尊": "靖嶽封域",
    "寧嶽天尊": "寧嶽鎮野", "安嶽聖君": "安嶽蔽天",
    "固嶽御皇": "固嶽護世", "穩岳法君": "穩岳庇世",
    "巍峨尊仙": "巍峨障天", "巍嶽道尊": "巍嶽蔽天",
    "太初宗主": "太初包宇", "元和天皇": "元和包宇",
    "沖和劍皇": "沖和包天", "中和武尊": "中和包宇",
    "圓融法王": "圓融包天", "圓通尊皇": "圓通括天",
    "炎陽尊者": "炎陽熾世", "烈焰神尊": "烈焰燎穹",
    "丹霞聖皇": "丹霞熾天", "赤霄尊神": "赤霄灼海",
    "烽火劍神": "烽火赫天", "踏浪尊仙": "踏浪斬浪劍",
    "周天御尊": "周天覆天", "大千玄尊": "大千涵宇",
    "寰界尊聖": "寰界承天", "四海尊王": "四海涵宇",
    "五嶽尊師": "五嶽覆天", "六合劍尊": "六合包宇",
    "八方靈尊": "八方承天", "雄峰法皇": "雄峰庇世",
    "絕壁天君": "絕壁屏山", "懸崖尊帝": "懸崖蔽天",
    "千仞劍聖": "千仞衛土", "萬仞仙尊": "萬仞屏山",
    "崇嶽王尊": "崇嶽覆地", "泰嶽聖尊": "泰嶽衛土",
    "華嶽宗主": "華嶽覆地", "衡嶽天皇": "衡嶽蔽天",
    "炙陽神皇": "炙陽燃野", "熔岩聖王": "熔岩炙淵",
    "九野帝尊": "九野包宇", "朱雀帝君": "朱雀熾世",
    "焰嵐尊主": "焰嵐赫天", "灼日靈皇": "灼日熾天",
    "烈日道皇": "烈日炙淵", "焱海戰尊": "焱海燃野",
    "炎獄道君": "炎獄焚岳", "十方法尊": "十方承天",
    "萬方靈君": "萬方包宇", "萬靈仙君": "萬靈承天",
    "業火尊者": "業火燎原", "焚淵真尊": "焚淵煉獄",
}

# 四轉's 3rd skill slot: the King's exclusive skill, per the seat-seeding
# feature (see _seed_npc_officials in app.py). It is NOT reachable through
# either normal unlock path -- not the currency-learn ladder (_learnable_skills
# is hardcoded to only ever offer tier4 slot 1) nor the skill-book path
# (TIER4_SLOT2_SKILL_KEYS stays hardcoded to slot 2 only). Slot 3 only ever
# exists because it's seeded directly onto an NPC king character's row --
# for the 195 new jobs (no NPC king), this slot's skill simply stays
# permanently unreachable by design, exactly like the 5 legacy jobs' own
# slot-3 skills always have been. Its tuning (TIER_SLOT_TUNING[(4, 3)]) is
# deliberately NOT part of the monotonic slot-1/slot-2 learn ladder --
# trigger_chance (45%) sits well above the ladder's 25% floor, because per
# the user's explicit spec this is a standalone "中機率、高傷害、中耗MP"
# (mid chance / high damage / mid MP cost) skill, not another progression
# step.
TIER4_SKILL_NAMES_SLOT3 = {
    "流金尊者": "流金鎮魂劫", "鑄金尊者": "鑄金斷魂劫",
    "煉金神尊": "煉金鎮魂劫", "赤金聖皇": "赤金斷魂劫",
    "紫金尊神": "紫金誅命劫", "玄金劍神": "玄金滅命劫",
    "寒金神皇": "寒金斷魂劫", "瑞金聖王": "瑞金殞命劫",
    "祥金帝君": "祥金滅運劫", "金烽尊主": "金烽封魂劫",
    "金曜靈皇": "金曜滅命劫", "金輝道皇": "金輝滅運劫",
    "金曦戰尊": "金曦滅命劫", "金霜道君": "金霜絕命劫",
    "寰宇尊者": "寰宇鎮四海", "蒼木尊者": "蒼木撼地",
    "乾坤神尊": "乾坤震乾坤", "古木神尊": "古木定山",
    "金瀾真尊": "金瀾絕命劫", "清流尊者": "清流破天劍",
    "金潮天尊": "金潮斷魂劫", "鎏金聖君": "鎏金封魂劫",
    "寒流神尊": "寒流屠浪劍", "激流聖皇": "激流滅波劍",
    "鎔金御皇": "鎔金殞命劫", "鑠金法君": "鑠金絕命劫",
    "金羽尊仙": "金羽鎮魂劫", "金翎道尊": "金翎誅運劫",
    "紫微法皇": "紫微誅運劫", "天地聖皇": "天地封萬象",
    "湍流尊神": "湍流斷天劍", "飛瀑劍神": "飛瀑裂天劍",
    "星命天君": "星命誅命劫", "命盤尊帝": "命盤斷魂劫",
    "卦靈劍聖": "卦靈封命劫", "玄卦仙尊": "玄卦誅命劫",
    "四方尊神": "四方震乾坤", "八荒劍神": "八荒鎮宇訣",
    "中央神皇": "中央撼萬象", "黃庭聖王": "黃庭震世訣",
    "坤靈帝君": "坤靈震世訣", "后土尊主": "后土鎮宇訣",
    "社稷靈皇": "社稷封萬象", "山河道皇": "山河定乾坤",
    "九州戰尊": "九州鎮宇訣", "老藤聖皇": "老藤壓世",
    "深林尊神": "深林定山", "幽林劍神": "幽林撼地",
    "密林神皇": "密林鎮世", "森羅聖王": "森羅鎮宇",
    "林嵐帝君": "林嵐鎮宇", "松柏尊主": "松柏拔山",
    "古柏靈皇": "古柏撐天", "蒼松道皇": "蒼松定山",
    "翠柏戰尊": "翠柏撐天", "竹影道君": "竹影定山",
    "寒竹真尊": "寒竹鎮宇", "墨竹天尊": "墨竹鎮宇",
    "楠木聖君": "楠木定山", "古楠御皇": "古楠頂天",
    "樟木法君": "樟木壓世", "易卦王尊": "易卦滅命劫",
    "懸瀑神皇": "懸瀑碎天劍", "問卦聖尊": "問卦殞命劫",
    "推演宗主": "推演封命劫", "推命天皇": "推命滅命劫",
    "寰中道君": "寰中定萬象", "環宇真尊": "環宇撼萬象",
    "演卦劍皇": "演卦滅運劫", "卦數武尊": "卦數滅運劫",
    "天數法王": "天數滅命劫", "氣數尊皇": "氣數殞命劫",
    "劫數御尊": "劫數殞命劫", "命數玄尊": "命數斷魂劫",
    "運數尊聖": "運數斷魂劫", "造化尊王": "造化滅運劫",
    "碧波聖王": "碧波滅浪劍", "滄波帝君": "滄波裂天劍",
    "滄浪尊主": "滄浪斷天劍", "煙波靈皇": "煙波滅浪劍",
    "天造尊師": "天造滅命劫", "化機劍尊": "化機滅命劫",
    "機緣靈尊": "機緣封命劫", "太一天尊": "太一鎮萬靈",
    "無極聖君": "無極撫萬方", "老榕尊仙": "老榕撼地",
    "槐蔭道尊": "槐蔭撐天", "古槐法皇": "古槐定山",
    "垂柳天君": "垂柳鎮世", "渾元御皇": "渾元封萬象",
    "混元法君": "混元鎮萬靈", "太極尊仙": "太極撼萬象",
    "寒柳尊帝": "寒柳鎮宇", "楓林劍聖": "楓林頂天",
    "丹楓仙尊": "丹楓撼地", "梧桐王尊": "梧桐壓世",
    "古桐聖尊": "古桐擎天", "荊棘宗主": "荊棘擎天",
    "藤蔓天皇": "藤蔓鎮世", "古藤劍皇": "古藤頂天",
    "青苔武尊": "青苔擎天", "蒼崖法王": "蒼崖擎天",
    "雲杉尊皇": "雲杉鎮世", "巨木御尊": "巨木撼地",
    "神木玄尊": "神木撼地", "虯木尊聖": "虯木鎮世",
    "虯藤尊王": "虯藤撐天", "蒼藤尊師": "蒼藤鎮域",
    "墨林劍尊": "墨林壓世", "幽篁靈尊": "幽篁擎天",
    "青木道尊": "青木擎天", "雲水道皇": "雲水絕影劍",
    "道樞道尊": "道樞撼萬象", "樞極法皇": "樞極撼萬象",
    "流水劍尊": "流水裂天劍", "緣數帝尊": "緣數絕命劫",
    "福緣法尊": "福緣滅運劫", "流雲戰尊": "流雲滅浪劍",
    "迅雷道君": "迅雷絕影劍", "驚雷真尊": "驚雷滅浪劍",
    "閃電天尊": "閃電斷天劍", "祿星靈君": "祿星滅運劫",
    "瑞星仙君": "瑞星絕命劫", "吉星尊者": "吉星滅運劫",
    "厚土真尊": "厚土震世訣", "中樞天君": "中樞定萬象",
    "寰極尊帝": "寰極撼萬象", "瞬影聖君": "瞬影碎天劍",
    "流光御皇": "流光殞浪劍", "逐浪法君": "逐浪裂天劍",
    "萬象劍聖": "萬象撼萬象", "元始仙尊": "元始震世訣",
    "洪荒王尊": "洪荒鎮萬靈", "寒篁帝尊": "寒篁鎮宇",
    "篁影法尊": "篁影定山", "竹徑靈君": "竹徑鎮域",
    "磐石仙君": "磐石壓世", "巨巖尊者": "巨巖頂天",
    "磐礎神尊": "磐礎撼地", "厚壁聖皇": "厚壁鎮世",
    "混沌聖尊": "混沌定萬象", "雄關尊神": "雄關擎天",
    "壁壘劍神": "壁壘壓世", "城壘神皇": "城壘頂天",
    "鎮嶽聖王": "鎮嶽鎮世", "岳鎮帝君": "岳鎮鎮宇",
    "盤石尊主": "盤石鎮世", "砥柱靈皇": "砥柱鎮域",
    "中流道皇": "中流頂天", "定海戰尊": "定海鎮世",
    "鎮海道君": "鎮海拔山", "靖嶽真尊": "靖嶽拔山",
    "寧嶽天尊": "寧嶽定山", "安嶽聖君": "安嶽擎天",
    "固嶽御皇": "固嶽鎮世", "穩岳法君": "穩岳撼地",
    "巍峨尊仙": "巍峨鎮宇", "巍嶽道尊": "巍嶽擎天",
    "太初宗主": "太初鎮四海", "元和天皇": "元和鎮四海",
    "沖和劍皇": "沖和定乾坤", "中和武尊": "中和鎮四海",
    "圓融法王": "圓融定乾坤", "圓通尊皇": "圓通鎮宇訣",
    "炎陽尊者": "炎陽焚天劫", "烈焰神尊": "烈焰劫焰",
    "丹霞聖皇": "丹霞焚滅", "赤霄尊神": "赤霄殞世",
    "烽火劍神": "烽火焚穹滅", "踏浪尊仙": "踏浪滅波劍",
    "周天御尊": "周天撼萬象", "大千玄尊": "大千撫萬方",
    "寰界尊聖": "寰界鎮萬靈", "四海尊王": "四海撫萬方",
    "五嶽尊師": "五嶽撼萬象", "六合劍尊": "六合鎮四海",
    "八方靈尊": "八方鎮萬靈", "雄峰法皇": "雄峰撼地",
    "絕壁天君": "絕壁鎮域", "懸崖尊帝": "懸崖擎天",
    "千仞劍聖": "千仞壓世", "萬仞仙尊": "萬仞鎮域",
    "崇嶽王尊": "崇嶽頂天", "泰嶽聖尊": "泰嶽壓世",
    "華嶽宗主": "華嶽頂天", "衡嶽天皇": "衡嶽擎天",
    "炙陽神皇": "炙陽滅世", "熔岩聖王": "熔岩湮滅",
    "九野帝尊": "九野鎮四海", "朱雀帝君": "朱雀焚天劫",
    "焰嵐尊主": "焰嵐焚穹滅", "灼日靈皇": "灼日焚滅",
    "烈日道皇": "烈日湮滅", "焱海戰尊": "焱海滅世",
    "炎獄道君": "炎獄焦土", "十方法尊": "十方鎮萬靈",
    "萬方靈君": "萬方鎮四海", "萬靈仙君": "萬靈鎮萬靈",
    "業火尊者": "業火滅世", "焚淵真尊": "焚淵毀天",
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
    # King-only exclusive skill (see TIER4_SKILL_NAMES_SLOT3 above) -- never
    # reachable via the normal currency-learn path or the skill-book path,
    # only ever granted by directly seeding it onto an NPC king character.
    # "中機率、高傷害、中耗MP" per spec: trigger_chance intentionally sits
    # above slot 1/2's 25% floor since it isn't a rung on that sequential
    # ladder.
    (4, 3): {
        "mp_cost": 45, "multiplier": 3.8, "trigger_chance": 45, "learn_level": 121,
        "learn_cost": None, "requires_skill_book": False,
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
    for job, info in TIER4_JOBS.items():
        # None (balanced/tie job, e.g. 厚土真尊) uses "avg" same as the 土
        # novice skill -- _skill_damage_stat_value averages all 4 stats for
        # it, and it's deliberately absent from STAT_ELEMENT so it gets no
        # element-colored badge, same as every "avg" skill already does.
        stat = info["dominant_stat"] or "avg"
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
        catalog[_skill_key(job, 3)] = {
            "key": _skill_key(job, 3), "name": TIER4_SKILL_NAMES_SLOT3[job], "stat": stat,
            "job_tier": 4, "slot": 3, "job_class": job,
            **TIER_SLOT_TUNING[(4, 3)],
        }
    return catalog


SKILL_CATALOG = _build_skill_catalog()

# skill name -> element, for coloring skill names in the battle log by the
# element of the signature stat they scale off. Same STAT_ELEMENT mapping
# already used to badge skills on the character sheet (see
# templates/character.html's stat_element.get(skill.stat) calls) -- reused
# here rather than re-derived, so both places agree on which element a skill
# is even if SET_SIGNATURE_STAT ever changes. 土-flavored skills (stat ==
# "avg") intentionally get no entry, same as there.
SKILL_ELEMENT_BY_NAME = {
    skill["name"]: STAT_ELEMENT[skill["stat"]]
    for skill in SKILL_CATALOG.values()
    if skill["stat"] in STAT_ELEMENT
}

# The 200 monster-drop-only 四轉 slot-2 skill keys (one per TIER4_JOBS job),
# used by the ultimate hunting ground's skill-book drop roll to pick which
# book a won hunt hands out -- see the "TIER4_SLOT2_SKILL_KEYS below grows"
# comment on TIER4_SKILL_NAMES_SLOT2 above for why this list's size tracks
# TIER4_JOBS automatically instead of needing its own update.
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
