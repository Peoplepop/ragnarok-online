"""天下武道大會 (World Martial Arts Tournament): a weekly, fully automatic
single-elimination PvP bracket.

There is no background scheduler in this app (the same constraint that made
the officers' weekly treasury income a manual claim button), so "it runs by
itself every Sunday" is implemented as LAZY SETTLEMENT: _settle_due_cycle()
is wired into an @app.before_request hook and, on the first request that
arrives at or after the open cycle's start_at, resolves the ENTIRE bracket
synchronously in that one request and immediately opens the next week's
cycle. In the (overwhelmingly common) case where nothing is due, the hook
costs exactly one single-row SELECT.

Everything a registrant needs to fight is frozen into
tournament_registrations at signup time, so the bracket that runs days later
is completely unaffected by anything the character does in the meantime.
"""
import random
import sqlite3
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, session, flash

from db import get_db, log_activity
from web_helpers import (
    character_required, _next_weekly_instant_at, _taipei_time_label,
    _tournament_registration_open, ACTION_DT_FORMAT, avatar_url, background_url,
)
from game_data.equipment import _fetch_equipped_items, character_special_effects
from game_data.skills import _equipped_combat_skills, _learned_skill_keys
from game_data.stats import character_final_stats
from game_data.combat import run_pvp_duel

tournament_bp = Blueprint("tournament", __name__)


# ---------------------------------------------------------------------------
# Cycle bookkeeping (called from app.py's before_request hook)
# ---------------------------------------------------------------------------

def _latest_tournament(db):
    return db.execute("SELECT * FROM tournaments ORDER BY id DESC LIMIT 1").fetchone()


def _open_new_cycle(db):
    """Insert the next 'registration' cycle, freezing both of its timestamps
    ONCE, right now, as absolute UTC-naive instants.

    Note the deliberate edge case: both instants are independently "the next
    upcoming occurrence of that weekday+time", so a cycle that happens to be
    bootstrapped in the gap between the deadline weekday and the start
    weekday (e.g. first-ever run at Saturday 21:00 with the defaults) gets a
    start_at that precedes its own deadline. That cycle simply settles
    immediately with 0 registrants and is cancelled, and the cycle opened
    right after it is correctly ordered -- self-correcting after one empty
    week, which is cheaper than special-casing the bootstrap."""
    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    if settings is None:
        return  # DB not seeded yet -- the next request will retry
    db.execute(
        """INSERT INTO tournaments (status, registration_deadline_at, start_at)
           VALUES ('registration', ?, ?)""",
        (
            _next_weekly_instant_at(
                settings["tournament_registration_deadline_weekday"],
                settings["tournament_registration_deadline_time"],
            ),
            _next_weekly_instant_at(
                settings["tournament_start_weekday"], settings["tournament_start_time"],
            ),
        ),
    )


def _settle_due_cycle():
    """The whole lazy-scheduler entry point. Cheap no-op (one SELECT) unless
    the open cycle's start_at has actually arrived."""
    db = get_db()
    latest = _latest_tournament(db)

    if latest is None or latest["status"] != "registration":
        # Bootstraps the very first cycle, and re-opens the next one after a
        # settlement (or after an admin manually closed one out).
        _open_new_cycle(db)
        db.commit()
    elif datetime.utcnow() >= datetime.strptime(latest["start_at"], ACTION_DT_FORMAT):
        _settle_tournament(db, latest)
        _open_new_cycle(db)
        db.commit()

    db.close()


# ---------------------------------------------------------------------------
# Bracket generation and settlement
# ---------------------------------------------------------------------------

def _prepare_registrant(row):
    """Rehydrate one registration row's frozen snapshot into the shapes the
    duel engine wants: a six-stat dict, and the ordered skill list the
    character would have fought with at signup time (reconstructed through
    the same pure _equipped_combat_skills the live combat paths use, so the
    lineage-lock rules in _usable_skill_keys still apply)."""
    reg = dict(row)
    reg["stats"] = {
        "hp": row["snap_hp"], "mp": row["snap_mp"], "str": row["snap_str"],
        "def": row["snap_def"], "agi": row["snap_agi"], "luk": row["snap_luk"],
    }
    learned_keys = {k for k in (row["snap_learned_skill_keys"] or "").split(",") if k}
    reg["skills"] = _equipped_combat_skills(
        {
            "job_class": row["snap_job_class"], "job_tier": row["snap_job_tier"],
            "element": row["snap_element"],
        },
        learned_keys,
        [row["snap_equipped_skill_1"], row["snap_equipped_skill_2"]],
    )
    return reg


def _build_round1_pairs(registrations, byes_needed):
    """Round-1 pairing that can never pair a bye against a bye: the first
    `byes_needed` (already randomly shuffled) registrants each get one, and
    everyone left pairs up two at a time. Both invariants this relies on
    follow from P being the SMALLEST power of 2 >= N: byes_needed is always
    < P/2, and the leftover real-registrant count (N - byes_needed) is
    always even."""
    byed = registrations[:byes_needed]
    remaining = registrations[byes_needed:]
    return (
        [(r, None) for r in byed]
        + [(remaining[i], remaining[i + 1]) for i in range(0, len(remaining), 2)]
    )


def _insert_match(db, tournament_id, round_number, match_index, game_number,
                  a, b, winner, is_bye=False, timed_out=False, log_lines=()):
    db.execute(
        """INSERT INTO tournament_matches
           (tournament_id, round_number, match_index, game_number, registration_a_id,
            registration_b_id, winner_registration_id, is_bye, timed_out, battle_log)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tournament_id, round_number, match_index, game_number, a["id"],
            b["id"] if b is not None else None, winner["id"],
            1 if is_bye else 0, 1 if timed_out else 0,
            "\n".join(log_lines) if log_lines else None,
        ),
    )


def _resolve_game(db, tournament_id, round_number, match_index, game_number, a, b):
    """Resolves exactly one game and writes exactly one tournament_matches
    row. Both sides always start from their full snapshotted HP/MP -- damage
    never carries across games, including between the games of a final."""
    result = run_pvp_duel(
        a["character_name"], a["stats"], a["snap_element"], a["skills"],
        b["character_name"], b["stats"], b["snap_element"], b["skills"],
        # 獨立傷害 from a fully-equipped 秘境 火 set, read off each side's
        # frozen snapshot like every other combat input here -- gear swapped
        # after signup deliberately has no effect on the bracket.
        a_independent_damage_percent=a["snap_independent_damage_percent"],
        b_independent_damage_percent=b["snap_independent_damage_percent"],
    )

    if result["winner"] == "a":
        winner = a
    elif result["winner"] == "b":
        winner = b
    else:
        # Round cap reached with both still standing. Tiebreak (confirmed
        # with the user) is the higher REMAINING-HP PERCENTAGE, applied
        # uniformly to every timed-out game anywhere in the bracket -- a
        # percentage rather than an absolute so a naturally high-HP build
        # isn't handed the win for free. max(1, ...) only guards a division
        # that snapshotted stats can never actually make zero.
        a_pct = result["a_hp"] / max(1, a["snap_hp"])
        b_pct = result["b_hp"] / max(1, b["snap_hp"])
        if a_pct > b_pct:
            winner = a
        elif b_pct > a_pct:
            winner = b
        else:
            winner = random.choice([a, b])

    _insert_match(
        db, tournament_id, round_number, match_index, game_number, a, b, winner,
        timed_out=result["timed_out"], log_lines=result["log"],
    )
    return winner


def _resolve_best_of_3(db, tournament_id, round_number, match_index, a, b):
    """The final only. Plays up to 3 independent games (each its own
    tournament_matches row) and returns whoever wins 2 first."""
    wins = {a["id"]: 0, b["id"]: 0}
    winner = None
    for game_number in (1, 2, 3):
        winner = _resolve_game(db, tournament_id, round_number, match_index, game_number, a, b)
        wins[winner["id"]] += 1
        if wins[winner["id"]] == 2:
            return winner
    return winner  # unreachable: someone always reaches 2 wins within 3 games


def _settle_tournament(db, tournament):
    """Runs the complete bracket for one cycle, in one shot, and pays out."""
    tournament_id = tournament["id"]
    rows = db.execute(
        "SELECT * FROM tournament_registrations WHERE tournament_id = ? ORDER BY id",
        (tournament_id,),
    ).fetchall()

    if len(rows) < 2:
        for row in rows:
            db.execute(
                "UPDATE characters SET currency = currency + ? WHERE id = ?",
                (row["fee_paid"], row["character_id"]),
            )
        db.execute(
            """UPDATE tournaments
               SET status = 'cancelled', cancelled_reason = ?, completed_at = datetime('now')
               WHERE id = ?""",
            ("報名人數不足", tournament_id),
        )
        return

    registrations = [_prepare_registrant(row) for row in rows]
    random.shuffle(registrations)                      # random seeding
    n = len(registrations)
    bracket_size = 1
    while bracket_size < n:                            # smallest power of 2 >= n
        bracket_size *= 2
    byes_needed = bracket_size - n
    max_round = bracket_size.bit_length() - 1          # exact log2 of a power of 2

    current = _build_round1_pairs(registrations, byes_needed)
    random.shuffle(current)                            # cosmetic bracket positions
    round_number = 1
    while True:
        is_final = round_number == max_round
        winners = []
        for match_index, (a, b) in enumerate(current):
            if b is None:
                _insert_match(db, tournament_id, round_number, match_index, 1, a, None, a, is_bye=True)
                winners.append(a)
            elif is_final:
                winners.append(_resolve_best_of_3(db, tournament_id, round_number, match_index, a, b))
            else:
                winners.append(_resolve_game(db, tournament_id, round_number, match_index, 1, a, b))
        if len(winners) == 1:
            champion = winners[0]
            break
        # No byes past round 1, so this is always an even-length list.
        current = [(winners[i], winners[i + 1]) for i in range(0, len(winners), 2)]
        round_number += 1

    settings = db.execute("SELECT tournament_treasury_cut_percent FROM game_settings WHERE id = 1").fetchone()
    prize_pool = sum(r["fee_paid"] for r in registrations)
    treasury_cut = round(prize_pool * settings["tournament_treasury_cut_percent"] / 100)
    # Deliberately the remainder, not a second independent round(): the two
    # payouts must always sum to exactly prize_pool with no rounding leak.
    champion_payout = prize_pool - treasury_cut

    db.execute(
        "UPDATE characters SET currency = currency + ? WHERE id = ?",
        (champion_payout, champion["character_id"]),
    )
    # The champion's OWN country takes the cut (confirmed with the user: not
    # split between the finalists' countries). Uses the SNAPSHOT country_id,
    # like every other part of settlement -- the whole point of the snapshot
    # is that the bracket resolves as of signup time, and it keeps the cut
    # consistent with the denormalized champion_country_name shown alongside.
    db.execute(
        "UPDATE countries SET treasury = treasury + ? WHERE id = ?",
        (treasury_cut, champion["country_id"]),
    )
    db.execute(
        """UPDATE tournaments
           SET status = 'completed', champion_character_id = ?, champion_name = ?,
               champion_country_name = ?, prize_pool = ?, treasury_cut = ?,
               completed_at = datetime('now')
           WHERE id = ?""",
        (
            champion["character_id"], champion["character_name"], champion["country_name"],
            prize_pool, treasury_cut, tournament_id,
        ),
    )
    # System-triggered (settlement runs off the next incoming request's
    # before_request hook, not a specific player's action), so user_id is
    # NULL and username is a fixed system label -- this is what feeds the
    # 重大事件 panel on game.html (see _major_event_feed in web_helpers.py).
    log_activity(
        db, None, "系統", "tournament_champion",
        detail=f"{champion['character_name']}（{champion['country_name']}）",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _registration_character(db):
    """Everything needed to both validate the signup and build the frozen
    snapshot. NOTE the bare countries.* at the end: sqlite3.Row resolves a
    duplicated column name to the LAST one, so characters.id/name/country_id
    are all explicitly aliased -- character["id"] here would be the COUNTRY's
    id, exactly as documented throughout blueprints/game.py."""
    return db.execute(
        """SELECT characters.id AS character_id, characters.name AS character_name,
                  characters.country_id AS character_country_id, characters.currency,
                  characters.current_tile_id, characters.level, characters.exp,
                  characters.job_class, characters.job_tier, characters.rebirth_count,
                  characters.equipped_weapon_id, characters.equipped_armor_id,
                  characters.equipped_accessory_id,
                  characters.equipped_skill_1, characters.equipped_skill_2,
                  characters.stat_floor_hp, characters.stat_floor_mp, characters.stat_floor_str,
                  characters.stat_floor_def, characters.stat_floor_agi, characters.stat_floor_luk,
                  characters.level_bonus_hp, characters.level_bonus_mp, characters.level_bonus_str,
                  characters.level_bonus_def, characters.level_bonus_agi, characters.level_bonus_luk,
                  users.avatar_key, users.avatar_custom_filename,
                  map_tiles.tile_type, countries.*
           FROM characters
           JOIN users ON users.id = characters.user_id
           JOIN map_tiles ON map_tiles.id = characters.current_tile_id
           JOIN countries ON countries.id = characters.country_id
           WHERE characters.user_id = ?""",
        (session["user_id"],),
    ).fetchone()


@tournament_bp.route("/tournament/register", methods=["POST"])
@character_required
def tournament_register():
    db = get_db()
    character = _registration_character(db)

    # 報名只能在各地要塞報名 -- ANY country's fortress, not only your own.
    if character is None or character["tile_type"] != "fortress":
        db.close()
        flash("報名天下武道大會只能在要塞內進行")
        return redirect(url_for("game.game"))

    tournament = _latest_tournament(db)
    if not _tournament_registration_open(tournament):
        db.close()
        flash("目前不是天下武道大會的報名時間")
        return redirect(url_for("game.game"))

    already = db.execute(
        "SELECT id FROM tournament_registrations WHERE tournament_id = ? AND character_id = ?",
        (tournament["id"], character["character_id"]),
    ).fetchone()
    if already is not None:
        db.close()
        flash("你已經報名這一屆天下武道大會了")
        return redirect(url_for("game.game"))

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()
    fee = settings["tournament_registration_fee"]
    if character["currency"] < fee:
        db.close()
        flash(f"諸神幣不足，報名天下武道大會需要 {fee} 諸神幣")
        return redirect(url_for("game.game"))

    # Both bonus flags are deliberately False regardless of whatever war /
    # morale state happens to be live at this exact second: 天下武道大會 is a
    # sporting event, not a war mechanic, and freezing a transient national
    # buff that merely happened to be up at the literal moment of signup
    # would be arbitrary and trivially exploitable (sign up during 士氣激勵,
    # carry the buff into Sunday's bracket).
    equipped_items = _fetch_equipped_items(db, character)
    stats = character_final_stats(
        character, equipped_items, settings,
        king_war_defense_bonus=False, morale_buff_active=False,
    )
    # Comma-joined plain text rather than JSON: this codebase has no JSON
    # columns anywhere, and skill keys are simple identifiers (e.g. novice_火,
    # 業火尊者_1) that never contain a comma.
    learned_keys = ",".join(sorted(_learned_skill_keys(db, character["character_id"])))

    try:
        db.execute(
            """INSERT INTO tournament_registrations
               (tournament_id, character_id, character_name, country_id, country_name, fee_paid,
                snap_hp, snap_mp, snap_str, snap_def, snap_agi, snap_luk, snap_element,
                snap_job_class, snap_job_tier, snap_equipped_skill_1, snap_equipped_skill_2,
                snap_learned_skill_keys, snap_independent_damage_percent,
                snap_avatar_key, snap_avatar_custom_filename)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tournament["id"], character["character_id"], character["character_name"],
                # character["name"] is the COUNTRY's name here (bare
                # countries.* shadows characters.name, which is why the
                # character's own name is aliased to character_name).
                character["character_country_id"], character["name"], fee,
                stats["hp"], stats["mp"], stats["str"], stats["def"], stats["agi"], stats["luk"],
                character["element"], character["job_class"], character["job_tier"],
                character["equipped_skill_1"], character["equipped_skill_2"], learned_keys,
                character_special_effects(equipped_items).get("independent_damage", 0),
                # Frozen at signup like every other snap_* column -- a later
                # avatar change (or an as-yet-nonexistent character never
                # reaching job_tier 4) never rewrites an already-registered
                # entrant's look mid-cycle.
                character["avatar_key"], character["avatar_custom_filename"],
            ),
        )
    except sqlite3.IntegrityError:
        # UNIQUE(tournament_id, character_id) -- lost a double-submit race.
        db.close()
        flash("你已經報名這一屆天下武道大會了")
        return redirect(url_for("game.game"))

    db.execute(
        "UPDATE characters SET currency = currency - ? WHERE id = ?",
        (fee, character["character_id"]),
    )
    log_activity(
        db, session["user_id"], session["username"], "tournament_register",
        detail=f"報名天下武道大會，繳交報名費 {fee} 諸神幣", ip_address=request.remote_addr,
    )
    db.commit()
    db.close()

    flash(f"已報名天下武道大會，繳交報名費 {fee} 諸神幣")
    return redirect(url_for("game.game"))


def _bracket_rounds(db, tournament_id):
    """The finished bracket, grouped round -> match -> games, for display.
    Explicit aliases on every joined registration column (the registrations
    table is joined three times over)."""
    rows = db.execute(
        """SELECT tournament_matches.round_number, tournament_matches.match_index,
                  tournament_matches.game_number, tournament_matches.is_bye,
                  tournament_matches.timed_out, tournament_matches.battle_log,
                  ra.character_name AS a_name, ra.country_name AS a_country,
                  ra.snap_avatar_key AS a_avatar_key, ra.snap_avatar_custom_filename AS a_avatar_custom_filename,
                  rb.character_name AS b_name, rb.country_name AS b_country,
                  rb.snap_avatar_key AS b_avatar_key, rb.snap_avatar_custom_filename AS b_avatar_custom_filename,
                  rw.character_name AS winner_name
           FROM tournament_matches
           JOIN tournament_registrations AS ra ON ra.id = tournament_matches.registration_a_id
           LEFT JOIN tournament_registrations AS rb ON rb.id = tournament_matches.registration_b_id
           JOIN tournament_registrations AS rw ON rw.id = tournament_matches.winner_registration_id
           WHERE tournament_matches.tournament_id = ?
           ORDER BY tournament_matches.round_number, tournament_matches.match_index,
                    tournament_matches.game_number""",
        (tournament_id,),
    ).fetchall()
    if not rows:
        return []

    max_round = max(row["round_number"] for row in rows)
    rounds = []
    for row in rows:
        if not rounds or rounds[-1]["round_number"] != row["round_number"]:
            number = row["round_number"]
            if number == max_round:
                label = "決賽（三戰兩勝）"
            elif number == max_round - 1:
                label = "準決賽"
            else:
                label = f"第 {number} 輪"
            rounds.append({"round_number": number, "label": label, "matches": []})
        matches = rounds[-1]["matches"]
        if not matches or matches[-1]["match_index"] != row["match_index"]:
            matches.append({
                "match_index": row["match_index"], "a_name": row["a_name"],
                "a_country": row["a_country"], "b_name": row["b_name"],
                "b_country": row["b_country"], "is_bye": row["is_bye"], "games": [],
                "a_avatar_url": avatar_url(row["a_avatar_key"], row["a_avatar_custom_filename"]),
                "b_avatar_url": (
                    avatar_url(row["b_avatar_key"], row["b_avatar_custom_filename"])
                    if row["b_name"] is not None else None
                ),
            })
        matches[-1]["games"].append({
            "game_number": row["game_number"], "winner_name": row["winner_name"],
            "timed_out": row["timed_out"], "battle_log": row["battle_log"],
        })
    for r in rounds:
        for match in r["matches"]:
            match["winner_name"] = match["games"][-1]["winner_name"]
            match["any_timed_out"] = any(g["timed_out"] for g in match["games"])
    return rounds


@tournament_bp.route("/tournament")
@character_required
def tournament_page():
    db = get_db()
    character = _registration_character(db)
    tournament = _latest_tournament(db)

    registrants = []
    already_registered = False
    if tournament is not None:
        registrant_rows = db.execute(
            """SELECT character_name, country_name, snap_avatar_key, snap_avatar_custom_filename
               FROM tournament_registrations
               WHERE tournament_id = ? ORDER BY id""",
            (tournament["id"],),
        ).fetchall()
        registrants = [
            {
                "character_name": r["character_name"], "country_name": r["country_name"],
                "avatar_url": avatar_url(r["snap_avatar_key"], r["snap_avatar_custom_filename"]),
            }
            for r in registrant_rows
        ]
        if character is not None:
            already_registered = db.execute(
                "SELECT id FROM tournament_registrations WHERE tournament_id = ? AND character_id = ?",
                (tournament["id"], character["character_id"]),
            ).fetchone() is not None

    settings = db.execute("SELECT * FROM game_settings WHERE id = 1").fetchone()

    # The most recent COMPLETED cycle, which is normally one row behind the
    # open 'registration' one (settlement immediately opens the next cycle,
    # so the newest row is almost never the completed one).
    last_completed = db.execute(
        "SELECT * FROM tournaments WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    rounds = _bracket_rounds(db, last_completed["id"]) if last_completed is not None else []
    hall_of_fame = db.execute(
        "SELECT * FROM tournaments WHERE status = 'completed' ORDER BY id DESC"
    ).fetchall()
    db.close()

    registration_open = _tournament_registration_open(tournament)
    at_fortress = character is not None and character["tile_type"] == "fortress"
    return render_template(
        "tournament.html",
        tournament=tournament,
        page_background_url=background_url("tournament_bg"),
        registration_open=registration_open,
        registration_deadline_label=(
            _taipei_time_label(tournament["registration_deadline_at"]) if tournament else "-"
        ),
        start_label=_taipei_time_label(tournament["start_at"]) if tournament else "-",
        registrants=registrants,
        already_registered=already_registered,
        at_fortress=at_fortress,
        can_register=registration_open and at_fortress and not already_registered,
        fee=settings["tournament_registration_fee"],
        treasury_cut_percent=settings["tournament_treasury_cut_percent"],
        last_completed=last_completed,
        rounds=rounds,
        hall_of_fame=hall_of_fame,
    )
