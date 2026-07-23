"""Hex-grid geometry and the world map layout for 諸神的黃昏.

Countries are seeded in this fixed order (matches db.DEFAULT_COUNTRIES):
金 百鍊流金國, 木 翡翠靈木國, 水 蔚藍千泉國, 火 紅蓮業火國, 土 萬物母育國.
Each country gets 1 fortress + 2 preset towns; the rest of the map is
filled with 20 unclaimed neutral towns.
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

NEUTRAL_NAMES = [
    f"廢墟{n}" for n in [
        "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
        "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    ]
]


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


def _axial_distance(a, b):
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
        (c for c in cells if _axial_distance(origin, c) == RADIUS),
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
            key=lambda c: (_axial_distance(fort_cell, c), _angle_of(c)),
        )
        chosen_towns = candidates[:2]
        assigned.update(chosen_towns)

        for name, c in zip(town_names, chosen_towns):
            tiles.append({
                "q": c[0], "r": c[1],
                "tile_type": "town", "name": name, "country_index": idx,
            })

    remaining = [c for c in cells if c not in assigned]
    remaining.sort(key=lambda c: (_axial_distance(origin, c), _angle_of(c)))

    for name, c in zip(NEUTRAL_NAMES, remaining[:20]):
        tiles.append({
            "q": c[0], "r": c[1],
            "tile_type": "neutral", "name": name, "country_index": None,
        })

    return tiles
