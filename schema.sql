CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    last_seen_at TEXT,
    is_online INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip_address TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS countries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    element TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    hp_bonus INTEGER NOT NULL DEFAULT 0,
    mp_bonus INTEGER NOT NULL DEFAULT 0,
    str_bonus INTEGER NOT NULL DEFAULT 0,
    def_bonus INTEGER NOT NULL DEFAULT 0,
    agi_bonus INTEGER NOT NULL DEFAULT 0,
    luk_bonus INTEGER NOT NULL DEFAULT 0,
    treasury INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER UNIQUE NOT NULL,
    country_id INTEGER NOT NULL,
    current_tile_id INTEGER,
    currency INTEGER NOT NULL DEFAULT 1000,
    level INTEGER NOT NULL DEFAULT 1,
    exp INTEGER NOT NULL DEFAULT 0,
    next_action_at TEXT,
    equipped_weapon_id INTEGER,
    equipped_armor_id INTEGER,
    equipped_accessory_id INTEGER,
    battles_count INTEGER NOT NULL DEFAULT 0,
    wins_count INTEGER NOT NULL DEFAULT 0,
    bank_balance INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (country_id) REFERENCES countries(id),
    FOREIGN KEY (current_tile_id) REFERENCES map_tiles(id),
    FOREIGN KEY (equipped_weapon_id) REFERENCES items(id),
    FOREIGN KEY (equipped_armor_id) REFERENCES items(id),
    FOREIGN KEY (equipped_accessory_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS map_tiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    q INTEGER NOT NULL,
    r INTEGER NOT NULL,
    tile_type TEXT NOT NULL,
    name TEXT NOT NULL,
    country_id INTEGER,
    FOREIGN KEY (country_id) REFERENCES countries(id)
);

CREATE TABLE IF NOT EXISTS game_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    turn_wait_seconds INTEGER NOT NULL DEFAULT 2,
    exp_base INTEGER NOT NULL DEFAULT 100,
    exp_growth_percent REAL NOT NULL DEFAULT 0.5,
    sell_back_percent REAL NOT NULL DEFAULT 75,
    shop_tax_percent REAL NOT NULL DEFAULT 5,
    heal_cost_per_point REAL NOT NULL DEFAULT 1,
    town_defense_level INTEGER NOT NULL DEFAULT 500,
    fortress_defense_level INTEGER NOT NULL DEFAULT 1000
);

CREATE TABLE IF NOT EXISTS hunting_grounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tier TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    min_level INTEGER NOT NULL,
    max_level INTEGER NOT NULL,
    monster_exp INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_type TEXT NOT NULL,
    name TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 0,
    stat TEXT NOT NULL,
    stat_bonus INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    UNIQUE(character_id, item_id),
    FOREIGN KEY (character_id) REFERENCES characters(id),
    FOREIGN KEY (item_id) REFERENCES items(id)
);

CREATE TABLE IF NOT EXISTS monsters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hunting_ground_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    is_boss INTEGER NOT NULL DEFAULT 0,
    hp INTEGER NOT NULL,
    atk INTEGER NOT NULL,
    def INTEGER NOT NULL,
    agi INTEGER NOT NULL,
    currency_reward INTEGER NOT NULL DEFAULT 0,
    element TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (hunting_ground_id) REFERENCES hunting_grounds(id)
);
