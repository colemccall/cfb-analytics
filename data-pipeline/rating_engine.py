"""Rating engine — computes raw ratings, normalizes, and calculates overalls.

Key design decisions:
- Position-specific skill categories derived from what the stats actually measure
- No fake physical attributes (speed/strength/agility inferred from production stats)
- Only RELEVANT skills are computed per position
- Team quality (SP+, talent) scales stat-based ratings so weak-conference stats don't inflate
- Recruiting stars ONLY provide a floor for players with very low/no stats
- Players WITH individual stats get rated from those stats
- Players WITHOUT stats (linemen, backups) get team-proxy + wide deterministic jitter
  so teammates aren't clones
- Normalization curve targets median ~70
"""

import hashlib

# Position-specific skill categories — replaces the old generic 10-attribute system.
# Each entry maps position group → list of skill attribute names meaningful to that position.
SKILL_ATTRS = {
    "QB":  ["passRating", "deepBall", "accuracy", "mobility"],
    "RB":  ["rushing", "receiving", "powerRunning"],
    "FB":  ["blocking", "rushing", "receiving"],
    "WR":  ["receiving", "routeRunning", "yac"],
    "TE":  ["receiving", "blocking", "routeRunning"],
    "OL":  ["runBlock", "passBlock"],
    "DL":  ["passRush", "runStop"],
    "LB":  ["coverage", "runStop", "passRush"],
    "DB":  ["coverage", "tackling", "ballHawking"],
    "K":   ["power", "accuracy"],
    "P":   ["distance", "placement"],
    "LS":  ["runBlock", "passBlock"],  # long snappers: rated as specialists, not true OL
}

POSITION_OVERALL_WEIGHTS = {
    "QB":  {"passRating": 0.55, "accuracy": 0.22, "deepBall": 0.13, "mobility": 0.10},
    "RB":  {"rushing": 0.65, "powerRunning": 0.22, "receiving": 0.13},
    "FB":  {"blocking": 0.55, "rushing": 0.30, "receiving": 0.15},
    "WR":  {"receiving": 0.60, "routeRunning": 0.25, "yac": 0.15},
    "TE":  {"receiving": 0.50, "blocking": 0.28, "routeRunning": 0.22},
    "OL":  {"runBlock": 0.50, "passBlock": 0.50},
    "DL":  {"passRush": 0.55, "runStop": 0.45},
    "LB":  {"runStop": 0.40, "coverage": 0.38, "passRush": 0.22},
    "DB":  {"coverage": 0.48, "ballHawking": 0.22, "tackling": 0.30},
    "K":   {"power": 0.55, "accuracy": 0.45},
    "P":   {"distance": 0.50, "placement": 0.50},
    "LS":  {"runBlock": 0.50, "passBlock": 0.50},
}

POSITION_MAP = {
    "QB": "QB", "RB": "RB", "FB": "FB", "WR": "WR", "TE": "TE",
    "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL", "G": "OL", "T": "OL",
    "DL": "DL", "DT": "DL", "DE": "DL", "NT": "DL",
    "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB",
    "DB": "DB", "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB",
    "K": "K", "PK": "K", "P": "P",
    "EDGE": "DL", "ATH": "RB", "LS": "LS",
}

# Long snappers are specialists — not true OL blockers.
# They receive a fixed low-range rating and are excluded from OL depth charts.
LS_FIXED_RANGE = (52, 62)  # random rating in this range based on player hash


def _hash_jitter(player_id, attr_name, magnitude=5):
    h = hashlib.md5(f"{player_id}_{attr_name}".encode()).hexdigest()
    return (int(h[:8], 16) % (2 * magnitude + 1)) - magnitude


def _hash_float(player_id, attr_name):
    """Deterministic float 0.0-1.0."""
    h = hashlib.md5(f"{player_id}_{attr_name}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def get_position_group(position):
    return POSITION_MAP.get(position, "RB")


def _has_meaningful_stats(player_stats):
    """Check if a player has any non-zero individual stats."""
    for k, v in player_stats.items():
        try:
            if float(v) != 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def compute_raw_ratings(player_id, pos_group, player_stats, ppa_val,
                        team_stats, team_quality, recruit_stars, player_usage=None, position=None):
    """Compute raw skill ratings. No physical attributes — stats only.

    Each position gets its own skill categories derived from what the stats
    actually measure. Returns a dict of {skill_name: raw_value} containing
    only the skills relevant to pos_group.
    """
    raw = {}
    has_stats = _has_meaningful_stats(player_stats)

    # Snap/usage multiplier: scales stat-based ratings by play participation rate.
    if player_usage and has_stats:
        overall_usage = float(player_usage.get("overall") or 0)
        snap_mult = max(0.5, min(1.15, 0.5 + 0.65 * overall_usage))
    else:
        snap_mult = 1.0

    # Sample size multiplier: penalizes stat-based players with few games played.
    # A player with 2 great games should not rate the same as one with 12 great games.
    # Only applied when we have games data (0 means unknown → no penalty).
    if player_usage and has_stats:
        games = int(player_usage.get("games") or 0)
        if games >= 10:
            sample_mult = 1.0
        elif games >= 6:
            sample_mult = 0.90
        elif games >= 3:
            sample_mult = 0.75
        elif games > 0:
            sample_mult = 0.55
        else:
            sample_mult = 1.0  # no data → assume full season
    else:
        sample_mult = 1.0

    # Team quality multiplier: good team → 1.0x, bad team → 0.50x
    # Wider range (0.50-1.0 vs old 0.65-1.0) creates more separation between G5 and P4
    # players on no-stat proxy positions (OL/DL), reducing G5 starter inflation
    tq_mult = 0.50 + 0.50 * team_quality

    # Stat volume multiplier: penalizes low-volume performances beyond the games-played penalty.
    # A QB with 30 attempts should not rate the same as one with 300 attempts, even in 12 games.
    # Only applied to stat-based skill positions with sufficient game counts.
    volume_mult = 1.0
    if has_stats and pos_group == "QB":
        attempts = _safe_float(player_stats.get("passingATT", player_stats.get("passingAttempts", 0)))
        if attempts < 50:
            volume_mult = max(0.60, attempts / 50)
    elif has_stats and pos_group in ("RB", "FB"):
        carries = _safe_float(player_stats.get("rushingCAR", player_stats.get("rushingATT", 0)))
        if carries == 0:
            # Estimate from yards if carry count not available
            rush_yds_est = _safe_float(player_stats.get("rushingYDS", player_stats.get("rushingYards", 0)))
            carries = max(rush_yds_est / 5.0, 1.0)
        if carries < 50:
            volume_mult = max(0.65, carries / 50)
    elif has_stats and pos_group in ("WR", "TE"):
        receptions = _safe_float(player_stats.get("receivingREC", player_stats.get("receptions", 0)))
        if receptions < 15:
            volume_mult = max(0.60, receptions / 15)

    # Combined: quality × snap context × sample size × volume
    combined_mult = tq_mult * snap_mult * sample_mult * volume_mult

    # DL with individual defensive stats should use stat-based formulas
    has_defensive_stats = has_stats and any(
        k.startswith("defensive") and _safe_float(player_stats.get(k, 0)) > 0
        for k in player_stats
    )
    is_lineman = pos_group in ("OL", "DL") and not has_defensive_stats

    # ── QB ────────────────────────────────────────────────────────────────
    if pos_group == "QB" and has_stats:
        pass_yds = _safe_float(player_stats.get("passingYDS", player_stats.get("passingYards", 0)))
        pass_tds = _safe_float(player_stats.get("passingTD", player_stats.get("passingTDs", 0)))
        rush_yds = _safe_float(player_stats.get("rushingYDS", player_stats.get("rushingYards", 0)))
        completions = _safe_float(player_stats.get("passingCOMPLETIONS", player_stats.get("passingCOMP", player_stats.get("completions", 0))))
        attempts = _safe_float(player_stats.get("passingATT", player_stats.get("passingAttempts", player_stats.get("attempts", 0))))
        ints = _safe_float(player_stats.get("passingINT", player_stats.get("interceptions", 0)))

        # Efficiency metrics — only meaningful with sufficient attempts
        comp_pct = completions / attempts if attempts >= 5 else 0.0
        ypa = pass_yds / attempts if attempts >= 5 else 0.0

        # passRating: blends volume (yards, TDs) with efficiency (comp %, YPA)
        # YPA is the single best predictor of QB quality in college football
        raw["passRating"] = (pass_yds * 0.007 + pass_tds * 1.8 + completions * 0.02
                             + ypa * 4.0 + comp_pct * 8.0 - ints * 1.5) * combined_mult
        # deepBall: big-play ability — TD rate, YPA efficiency, PPA
        raw["deepBall"] = (pass_tds * 2.0 + ypa * 2.0 + ppa_val * 4.0) * combined_mult
        # accuracy: completion % and efficiency quality minus turnover penalty
        raw["accuracy"] = (comp_pct * 14.0 + ypa * 2.5 - ints * 2.5 + ppa_val * 1.5) * combined_mult
        raw["mobility"] = rush_yds * 0.025 * combined_mult

    # ── RB / FB ───────────────────────────────────────────────────────────
    elif pos_group in ("RB", "FB") and has_stats:
        rush_yds = _safe_float(player_stats.get("rushingYDS", player_stats.get("rushingYards", 0)))
        rush_tds = _safe_float(player_stats.get("rushingTD", player_stats.get("rushingTDs", 0)))
        ypc = _safe_float(player_stats.get("rushingYPC", player_stats.get("yardsPerRushAttempt", 0)))
        rec_yds = _safe_float(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))
        rec_tds = _safe_float(player_stats.get("receivingTD", player_stats.get("receivingTDs", 0)))
        receptions = _safe_float(player_stats.get("receivingREC", player_stats.get("receptions", 0)))

        raw["rushing"] = (rush_yds * 0.010 + rush_tds * 2.0 + ypc * 1.5) * combined_mult
        # Receiving: blends yards + TDs + catch volume — pass-catching backs rate separately from
        # pure power backs who have zero receiving stats
        raw["receiving"] = (rec_yds * 0.012 + rec_tds * 2.0 + receptions * 0.15) * combined_mult
        # powerRunning: TD volume + yardage bulk — measures yards-earned, not just YPC
        raw["powerRunning"] = (rush_yds * 0.006 + rush_tds * 2.0) * combined_mult
        if pos_group == "FB":
            raw["blocking"] = 3.0 * combined_mult  # no individual blocking stats; team proxy

    # ── WR / TE ───────────────────────────────────────────────────────────
    elif pos_group in ("WR", "TE") and has_stats:
        rec_yds = _safe_float(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))
        rec_tds = _safe_float(player_stats.get("receivingTD", player_stats.get("receivingTDs", 0)))
        receptions = _safe_float(player_stats.get("receivingREC", player_stats.get("receptions", 0)))

        # Yards per reception: separates efficient big-play receivers from slot volume guys
        # Only meaningful with 3+ catches to avoid single-reception outliers
        ypr = rec_yds / receptions if receptions >= 3 else 0.0

        raw["receiving"] = (rec_yds * 0.010 + rec_tds * 2.0 + receptions * 0.10) * combined_mult
        # routeRunning: efficiency — receptions per opportunity, YPR, and PPA signal
        raw["routeRunning"] = (receptions * 0.10 + ypr * 0.15 + ppa_val * 2.5) * combined_mult
        # yac: big-play ability — TD rate, YPR (separates YAC monsters from short-route receivers), PPA
        raw["yac"] = (rec_tds * 2.5 + ypr * 0.20 + ppa_val * 2.0) * combined_mult
        if pos_group == "TE":
            raw["blocking"] = 3.0 * combined_mult  # no individual TE blocking stats

    # ── LB / DB / DL-with-stats ───────────────────────────────────────────
    elif (pos_group in ("LB", "DB") or (pos_group == "DL" and has_defensive_stats)) and has_stats:
        tackles = _safe_float(player_stats.get("defensiveTOT", player_stats.get("totalTackles", 0)))
        sacks = _safe_float(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        # Interceptions appear in TWO API stat categories depending on whether they're tracked
        # as raw defensive stats ("defensiveINT") or as return-stats ("interceptionsINT").
        # Take the maximum to avoid undercounting when only one source captures them.
        ints = max(
            _safe_float(player_stats.get("defensiveINT", player_stats.get("interceptions", 0))),
            _safe_float(player_stats.get("interceptionsINT", 0)),
        )
        pds = _safe_float(player_stats.get("defensivePD", player_stats.get("passesDeflected", 0)))
        tfl = _safe_float(player_stats.get("defensiveTFL", 0))
        qbh = _safe_float(player_stats.get("defensiveQBH", player_stats.get("defensiveQB HUR", 0)))
        ff = _safe_float(player_stats.get("defensiveFF", 0))

        if pos_group == "DL":
            # DL: pure pass rush and run stop — no coverage
            raw["passRush"] = (sacks * 3.5 + tfl * 1.2 + qbh * 1.0 + ff * 1.5) * combined_mult
            raw["runStop"] = (tackles * 0.08 + tfl * 2.0) * combined_mult

        elif pos_group == "LB":
            # LB: three-way — coverage, run stop, pass rush
            # MLB/ILB: primary run defenders, middle-zone coverage; boost runStop/coverage
            # OLB: edge setting + pass rush; boost passRush
            lb_pos = (position or "LB").upper()
            if lb_pos in ("OLB",):
                # Outside LB: edge rusher / hybrid — more pass rush, still can cover
                raw["coverage"] = (ints * 4.0 + pds * 1.0 + ppa_val * 2.0) * combined_mult
                raw["runStop"] = (tackles * 0.08 + tfl * 1.5) * combined_mult
                raw["passRush"] = (sacks * 3.5 + qbh * 1.5 + ff * 1.5) * combined_mult
            elif lb_pos in ("MLB", "ILB"):
                # Middle/inside LB: run stopper, zone anchor — boost tackles and TFL
                raw["coverage"] = (ints * 5.0 + pds * 1.2 + ppa_val * 2.5) * combined_mult
                raw["runStop"] = (tackles * 0.14 + tfl * 2.5) * combined_mult
                raw["passRush"] = (sacks * 1.8 + qbh * 0.8 + ff * 1.0) * combined_mult
            else:
                # Generic LB — balanced weights
                raw["coverage"] = (ints * 5.0 + pds * 1.2 + ppa_val * 2.5) * combined_mult
                raw["runStop"] = (tackles * 0.10 + tfl * 2.0) * combined_mult
                raw["passRush"] = (sacks * 2.5 + qbh * 1.0 + ff * 1.2) * combined_mult

        elif pos_group == "DB":
            # DB: coverage primary, ball hawking (turnovers), tackling
            # INTs are the definitive coverage play — weight 4x vs PDs which are more common
            raw["coverage"] = (ints * 4.0 + pds * 1.0 + ppa_val * 2.5) * combined_mult
            # tackling includes sacks — a DB with several sacks is an elite blitzer/run-stopper
            raw["tackling"] = (tackles * 0.10 + tfl * 1.0 + sacks * 2.5) * combined_mult
            # ballHawking = ability to create turnovers; INTs are 8x more valuable than PDs
            raw["ballHawking"] = (ints * 8.0 + pds * 1.0 + ff * 2.0) * combined_mult

    # ── K ─────────────────────────────────────────────────────────────────
    elif pos_group == "K" and has_stats:
        fgm = _safe_float(player_stats.get("kickingFGM", player_stats.get("fieldGoalsMade", 0)))
        fga = _safe_float(player_stats.get("kickingFGA", player_stats.get("fieldGoalAttempts", 0)))
        longest = _safe_float(player_stats.get("kickingLONG", player_stats.get("longFieldGoal", 0)))
        xpm = _safe_float(player_stats.get("kickingXPM", player_stats.get("extraPointsMade", 0)))
        xpa = _safe_float(player_stats.get("kickingXPA", player_stats.get("extraPointAttempts", 0)))
        fg_pct = fgm / max(fga, 1)
        xp_pct = xpm / max(xpa, 1)

        # power: longest FG is the clearest range signal; FGM rewards consistency; fg_pct bonus
        raw["power"] = (longest * 0.60 + fgm * 1.5 + fg_pct * 5.0) * combined_mult
        # accuracy: FG% is dominant; XP% is table stakes but still differentiates; penalize misses
        raw["accuracy"] = (fg_pct * 15.0 + xp_pct * 5.0 - (1.0 - fg_pct) * 3.0 + fgm * 0.3) * combined_mult

    # ── P ─────────────────────────────────────────────────────────────────
    elif pos_group == "P" and has_stats:
        punt_yds = _safe_float(player_stats.get("puntingYDS", player_stats.get("puntYards", 0)))
        punt_no = _safe_float(player_stats.get("puntingNO", player_stats.get("punts", 0)))
        punt_long = _safe_float(player_stats.get("puntingLONG", player_stats.get("longPunt", 0)))
        punt_in20 = _safe_float(player_stats.get("puntingIN20", player_stats.get("puntsInsideTwenty", 0)))
        punt_avg = punt_yds / max(punt_no, 1)
        in20_rate = punt_in20 / max(punt_no, 1)

        # distance: avg is the core signal; longest shows ceiling; volume shows dependability
        raw["distance"] = (punt_avg * 0.65 + punt_long * 0.15 + punt_no * 0.05) * combined_mult
        # placement: inside-20 rate is the elite punter differentiator; volume confirms workload
        raw["placement"] = (in20_rate * 18.0 + punt_no * 0.10) * combined_mult

    # ── Long snappers: fixed specialist range, not rated as OL ───────────
    if pos_group == "LS":
        lo, hi = LS_FIXED_RANGE
        raw["runBlock"] = lo + _hash_float(player_id, "ls_runBlock") * (hi - lo)
        raw["passBlock"] = lo + _hash_float(player_id, "ls_passBlock") * (hi - lo)
        return raw

    # ── Linemen and no-stat players: team proxy + star signal + jitter ───
    if is_lineman or not raw:
        team_rush = _safe_float(team_stats.get("rushingYards", 0))
        team_sacks_allowed = _safe_float(team_stats.get("sacksAllowed", 0))
        team_sacks = _safe_float(team_stats.get("sacks", 0))

        # Recruiting stars act as the primary individual differentiator for no-stat players.
        # For players where we have no individual stats, stars tell us who the coaching staff
        # and recruiting services valued. 5-star gets meaningful boost over unranked walk-on.
        # stars_signal: 0 for unranked/1-star, 0.5 for 3-star, 1.0 for 4-star, 1.5 for 5-star
        stars_signal = max(0.0, (recruit_stars - 2) * 0.5)
        jitter_mag = 2  # smaller jitter since stars provide the main spread

        # Usage multiplier for no-stat linemen: separates starters (high usage) from backups.
        # A starter at ~65% usage gets ~1.0x; a backup at 20% gets ~0.35x; 0 usage → 0.5x.
        ol_usage_mult = 0.5  # default when no usage data (unknown)
        if player_usage:
            overall_usage = float(player_usage.get("overall") or 0)
            if overall_usage > 0:
                ol_usage_mult = max(0.30, min(1.20, overall_usage / 0.65))

        if pos_group == "OL":
            # Position-specific value multipliers:
            # - Tackles (LT especially) face premier edge rushers → highest passBlock value
            # - Guards/C drive run blocking via pulling, double-teams, zone combos → higher runBlock
            # - LT left side = blind side → premium over RT in pass protection
            ol_pos = (position or "OL").upper()
            OL_PASS_VALUE = {
                "LT": 1.25, "RT": 1.10, "OT": 1.17, "T": 1.17,
                "OG": 0.88, "G": 0.88,
                "C": 0.94,
                "OL": 1.0,
            }
            OL_RUN_VALUE = {
                "LT": 0.93, "RT": 0.95, "OT": 0.94, "T": 0.94,
                "OG": 1.12, "G": 1.12,  # guards key in zone/gap run schemes
                "C": 1.10,               # center controls combo blocks, line calls
                "OL": 1.0,
            }
            # Sack attribution: each position bears a different share of total sacks allowed.
            # Tackles face edge rushers (primary sack source); guards/center face interior.
            # LT bears more than RT since elite pass rushers align to the blind side.
            OL_SACK_SHARE = {
                "LT": 0.30, "RT": 0.22, "OT": 0.26, "T": 0.26,
                "OG": 0.13, "G": 0.13,
                "C": 0.09,
                "OL": 0.20,
            }
            pass_value = OL_PASS_VALUE.get(ol_pos, 1.0)
            run_value  = OL_RUN_VALUE.get(ol_pos, 1.0)
            sack_share = OL_SACK_SHARE.get(ol_pos, 0.20)

            # Attributed sacks: rescaled so total attribution ≈ team sacks for typical line
            attributed_sacks = team_sacks_allowed * sack_share * 5

            # runBlock: team rushing quality + usage + star signal + position run value + jitter
            run_base = team_rush * 0.0012 * tq_mult * ol_usage_mult + stars_signal
            raw["runBlock"] = (run_base + _hash_jitter(player_id, "runBlock", jitter_mag)) * run_value

            # passBlock: attributed sacks penalize the player's position share; lower = better
            pass_base = max(0, 5 - attributed_sacks * 0.035) * tq_mult * ol_usage_mult + stars_signal
            raw["passBlock"] = (pass_base + _hash_jitter(player_id, "passBlock", jitter_mag)) * pass_value

        elif pos_group == "DL":
            base = (team_sacks * 0.08 + 2) * tq_mult * ol_usage_mult + stars_signal
            raw["passRush"] = base + _hash_jitter(player_id, "passRush", jitter_mag)
            raw["runStop"] = base * 0.9 + _hash_jitter(player_id, "runStop", jitter_mag)

        else:
            # Non-lineman with no stats: low baseline + jitter for all position skills
            base = 1.5 * tq_mult + stars_signal
            for attr in SKILL_ATTRS.get(pos_group, ["runBlock", "passBlock"]):
                raw[attr] = base + _hash_jitter(player_id, attr, jitter_mag)

    # Recruiting star floor: only for players with very low raw scores
    if recruit_stars >= 3:
        star_floor = (recruit_stars - 2) * 1.5
        for attr in raw:
            if raw[attr] < star_floor:
                raw[attr] = star_floor + _hash_float(player_id, f"floor_{attr}") * 1.0

    return raw


def normalize_all_ratings(raw_by_player):
    """Normalize ratings. Curve targets: median ~70, top ~95-99, bottom ~45-55."""
    by_pos = {}
    for pid, info in raw_by_player.items():
        pos = info["pos"]
        by_pos.setdefault(pos, []).append((pid, info["raw"]))

    normalized = {}

    for pos, players in by_pos.items():
        attrs = SKILL_ATTRS.get(pos, ["blocking"])

        for attr in attrs:
            vals = sorted([p[1].get(attr, 0) for p in players])
            n = len(vals)
            if n == 0:
                continue

            for pid, raw in players:
                v = raw.get(attr, 0)
                below = sum(1 for x in vals if x < v)
                equal = sum(1 for x in vals if x == v)
                rank = (below + 0.5 * equal) / n

                # Curve: median (0.5) → 65, top 10% → 84+, top 3% → 91+, elite → 95-99
                # Compressed vs before to prevent 85-90 inflation for ordinary starters
                if rank <= 0.05:
                    rating = 38 + (rank / 0.05) * 9
                elif rank <= 0.20:
                    rating = 47 + ((rank - 0.05) / 0.15) * 10
                elif rank <= 0.50:
                    rating = 57 + ((rank - 0.20) / 0.30) * 11
                elif rank <= 0.75:
                    rating = 68 + ((rank - 0.50) / 0.25) * 9
                elif rank <= 0.90:
                    rating = 77 + ((rank - 0.75) / 0.15) * 7
                elif rank <= 0.97:
                    rating = 84 + ((rank - 0.90) / 0.07) * 7
                elif rank <= 0.995:
                    rating = 91 + ((rank - 0.97) / 0.025) * 4
                else:
                    rating = 95 + ((rank - 0.995) / 0.005) * 4

                rating = max(40, min(99, int(round(rating))))
                if pid not in normalized:
                    normalized[pid] = {}
                normalized[pid][attr] = rating

    return normalized


def compute_overall(ratings, pos_group):
    weights = POSITION_OVERALL_WEIGHTS.get(pos_group, POSITION_OVERALL_WEIGHTS["RB"])
    total = 0
    weight_sum = 0
    for attr, w in weights.items():
        if w > 0 and attr in ratings:
            v = ratings[attr]
            # QB mobility: pocket passers should not be penalized for low rush production.
            # Mobile QBs get a bonus above the neutral baseline (65); immobile QBs are neutral.
            if pos_group == "QB" and attr == "mobility":
                v = max(v, 65)
            total += v * w
            weight_sum += w
    if weight_sum == 0:
        return 55
    return max(40, min(99, int(round(total / weight_sum))))
