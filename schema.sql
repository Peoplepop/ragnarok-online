CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    last_seen_at TEXT,
    is_online INTEGER NOT NULL DEFAULT 0,
    is_npc INTEGER NOT NULL DEFAULT 0,
    -- avatar_key is one of the 20 built-in avatars (see game_data/avatars.py)
    -- unless avatar_custom_filename is set, in which case the uploaded file
    -- (under static/avatars/uploads/) takes precedence -- see
    -- web_helpers.avatar_url(). avatar_custom_filename is NULL until a
    -- job_tier==4 character spends currency to upload one (character.py's
    -- character_change_avatar).
    avatar_key TEXT NOT NULL DEFAULT 'avatar_01',
    avatar_custom_filename TEXT,
    -- set by an admin via /admin/sessions to temporarily block login
    -- (auth.login rejects it the same way it rejects a wrong password).
    is_locked INTEGER NOT NULL DEFAULT 0
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
    treasury INTEGER NOT NULL DEFAULT 0,
    king_character_id INTEGER,
    advisor_character_id INTEGER,
    general_character_id INTEGER,
    pending_challenge_seat TEXT,
    pending_challenge_character_id INTEGER,
    pending_challenge_authorized_at TEXT,
    morale_buff_expires_at TEXT,
    FOREIGN KEY (king_character_id) REFERENCES characters(id),
    FOREIGN KEY (advisor_character_id) REFERENCES characters(id),
    FOREIGN KEY (general_character_id) REFERENCES characters(id),
    FOREIGN KEY (pending_challenge_character_id) REFERENCES characters(id)
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
    pvp_battles_count INTEGER NOT NULL DEFAULT 0,
    pvp_wins_count INTEGER NOT NULL DEFAULT 0,
    bank_balance INTEGER NOT NULL DEFAULT 0,
    job_class TEXT NOT NULL DEFAULT '初心者',
    job_tier INTEGER NOT NULL DEFAULT 0,
    rebirth_count INTEGER NOT NULL DEFAULT 0,
    stat_floor_hp INTEGER,
    stat_floor_mp INTEGER,
    stat_floor_str INTEGER,
    stat_floor_def INTEGER,
    stat_floor_agi INTEGER,
    stat_floor_luk INTEGER,
    pending_boss_monster_id INTEGER,
    level_bonus_hp INTEGER NOT NULL DEFAULT 0,
    level_bonus_mp INTEGER NOT NULL DEFAULT 0,
    level_bonus_str INTEGER NOT NULL DEFAULT 0,
    level_bonus_def INTEGER NOT NULL DEFAULT 0,
    level_bonus_agi INTEGER NOT NULL DEFAULT 0,
    level_bonus_luk INTEGER NOT NULL DEFAULT 0,
    equipped_skill_1 TEXT,
    equipped_skill_2 TEXT,
    is_npc INTEGER NOT NULL DEFAULT 0,
    rename_count INTEGER NOT NULL DEFAULT 0,
    avatar_change_count INTEGER NOT NULL DEFAULT 0,
    contribution INTEGER NOT NULL DEFAULT 0,
    donated_today INTEGER NOT NULL DEFAULT 0,
    donated_today_date TEXT,
    garrison_cooldown_until TEXT,
    income_claimed_at TEXT,
    siege_attack_next_at TEXT,
    -- DEFAULT 1 so a fresh DB's schema-created rows never get nagged with the
    -- tutorial prompt; _create_character() explicitly overrides this to 0 in
    -- its INSERT so brand-new characters DO get prompted (see the
    -- ALTER TABLE migration note in db.py for why existing DBs are backfilled
    -- the same way).
    tutorial_seen INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (country_id) REFERENCES countries(id),
    FOREIGN KEY (current_tile_id) REFERENCES map_tiles(id),
    FOREIGN KEY (equipped_weapon_id) REFERENCES items(id),
    FOREIGN KEY (equipped_armor_id) REFERENCES items(id),
    FOREIGN KEY (equipped_accessory_id) REFERENCES items(id),
    FOREIGN KEY (pending_boss_monster_id) REFERENCES monsters(id)
);

CREATE TABLE IF NOT EXISTS map_tiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    q INTEGER NOT NULL,
    r INTEGER NOT NULL,
    tile_type TEXT NOT NULL,
    name TEXT NOT NULL,
    country_id INTEGER,
    mayor_character_id INTEGER,
    bandit_hp INTEGER,
    defense_reduction_percent INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (country_id) REFERENCES countries(id),
    FOREIGN KEY (mayor_character_id) REFERENCES characters(id)
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
    fortress_defense_level INTEGER NOT NULL DEFAULT 1000,
    exp_growth_novice_percent REAL NOT NULL DEFAULT 6.6,
    exp_growth_tier2_percent REAL NOT NULL DEFAULT 6.0,
    exp_growth_tier3_percent REAL NOT NULL DEFAULT 0.8,
    exp_growth_tier4_percent REAL NOT NULL DEFAULT 0.8,
    rebirth_stat_bonus_percent REAL NOT NULL DEFAULT 15,
    stat_reroll_cost INTEGER NOT NULL DEFAULT 100000,
    war_town_weekday INTEGER NOT NULL DEFAULT 3,
    war_town_start_time TEXT NOT NULL DEFAULT '20:00',
    war_town_end_time TEXT NOT NULL DEFAULT '21:00',
    war_fortress_weekday INTEGER NOT NULL DEFAULT 5,
    war_fortress_start_time TEXT NOT NULL DEFAULT '20:00',
    war_fortress_end_time TEXT NOT NULL DEFAULT '21:30',
    king_weekly_income_percent INTEGER NOT NULL DEFAULT 5,
    official_weekly_income_percent INTEGER NOT NULL DEFAULT 3,
    king_war_defense_bonus_percent INTEGER NOT NULL DEFAULT 5,
    office_challenge_aura_bonus_percent INTEGER NOT NULL DEFAULT 20,
    morale_buff_cost INTEGER NOT NULL DEFAULT 30000,
    morale_buff_bonus_percent INTEGER NOT NULL DEFAULT 5,
    siege_attack_cost INTEGER NOT NULL DEFAULT 30000,
    siege_attack_reduction_percent INTEGER NOT NULL DEFAULT 15,
    siege_attack_reduction_floor_percent INTEGER NOT NULL DEFAULT 70,
    siege_attack_cooldown_seconds INTEGER NOT NULL DEFAULT 900,
    defense_repair_cost_per_percent INTEGER NOT NULL DEFAULT 5000,
    tournament_registration_fee INTEGER NOT NULL DEFAULT 5000,
    tournament_treasury_cut_percent INTEGER NOT NULL DEFAULT 10,
    tournament_registration_deadline_weekday INTEGER NOT NULL DEFAULT 6,
    tournament_registration_deadline_time TEXT NOT NULL DEFAULT '20:00',
    tournament_start_weekday INTEGER NOT NULL DEFAULT 7,
    tournament_start_time TEXT NOT NULL DEFAULT '14:00',
    -- 秘境 (hidden ground) interrupt chances, as a PERCENT (so the 0.05
    -- default is 1/2000 per hunt, and 0.02 is 1/5000). Rolled only inside
    -- an ordinary /game/hunt action, wuji first so the two can never both
    -- fire in one action. The drop_percent pair gates the legendary set
    -- drop on a WIN of the matching hidden fight.
    hidden_taiji_trigger_percent REAL NOT NULL DEFAULT 0.05,
    hidden_wuji_trigger_percent REAL NOT NULL DEFAULT 0.02,
    hidden_taiji_drop_percent REAL NOT NULL DEFAULT 50,
    hidden_wuji_drop_percent REAL NOT NULL DEFAULT 30,
    avatar_change_base_cost INTEGER NOT NULL DEFAULT 5000
);

CREATE TABLE IF NOT EXISTS site_visits (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_views INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS site_visitors (
    visitor_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS job_masteries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    job_name TEXT NOT NULL,
    mastered_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(character_id, job_name),
    FOREIGN KEY (character_id) REFERENCES characters(id)
);

CREATE TABLE IF NOT EXISTS character_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    skill_key TEXT NOT NULL,
    learned_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(character_id, skill_key),
    FOREIGN KEY (character_id) REFERENCES characters(id)
);

CREATE TABLE IF NOT EXISTS character_skill_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL,
    skill_key TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    UNIQUE(character_id, skill_key),
    FOREIGN KEY (character_id) REFERENCES characters(id)
);

CREATE TABLE IF NOT EXISTS garrisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL UNIQUE,
    tile_id INTEGER NOT NULL,
    stationed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (character_id) REFERENCES characters(id),
    FOREIGN KEY (tile_id) REFERENCES map_tiles(id)
);

-- is_hidden marks a 秘境 (hidden ground): never offered in the player-facing
-- hunt dropdown, never chosen by picking a ground_id. The only way in is the
-- rare random interrupt rolled inside /game/hunt (see game_hunt), plus the
-- admin-only forced-monster dropdown. Each hidden ground holds exactly ONE
-- monster, which is neither is_boss nor is_guardian -- hidden encounters are
-- a third, separate path through game_hunt, not a reuse of either flag.
CREATE TABLE IF NOT EXISTS hunting_grounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tier TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    min_level INTEGER NOT NULL,
    max_level INTEGER NOT NULL,
    monster_exp INTEGER NOT NULL DEFAULT 0,
    is_hidden INTEGER NOT NULL DEFAULT 0
);

-- hidden_set_key/hidden_set_name/special_effect_* are all NULL for every
-- ordinary (purchasable) item. They are only ever set on the 10 legendary
-- 秘境 sets, which cannot be bought anywhere -- the shop's listing query
-- filters on `hidden_set_key IS NULL` -- and only enter play as a drop from
-- winning a hidden-ground fight. Unlike the partial 2-piece country-set
-- bonus, a hidden set's special effect is ALL-OR-NOTHING: it activates only
-- when all 3 pieces of the same hidden_set_key are equipped at once (see
-- character_special_effects in game_data/equipment.py).
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_type TEXT NOT NULL,
    name TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 0,
    stat TEXT NOT NULL,
    stat_bonus INTEGER NOT NULL DEFAULT 0,
    country_id INTEGER,
    hidden_set_key TEXT,
    hidden_set_name TEXT,
    special_effect_key TEXT,
    special_effect_percent INTEGER,
    FOREIGN KEY (country_id) REFERENCES countries(id)
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
    is_guardian INTEGER NOT NULL DEFAULT 0,
    level_min INTEGER,
    level_max INTEGER,
    hp INTEGER NOT NULL,
    atk INTEGER NOT NULL,
    def INTEGER NOT NULL,
    agi INTEGER NOT NULL,
    luk INTEGER NOT NULL DEFAULT 0,
    currency_reward INTEGER NOT NULL DEFAULT 0,
    exp_reward INTEGER NOT NULL DEFAULT 0,
    element TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (hunting_ground_id) REFERENCES hunting_grounds(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    initiator_character_id INTEGER NOT NULL,
    target_character_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    initiator_currency INTEGER NOT NULL DEFAULT 0,
    target_currency INTEGER NOT NULL DEFAULT 0,
    initiator_confirmed INTEGER NOT NULL DEFAULT 0,
    target_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (initiator_character_id) REFERENCES characters(id),
    FOREIGN KEY (target_character_id) REFERENCES characters(id)
);

-- 天下武道大會 (World Martial Arts Tournament): one weekly single-elimination
-- PvP bracket. There is no background scheduler in this app (same constraint
-- that made the officers' weekly treasury income a manual claim button), so
-- the whole cycle is driven lazily off an @app.before_request hook -- see
-- blueprints/tournament.py. registration_deadline_at/start_at are absolute
-- UTC-naive ACTION_DT_FORMAT instants computed ONCE when the row is created,
-- from the game_settings tournament_* weekday/time pairs.
CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'registration',   -- 'registration' | 'completed' | 'cancelled'
    registration_deadline_at TEXT NOT NULL,
    start_at TEXT NOT NULL,
    champion_character_id INTEGER,
    champion_name TEXT,
    champion_country_name TEXT,
    prize_pool INTEGER NOT NULL DEFAULT 0,
    treasury_cut INTEGER NOT NULL DEFAULT 0,
    cancelled_reason TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (champion_character_id) REFERENCES characters(id)
);

-- Every snap_* column freezes the registrant exactly as they were at signup
-- time, so the bracket that runs days later is completely independent of
-- anything the character does in the meantime (gear swaps, level ups,
-- renames, changing country, re-equipping skills).
CREATE TABLE IF NOT EXISTS tournament_registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    character_name TEXT NOT NULL,
    country_id INTEGER NOT NULL,
    country_name TEXT NOT NULL,
    fee_paid INTEGER NOT NULL,
    snap_hp INTEGER NOT NULL, snap_mp INTEGER NOT NULL, snap_str INTEGER NOT NULL,
    snap_def INTEGER NOT NULL, snap_agi INTEGER NOT NULL, snap_luk INTEGER NOT NULL,
    snap_element TEXT NOT NULL,
    snap_job_class TEXT NOT NULL, snap_job_tier INTEGER NOT NULL,
    snap_equipped_skill_1 TEXT, snap_equipped_skill_2 TEXT,
    snap_learned_skill_keys TEXT,
    -- Frozen 獨立傷害 (independent damage) percent from whatever hidden set
    -- the registrant had fully equipped at signup -- the bracket runs days
    -- later off snapshots alone, so this follows the same freeze-everything
    -- rule as every other snap_* column rather than re-reading live gear.
    snap_independent_damage_percent INTEGER NOT NULL DEFAULT 0,
    snap_avatar_key TEXT NOT NULL DEFAULT 'avatar_01',
    snap_avatar_custom_filename TEXT,
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
    FOREIGN KEY (character_id) REFERENCES characters(id),
    UNIQUE (tournament_id, character_id)
);

-- One row per GAME actually played (not per pairing): an ordinary match is a
-- single game_number=1 row, the best-of-3 final is 2 or 3 rows sharing the
-- same round_number/match_index, and a bye is a game_number=1 row with a NULL
-- registration_b_id and is_bye=1.
CREATE TABLE IF NOT EXISTS tournament_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL,
    match_index INTEGER NOT NULL,
    game_number INTEGER NOT NULL DEFAULT 1,
    registration_a_id INTEGER NOT NULL,
    registration_b_id INTEGER,               -- NULL means a bye
    winner_registration_id INTEGER NOT NULL,
    is_bye INTEGER NOT NULL DEFAULT 0,
    timed_out INTEGER NOT NULL DEFAULT 0,
    battle_log TEXT,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id),
    FOREIGN KEY (registration_a_id) REFERENCES tournament_registrations(id),
    FOREIGN KEY (registration_b_id) REFERENCES tournament_registrations(id),
    FOREIGN KEY (winner_registration_id) REFERENCES tournament_registrations(id)
);

CREATE TABLE IF NOT EXISTS trade_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    UNIQUE(trade_id, character_id, item_id),
    FOREIGN KEY (trade_id) REFERENCES trades(id),
    FOREIGN KEY (item_id) REFERENCES items(id)
);

-- channel is 'public' (everyone) or 'country' (scoped by country_id, NULL on
-- 'public' rows). character_name/country_name are denormalized snapshots at
-- send-time (matches this codebase's existing snapshot convention, e.g.
-- tournament_registrations) so a later rename/rejoin_country never rewrites
-- history in the chat log.
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    country_id INTEGER,
    character_id INTEGER NOT NULL,
    character_name TEXT NOT NULL,
    country_name TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (character_id) REFERENCES characters(id),
    FOREIGN KEY (country_id) REFERENCES countries(id)
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_feed ON chat_messages(channel, country_id, id);

-- 玩家意見箱: visible only to its own submitter (scoped by user_id, never a
-- public feed) and to admins, who are the only ones who can change status.
-- character_name is a denormalized snapshot at submit time (same convention
-- as chat_messages/tournament_registrations) -- a later rename doesn't
-- rewrite history here either.
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    character_name TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, id);
