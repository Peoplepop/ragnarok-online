"""Hex-grid geometry and the world map layout for 五行爭鋒.

Countries are seeded in this fixed order (matches db.DEFAULT_COUNTRIES):
金 百鍊流金國, 木 翡翠靈木國, 水 蔚藍千泉國, 火 紅蓮業火國, 土 萬物母育國.
Each country gets 1 fortress + 2 preset towns; every remaining cell in
the hex disk is filled with an unclaimed neutral town, so the map reads
as one complete, gap-free hexagon rather than a scattered cluster.
"""

import math

RADIUS = 3

COUNTRY_TILE_NAMES = [
    ("鎏金城", ["鑄魂坊", "黑鐵鎮"]),   # 金 百鍊流金國
    ("靈木堡", ["翠語林", "藤蔓村"]),   # 木 翡翠靈木國
    ("千泉城", ["霧靄津", "寒潮港"]),   # 水 蔚藍千泉國
    ("業火砦", ["焚天鎮", "烈焰谷"]),   # 火 紅蓮業火國
    ("母育城", ["沃土村", "大地灣"]),   # 土 萬物母育國
]

# Unclaimed cells (37 total minus the 15 country-owned tiles = 22, 3 of which
# get overwritten to mountain by _apply_manual_overrides) get a real place
# name instead of a numbered "廢墟N" placeholder -- one entry per remaining
# cell in generation order, so this list must have at least 22 entries.
NEUTRAL_TOWN_NAMES = [
    "清風鎮", "望江村", "石橋鎮", "竹溪村", "青雲驛", "白露莊", "桃源里", "落霞鄉",
    "幽篁里", "曉月鎮", "靜水村", "孤峰鎮", "雲夢澤", "秋水鎮", "古渡口", "蘆花村",
    "疏影莊", "寒煙渡", "松風嶺", "明月灣", "遠山村", "溪雲鎮",
]


def _neutral_name(i):
    return NEUTRAL_TOWN_NAMES[i - 1]


# Manual overrides layered on top of the procedural layout below, per the
# user's explicit map design calls (not derivable from the generation rules).
# Axial shift directions (dq, dr) as they read on screen, per axial_to_pixel:
#   (0,-1) up, (0,1) down, (-1,0) up-left, (1,0) down-right,
#   (1,-1) up-right, (-1,1) down-left.
_MOUNTAIN_CELLS = {(2, -1), (0, 0), (0, 1)}  # was 幽篁里 / 松風嶺 / 秋水鎮
_TERRITORY_SHIFTS = [
    (4, (-1, 0)),  # 萬物母育國 moves one step 左上 (up-left), onto 蘆花村/古渡口
    (0, (0, 1)),   # 百鍊流金國 moves one step 下 (down), onto 疏影莊/寒煙渡
]


def _shift_country_territory(tiles, country_index, shift):
    dq, dr = shift
    moving = [t for t in tiles if t["country_index"] == country_index]
    old_cells = {(t["q"], t["r"]) for t in moving}
    for t in moving:
        t["q"] += dq
        t["r"] += dr
    new_cells = {(t["q"], t["r"]) for t in moving}

    reclaimed = sorted(new_cells - old_cells)  # cells taken over from former neutral ruins
    vacated = sorted(old_cells - new_cells)  # cells the country left behind

    freed_names = [
        t["name"] for c in reclaimed
        for t in tiles
        if (t["q"], t["r"]) == c and t["country_index"] != country_index
    ]

    tiles[:] = [
        t for t in tiles
        if (t["q"], t["r"]) not in reclaimed or t["country_index"] == country_index
    ]
    for (q, r), name in zip(vacated, freed_names):
        tiles.append({"q": q, "r": r, "tile_type": "neutral", "name": name, "country_index": None})


def _apply_manual_overrides(tiles):
    for tile in tiles:
        if (tile["q"], tile["r"]) in _MOUNTAIN_CELLS:
            tile["tile_type"] = "mountain"
            tile["name"] = "山嶺"
            tile["country_index"] = None

    for country_index, shift in _TERRITORY_SHIFTS:
        _shift_country_territory(tiles, country_index, shift)


def axial_to_pixel(q, r, size):
    x = size * 1.5 * q
    y = size * math.sqrt(3) * (r + q / 2)
    return x, y


def hex_corners(cx, cy, size):
    return [
        (
            cx + size * math.cos(math.radians(60 * i)),
            cy + size * math.sin(math.radians(60 * i)),
        )
        for i in range(6)
    ]


def _hex_area_cells(radius):
    """All axial (q, r) cells within `radius` steps of the origin."""
    cells = []
    for x in range(-radius, radius + 1):
        for z in range(-radius, radius + 1):
            y = -x - z
            if -radius <= y <= radius:
                cells.append((x, z))
    return cells


def axial_distance(a, b):
    ax, az = a
    ay = -ax - az
    bx, bz = b
    by = -bx - bz
    return (abs(ax - bx) + abs(ay - by) + abs(az - bz)) // 2


def _angle_of(cell):
    x, y = axial_to_pixel(cell[0], cell[1], 1)
    return math.atan2(y, x)


def generate_layout():
    """Return a list of tile dicts: q, r, tile_type, name, country_index."""
    cells = _hex_area_cells(RADIUS)
    origin = (0, 0)

    outer_ring = sorted(
        (c for c in cells if axial_distance(origin, c) == RADIUS),
        key=_angle_of,
    )

    n = len(outer_ring)
    fortress_cells = [outer_ring[round(i * n / 5) % n] for i in range(5)]

    assigned = set(fortress_cells)
    tiles = []

    for idx, fort_cell in enumerate(fortress_cells):
        fortress_name, town_names = COUNTRY_TILE_NAMES[idx]
        tiles.append({
            "q": fort_cell[0], "r": fort_cell[1],
            "tile_type": "fortress", "name": fortress_name, "country_index": idx,
        })

        candidates = sorted(
            (c for c in cells if c not in assigned),
            key=lambda c: (axial_distance(fort_cell, c), _angle_of(c)),
        )
        chosen_towns = candidates[:2]
        assigned.update(chosen_towns)

        for name, c in zip(town_names, chosen_towns):
            tiles.append({
                "q": c[0], "r": c[1],
                "tile_type": "town", "name": name, "country_index": idx,
            })

    remaining = [c for c in cells if c not in assigned]
    remaining.sort(
        key=lambda c: (min(axial_distance(c, a) for a in assigned), _angle_of(c))
    )

    for i, c in enumerate(remaining, start=1):
        tiles.append({
            "q": c[0], "r": c[1],
            "tile_type": "neutral", "name": _neutral_name(i), "country_index": None,
        })

    _apply_manual_overrides(tiles)
    return tiles
