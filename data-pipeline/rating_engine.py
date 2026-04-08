"""Rating engine — computes raw ratings, normalizes, and calculates overalls.

Design principles (v2 — Phase 4 overhaul):
- Snap-tier classification gates which rating pathway each player uses:
    Starter (>=35% snap): full stat formula + mild sample penalty
    Rotation (15-35%): full formula blended toward pos mean by confidence
    Backup (<15%): stars-anchored below team starter, no stat formula
    No-data (0%): stars x team ceiling, uniform attributes
- tq_mult narrowed to 0.70-1.00 for skill positions (was 0.50-1.00)
  because individual stats carry real signal regardless of team quality
- DL passRush: rate-based pressure per 100 opp pass plays (not raw sacks)
- RB explosiveness: big-carry rate from play-by-play (15+ yd carries)
- DB: coverage penalized by team defensive quality (SP+ def)
- OL: real multi-signal formula (team run eff, sack rate, SP+ off, stars,
  draft grade, awards) — no random jitter
- Normalization: hybrid 75% absolute historical threshold + 25% within-year
  percentile so elite years can have multiple 99s and weak years aren't
  artificially spread 40-99
"""

import hashlib

# ── Position skill attributes ─────────────────────────────────────────────────
# QB mobility is display-only — shown on card but excluded from overall weights.
SKILL_ATTRS = {
    "QB":  ["passVolume", "accuracy", "deepBall", "decisionMaking", "clutch", "mobility"],
    "RB":  ["rushing", "efficiency", "powerRunning", "receiving", "explosiveness"],
    "FB":  ["blocking", "rushing", "receiving"],
    "WR":  ["receiving", "routeRunning", "bigPlayAbility", "yac", "consistency"],
    "TE":  ["receiving", "blocking", "routeRunning", "bigPlayAbility"],
    "OL":  ["runBlock", "passBlock"],
    "DL":  ["passRush", "runStop"],
    "LB":  ["coverage", "runStop", "passRush"],
    "DB":  ["coverage", "tackling", "ballHawking"],
    "K":   ["power", "accuracy"],
    "P":   ["distance", "placement"],
    "LS":  ["runBlock", "passBlock"],
}

DISPLAY_ONLY_SKILLS = {"QB": {"mobility"}}

POSITION_OVERALL_WEIGHTS = {
    "QB":  {"passVolume": 0.20, "accuracy": 0.28, "deepBall": 0.17, "decisionMaking": 0.24, "clutch": 0.11},
    "RB":  {"rushing": 0.28, "efficiency": 0.25, "powerRunning": 0.18, "receiving": 0.15, "explosiveness": 0.14},
    "FB":  {"blocking": 0.55, "rushing": 0.30, "receiving": 0.15},
    "WR":  {"receiving": 0.28, "routeRunning": 0.25, "bigPlayAbility": 0.22, "yac": 0.14, "consistency": 0.11},
    "TE":  {"receiving": 0.42, "blocking": 0.25, "routeRunning": 0.20, "bigPlayAbility": 0.13},
    "OL":  {"runBlock": 0.50, "passBlock": 0.50},
    "DL":  {"passRush": 0.58, "runStop": 0.42},
    "LB":  {"runStop": 0.38, "coverage": 0.38, "passRush": 0.24},
    "DB":  {"coverage": 0.50, "ballHawking": 0.22, "tackling": 0.28},
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

LS_FIXED_RANGE = (52, 62)  # kept for reference but LS bypasses rating entirely

# OL position-specific value multipliers
_OL_PASS_VALUE = {"LT": 1.25, "RT": 1.10, "OT": 1.17, "T": 1.17, "OG": 0.88, "G": 0.88, "C": 0.94, "OL": 1.0}
_OL_RUN_VALUE  = {"LT": 0.93, "RT": 0.95, "OT": 0.94, "T": 0.94, "OG": 1.12, "G": 1.12, "C": 1.10, "OL": 1.0}
_OL_SACK_SHARE = {"LT": 0.30, "RT": 0.22, "OT": 0.26, "T": 0.26, "OG": 0.13, "G": 0.13, "C": 0.09, "OL": 0.20}

# Backup stars offset from team starter OVR
_BACKUP_STARS_OFFSET = {5: -3, 4: -8, 3: -15, 2: -22, 1: -28, 0: -33}
# No-data player stars ceiling (never exceeds team top-3 avg OVR at pos)
_NODATA_STARS_CEILING = {5: 72, 4: 65, 3: 58, 2: 50, 1: 46, 0: 44}

# Draft grade → individual quality signal for OL
_OL_DRAFT_SIGNAL  = {1: 4.0, 2: 3.0, 3: 2.0, 4: 1.5, 5: 1.5, 6: 0.8, 7: 0.8}
# Award tier → OL quality signal (2=All-American, 1=All-Conference)
_OL_AWARD_SIGNAL  = {2: 3.0, 1: 1.5, 0: 0.0}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hash_jitter(player_id, attr_name, magnitude=5):
    h = hashlib.md5(f"{player_id}_{attr_name}".encode()).hexdigest()
    return (int(h[:8], 16) % (2 * magnitude + 1)) - magnitude


def _hash_float(player_id, attr_name):
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
    for k, v in player_stats.items():
        try:
            if float(v) != 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def classify_snap_tier(snap_pct):
    """Snap%-based tier — used only as a secondary confidence signal when available.
    Returns 'starter' | 'rotation' | 'backup' | 'nodata'.
    Primary tier is determined by stat volume in compute_raw_ratings().
    """
    if snap_pct is None or snap_pct == 0:
        return "nodata"
    if snap_pct >= 0.35:
        return "starter"
    if snap_pct >= 0.15:
        return "rotation"
    return "backup"


# Stat-volume thresholds for tier classification (per position group).
# "full" = played a significant portion of the season as a contributor.
# "partial" = appeared meaningfully but not full-season.
# Below "partial" = backup/reserve, use star-anchor pathway.
_STAT_VOLUME_THRESHOLDS = {
    # pos_group: (full_threshold_stat, full_val, partial_val)
    "QB":  ("passingATT",    150, 40),   # ~10+ starts vs spot duty
    "RB":  ("rushingCAR",     80, 25),   # workhorse vs committee back
    "FB":  ("rushingCAR",     30, 10),
    "WR":  ("receivingREC",   25,  8),   # primary target vs occasional
    "TE":  ("receivingREC",   15,  4),
    "DL":  ("defensiveTOT",   25,  8),   # full-time starter vs rotational
    "LB":  ("defensiveTOT",   40, 12),
    "DB":  ("defensiveTOT",   30,  8),
    "K":   ("kickingFGA",      8,  2),
    "P":   ("puntingNO",       8,  3),
}

# Alternate stat keys for when primary key has different name in API response
_STAT_VOLUME_ALT = {
    "QB":  ("passingAttempts",),
    "RB":  ("rushingATT",),
    "FB":  ("rushingATT",),
    "WR":  ("receptions",),
    "TE":  ("receptions",),
    "DL":  ("totalTackles",),
    "LB":  ("totalTackles",),
    "DB":  ("totalTackles",),
    "K":   ("fieldGoalAttempts",),
    "P":   ("punts",),
}


def classify_tier_by_stats(pos_group, player_stats, snap_pct=0.0):
    """Classify player tier using stat volume as primary signal.

    Returns ('starter'|'rotation'|'backup'|'nodata', confidence: float 0-1).
    confidence=1.0 means full formula, no blending.
    confidence<1.0 means blend toward pos mean in proportion (1-confidence).
    """
    sf = _safe_float
    thresh = _STAT_VOLUME_THRESHOLDS.get(pos_group)
    if thresh is None:
        # OL has no individual stats — use snap% only (if available), else nodata
        if snap_pct >= 0.35:
            return "starter", 1.0
        if snap_pct >= 0.15:
            return "rotation", 0.60
        return "nodata", 0.0

    stat_key, full_val, partial_val = thresh
    alt_keys = _STAT_VOLUME_ALT.get(pos_group, ())

    vol = sf(player_stats.get(stat_key, 0))
    if vol == 0:
        for alt in alt_keys:
            vol = sf(player_stats.get(alt, 0))
            if vol > 0:
                break

    # Also check secondary stats for defensive positions
    if vol == 0 and pos_group in ("DL", "LB", "DB"):
        # Might have sacks/ints but low tackles
        sacks = sf(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        pds   = sf(player_stats.get("defensivePD", player_stats.get("passesDeflected", 0)))
        ints  = sf(player_stats.get("defensiveINT", player_stats.get("interceptions", 0)))
        vol = (sacks * 3 + pds * 2 + ints * 4)  # weighted equivalent tackles

    if vol == 0:
        # Truly no stats — use star-anchor pathway
        return "nodata", 0.0

    # Determine base tier from stat volume
    if vol >= full_val:
        base_tier = "starter"
        base_conf = 1.0
    elif vol >= partial_val:
        # Scale confidence linearly within the partial range
        base_conf = 0.50 + 0.45 * (vol - partial_val) / max(full_val - partial_val, 1)
        base_tier = "rotation"
    else:
        # Below partial threshold: small production
        base_conf = 0.30 + 0.20 * (vol / max(partial_val, 1))
        base_tier = "rotation"

    # Snap% (when available) can boost confidence for rotation players
    if snap_pct > 0 and base_tier == "rotation":
        snap_boost = min(0.20, snap_pct * 0.5)
        base_conf  = min(1.0, base_conf + snap_boost)
        if snap_pct >= 0.35:
            base_tier = "starter"
            base_conf = 1.0
    elif snap_pct >= 0.35:
        base_tier = "starter"
        base_conf = 1.0

    return base_tier, base_conf


# ── Raw rating computation ────────────────────────────────────────────────────

def compute_raw_ratings(player_id, pos_group, player_stats, ppa_val,
                        team_stats, team_quality, recruit_stars,
                        player_usage=None, position=None,
                        sp_off=0.5, sp_def=0.5,
                        draft_round=0, award_tier=0,
                        rb_explosive_lookup=None):
    """Compute raw skill ratings for one player.

    Returns dict of {skill_name: raw_value}.

    New in v2:
    - snap_tier gates pathway (starter/rotation/backup/nodata)
    - tq_mult narrowed to 0.70-1.00 for skill positions
    - DL passRush uses rate-based pressure per 100 opp pass plays
    - DB coverage penalized by team SP+ defensive quality
    - OL uses real multi-signal formula (no jitter except tiny tiebreaker)
    - RB explosiveness fed by play-by-play big-carry rate when available
    - FB/TE blocking uses team run efficiency signal
    """
    sf = _safe_float
    raw = {}
    has_stats = _has_meaningful_stats(player_stats)

    snap_pct = sf(player_usage.get("overall") if player_usage else 0)

    # Long snappers: bypass all tier logic — skip rating entirely (no individual stat formula)
    if pos_group == "LS":
        return raw

    # ── Tier classification ───────────────────────────────────────────────
    # Primary gate: stat volume per position (snap% is secondary boost only).
    # /player/usage API only covers ~25% of players; games is always 0 in API.
    # classify_tier_by_stats() handles both stat-rich and stat-poor players.
    snap_tier, tier_confidence = classify_tier_by_stats(pos_group, player_stats, snap_pct)

    if snap_tier == "nodata":
        # OL has no individual stats by design — use team-proxy formula for all OL
        # regardless of snap data. Other positions with no stats get star-anchor.
        if pos_group not in ("OL", "DL"):
            raw["_tier"] = "nodata"
            raw["_stars"] = recruit_stars
            return raw
        # OL/DL nodata: treat as low-confidence rotation so the team-proxy formula runs
        snap_tier = "rotation"
        tier_confidence = 0.40

    if snap_tier == "rotation" and tier_confidence < 1.0:
        raw["_rotation_confidence"] = tier_confidence

    # ── Multipliers ───────────────────────────────────────────────────────
    # tq_mult_skill: narrowed 0.70-1.00 — individual stats carry real signal
    # regardless of team quality. tq_mult_proxy: wider 0.50-1.00 for OL/no-stat DL.
    tq_mult_skill = 0.70 + 0.30 * team_quality
    tq_mult_proxy = 0.50 + 0.50 * team_quality

    has_defensive_stats = has_stats and any(
        k.startswith("defensive") and sf(player_stats.get(k, 0)) > 0
        for k in player_stats
    )
    is_lineman = pos_group in ("OL", "DL") and not has_defensive_stats

    combined_mult = tq_mult_skill

    # ── QB ────────────────────────────────────────────────────────────────
    if pos_group == "QB" and has_stats:
        pass_yds = sf(player_stats.get("passingYDS", player_stats.get("passingYards", 0)))
        pass_tds = sf(player_stats.get("passingTD",  player_stats.get("passingTDs", 0)))
        rush_yds = sf(player_stats.get("rushingYDS", player_stats.get("rushingYards", 0)))
        rush_tds = sf(player_stats.get("rushingTD",  player_stats.get("rushingTDs", 0)))
        comps    = sf(player_stats.get("passingCOMPLETIONS", player_stats.get("passingCOMP", player_stats.get("completions", 0))))
        attempts = sf(player_stats.get("passingATT", player_stats.get("passingAttempts", player_stats.get("attempts", 0))))
        ints     = sf(player_stats.get("passingINT", player_stats.get("interceptions", 0)))

        comp_pct = comps / attempts    if attempts >= 20 else 0.0
        ypa      = pass_yds / attempts if attempts >= 20 else 0.0
        int_rate = ints / attempts     if attempts >= 20 else 0.0
        # Adjusted YPA: NCAA passer rating proxy — rewards TDs, penalizes INTs per attempt
        adj_ypa  = ypa + (pass_tds * 20 - ints * 45) / max(attempts, 1)

        raw["passVolume"]     = (pass_yds * 0.007 + comps * 0.020 + pass_tds * 2.0) * combined_mult
        raw["accuracy"]       = (comp_pct * 12.0 + adj_ypa * 1.8 + ppa_val * 2.0) * combined_mult
        raw["deepBall"]       = (ypa * 3.5 + pass_tds * 1.2 + ppa_val * 4.0) * combined_mult
        raw["decisionMaking"] = (comp_pct * 8.0 - int_rate * 2.5 + adj_ypa * 1.5 + ppa_val * 2.5 + 5.0) * combined_mult
        raw["clutch"]         = 0.0   # filled by compute_gamelog_skills()
        raw["mobility"]       = (rush_yds * 0.022 + rush_tds * 2.0) * combined_mult  # display-only

    # ── RB / FB ───────────────────────────────────────────────────────────
    elif pos_group in ("RB", "FB") and has_stats:
        rush_yds = sf(player_stats.get("rushingYDS",  player_stats.get("rushingYards", 0)))
        rush_tds = sf(player_stats.get("rushingTD",   player_stats.get("rushingTDs", 0)))
        carries  = sf(player_stats.get("rushingCAR",  player_stats.get("rushingATT", 0)))
        ypc      = sf(player_stats.get("rushingYPC",  player_stats.get("yardsPerRushAttempt", 0)))
        rec_yds  = sf(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))
        rec_tds  = sf(player_stats.get("receivingTD",  player_stats.get("receivingTDs", 0)))
        recs     = sf(player_stats.get("receivingREC", player_stats.get("receptions", 0)))
        td_rate  = rush_tds / (rush_yds / 10.0) if rush_yds > 50 else 0.0

        raw["rushing"]      = (rush_yds * 0.009 + rush_tds * 2.2) * combined_mult
        raw["efficiency"]   = (ypc * 2.8 + ppa_val * 4.5 + 2.5) * combined_mult
        raw["powerRunning"] = (td_rate * 9.0 + rush_tds * 1.8) * combined_mult
        raw["receiving"]    = (rec_yds * 0.013 + rec_tds * 2.5 + recs * 0.20) * combined_mult
        raw["explosiveness"] = 0.0  # filled by compute_gamelog_skills() / PBP lookup

        if pos_group == "FB":
            # FB blocking: team run efficiency + SP+ offense + recruiting stars
            team_rush_att = sf(team_stats.get("rushingAttempts", 0))
            team_rush_yds = sf(team_stats.get("rushingYards", 0))
            team_run_ypc = team_rush_yds / max(team_rush_att, 100)
            raw["blocking"] = (team_run_ypc * 1.5 + sp_off * 0.03 + recruit_stars * 0.8 + 2.0) * tq_mult_skill

    # ── WR / TE ───────────────────────────────────────────────────────────
    elif pos_group in ("WR", "TE") and has_stats:
        rec_yds = sf(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))
        rec_tds = sf(player_stats.get("receivingTD",  player_stats.get("receivingTDs", 0)))
        recs    = sf(player_stats.get("receivingREC", player_stats.get("receptions", 0)))
        ypr     = rec_yds / recs if recs >= 5 else 0.0
        td_per_rec = rec_tds / max(recs, 1)

        raw["receiving"]      = (rec_yds * 0.010 + rec_tds * 2.2 + recs * 0.12) * combined_mult
        raw["routeRunning"]   = (recs * 0.14 + ppa_val * 3.5 + 3.5) * combined_mult
        raw["bigPlayAbility"] = (ypr * 0.28 + rec_tds * 2.0 + ppa_val * 3.0) * combined_mult
        raw["yac"]            = (ypr * 0.18 + td_per_rec * 12.0 + ppa_val * 1.8) * combined_mult
        raw["consistency"]    = 0.0  # filled by compute_gamelog_skills()

        if pos_group == "TE":
            team_rush_att = sf(team_stats.get("rushingAttempts", 0))
            team_rush_yds = sf(team_stats.get("rushingYards", 0))
            team_run_ypc = team_rush_yds / max(team_rush_att, 100)
            raw["blocking"] = (team_run_ypc * 1.2 + sp_off * 0.02 + recruit_stars * 0.6 + 2.0) * tq_mult_skill

    # ── DL (with individual defensive stats) ─────────────────────────────
    elif pos_group == "DL" and has_defensive_stats and has_stats:
        tackles = sf(player_stats.get("defensiveTOT",   player_stats.get("totalTackles", 0)))
        sacks   = sf(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        tfl     = sf(player_stats.get("defensiveTFL",   0))
        qbh     = sf(player_stats.get("defensiveQBH",   player_stats.get("defensiveQB HUR", 0)))
        ff      = sf(player_stats.get("defensiveFF",    0))

        # Rate-based pressure: normalize against opponent pass attempts so a DL on a
        # run-heavy team isn't punished for fewer raw sack opportunities.
        opp_pass_att = sf(team_stats.get("passAttemptsOpponent", 0))
        team_pass_faced = max(opp_pass_att, 350)  # fallback: typical FBS ~400/season
        pressure_events = sacks + tfl * 0.5 + qbh * 0.4
        pressure_rate = pressure_events / team_pass_faced * 100.0  # per 100 pass plays

        # Blend 70% rate + 30% volume: rewards efficiency AND full-season presence
        sack_vol = sacks + tfl * 0.4 + qbh * 0.3
        raw["passRush"] = (pressure_rate * 1.80 + sack_vol * 0.80 + ppa_val * 3.5 + ff * 1.2) * combined_mult
        raw["runStop"]  = (tackles * 0.09 + tfl * 2.2 + ppa_val * 1.5) * combined_mult

    # ── LB ────────────────────────────────────────────────────────────────
    elif pos_group == "LB" and has_stats:
        tackles = sf(player_stats.get("defensiveTOT",   player_stats.get("totalTackles", 0)))
        sacks   = sf(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        ints    = max(sf(player_stats.get("defensiveINT", player_stats.get("interceptions", 0))),
                      sf(player_stats.get("interceptionsINT", 0)))
        pds     = sf(player_stats.get("defensivePD",    player_stats.get("passesDeflected", 0)))
        tfl     = sf(player_stats.get("defensiveTFL",   0))
        qbh     = sf(player_stats.get("defensiveQBH",   player_stats.get("defensiveQB HUR", 0)))
        ff      = sf(player_stats.get("defensiveFF",    0))

        opp_pass_att = sf(team_stats.get("passAttemptsOpponent", 0))
        team_pass_faced = max(opp_pass_att, 350)
        pressure_events = sacks + tfl * 0.5 + qbh * 0.4
        pressure_rate   = pressure_events / team_pass_faced * 100.0

        lb_pos = (position or "LB").upper()
        if lb_pos == "OLB":
            raw["coverage"] = (ints * 4.5 + pds * 1.2 + ppa_val * 2.5) * combined_mult
            raw["runStop"]  = (tackles * 0.09 + tfl * 1.8) * combined_mult
            raw["passRush"] = (pressure_rate * 1.40 + sacks * 0.60 + qbh * 0.5 + ff * 1.2) * combined_mult
        elif lb_pos in ("MLB", "ILB"):
            raw["coverage"] = (ints * 5.5 + pds * 1.5 + ppa_val * 3.0) * combined_mult
            raw["runStop"]  = (tackles * 0.15 + tfl * 2.8) * combined_mult
            raw["passRush"] = (sacks * 1.5 + qbh * 0.6 + ff * 0.8) * combined_mult
        else:
            raw["coverage"] = (ints * 5.0 + pds * 1.4 + ppa_val * 2.8) * combined_mult
            raw["runStop"]  = (tackles * 0.11 + tfl * 2.3) * combined_mult
            raw["passRush"] = (pressure_rate * 0.80 + sacks * 0.70 + ff * 1.0) * combined_mult

    # ── DB ────────────────────────────────────────────────────────────────
    elif pos_group == "DB" and has_stats:
        tackles = sf(player_stats.get("defensiveTOT",   player_stats.get("totalTackles", 0)))
        sacks   = sf(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        ints    = max(sf(player_stats.get("defensiveINT", player_stats.get("interceptions", 0))),
                      sf(player_stats.get("interceptionsINT", 0)))
        pds     = sf(player_stats.get("defensivePD",    player_stats.get("passesDeflected", 0)))
        tfl     = sf(player_stats.get("defensiveTFL",   0))
        ff      = sf(player_stats.get("defensiveFF",    0))

        # Big-play-allowed penalty: DBs on bad defenses are in worse coverage situations.
        # sp_def is normalized 0-1 (higher = better defense).
        # Penalty is zero for top defenses, max ~3 for bottom defenses.
        # Scale by snap share — only penalize for time on the field.
        db_team_penalty = max(0.0, (1.0 - sp_def) * 3.0)
        individual_penalty = db_team_penalty * snap_pct * 1.5

        raw["coverage"]    = (ints * 4.5 + pds * 1.2 + ppa_val * 3.0 - individual_penalty) * combined_mult
        raw["tackling"]    = (tackles * 0.11 + tfl * 1.2 + sacks * 2.2) * combined_mult
        raw["ballHawking"] = (ints * 8.5 + pds * 1.2 + ff * 2.2) * combined_mult

    # ── K ─────────────────────────────────────────────────────────────────
    elif pos_group == "K" and has_stats:
        fgm     = sf(player_stats.get("kickingFGM", player_stats.get("fieldGoalsMade", 0)))
        fga     = sf(player_stats.get("kickingFGA", player_stats.get("fieldGoalAttempts", 0)))
        longest = sf(player_stats.get("kickingLONG", player_stats.get("longFieldGoal", 0)))
        xpm     = sf(player_stats.get("kickingXPM", player_stats.get("extraPointsMade", 0)))
        xpa     = sf(player_stats.get("kickingXPA", player_stats.get("extraPointAttempts", 0)))
        fg_pct  = fgm / max(fga, 1)
        xp_pct  = xpm / max(xpa, 1)
        # Bonus for proven 50+ yard range
        long50  = 1.0 if longest >= 50 else max(0.0, (longest - 40) / 10.0)

        raw["power"]    = (longest * 0.55 + fgm * 1.6 + long50 * 8.0) * combined_mult
        raw["accuracy"] = (fg_pct * 18.0 + xp_pct * 5.0 - (fga - fgm) * 0.8 + fgm * 0.25) * combined_mult

    # ── P ─────────────────────────────────────────────────────────────────
    elif pos_group == "P" and has_stats:
        punt_yds  = sf(player_stats.get("puntingYDS",  player_stats.get("puntYards", 0)))
        punt_no   = sf(player_stats.get("puntingNO",   player_stats.get("punts", 0)))
        punt_long = sf(player_stats.get("puntingLONG", player_stats.get("longPunt", 0)))
        punt_in20 = sf(player_stats.get("puntingIN20", player_stats.get("puntsInsideTwenty", 0)))
        punt_tb   = sf(player_stats.get("puntingTB",   player_stats.get("puntTouchbacks", 0)))
        punt_avg  = punt_yds / max(punt_no, 1)
        in20_rate = punt_in20 / max(punt_no, 1)
        tb_rate   = punt_tb   / max(punt_no, 1)

        raw["distance"]  = (punt_avg * 0.70 + punt_long * 0.12 + punt_no * 0.04) * combined_mult
        raw["placement"] = (in20_rate * 20.0 - tb_rate * 6.0 + punt_no * 0.08) * combined_mult

    # ── OL real formula (replaces jitter-based proxy) ─────────────────────
    if is_lineman and pos_group == "OL":
        ol_pos = (position or "OL").upper()
        pass_val = _OL_PASS_VALUE.get(ol_pos, 1.0)
        run_val  = _OL_RUN_VALUE.get(ol_pos, 1.0)
        sack_sh  = _OL_SACK_SHARE.get(ol_pos, 0.20)

        team_rush_yds = sf(team_stats.get("rushingYards", 0))
        team_rush_att = sf(team_stats.get("rushingAttempts", 0))
        team_sacks_al = sf(team_stats.get("sacksAllowed",   sf(team_stats.get("sacksOpponent", 0))))
        team_pass_att = sf(team_stats.get("passAttempts",   0))

        # Rate-based OL signals (not raw volume)
        team_run_eff  = team_rush_yds / max(team_rush_att, 100)   # yards per carry
        sack_rate_al  = team_sacks_al / max(team_pass_att, 200) * 100.0  # sacks per 100 attempts
        attributed_sr = sack_rate_al * sack_sh

        # Usage multiplier: separates starters from backups
        ol_usage_mult = {"starter": 1.00, "rotation": 0.55, "backup": 0.30, "nodata": 0.45}.get(snap_tier, 0.50)

        d_signal = _OL_DRAFT_SIGNAL.get(draft_round, 0.0)
        a_signal = _OL_AWARD_SIGNAL.get(award_tier,  0.0)
        stars_sig = max(0.0, (recruit_stars - 2) * 0.5)

        run_base  = (team_run_eff * 2.5 + sp_off * 0.045 + stars_sig + d_signal * 1.5 + a_signal * 2.0) * tq_mult_proxy * ol_usage_mult
        pass_base = (max(0.0, 7.0 - attributed_sr * 1.8) + sp_off * 0.040 + stars_sig + d_signal * 1.5 + a_signal * 2.0) * tq_mult_proxy * ol_usage_mult

        raw["runBlock"]  = run_base  * run_val  + _hash_jitter(player_id, "runBlock",  1)
        raw["passBlock"] = pass_base * pass_val + _hash_jitter(player_id, "passBlock", 1)

    # ── DL no-stat proxy ──────────────────────────────────────────────────
    elif is_lineman and pos_group == "DL":
        team_sacks   = sf(team_stats.get("sacks", 0))
        ol_usage_mult = {"starter": 1.00, "rotation": 0.55, "backup": 0.30, "nodata": 0.45}.get(snap_tier, 0.50)
        stars_sig = max(0.0, (recruit_stars - 2) * 0.5)
        base = (team_sacks * 0.08 + 2.0) * tq_mult_proxy * ol_usage_mult + stars_sig
        raw["passRush"] = base + _hash_jitter(player_id, "passRush", 1)
        raw["runStop"]  = base * 0.9 + _hash_jitter(player_id, "runStop",  1)

    # ── No-stat non-linemen ───────────────────────────────────────────────
    elif not raw or (len(raw) == 1 and "_rotation_confidence" in raw):
        stars_sig = max(0.0, (recruit_stars - 2) * 0.5)
        base = 1.5 * tq_mult_skill + stars_sig
        for attr in SKILL_ATTRS.get(pos_group, ["runBlock", "passBlock"]):
            raw[attr] = base + _hash_jitter(player_id, attr, 2)

    return raw


def _is_specialist(pos_group):
    return pos_group in ("K", "P", "LS")


# ── Gamelog skill fills ───────────────────────────────────────────────────────

def compute_gamelog_skills(raw_by_player, player_gamelogs, rb_explosive_lookup=None):
    """Fill in gamelog-derived skill placeholders.

    Called AFTER build_player_gamelogs() and BEFORE normalize_all_ratings().
    Modifies raw_by_player in-place.

    Skills filled:
    - QB clutch:        Peak game performance vs season avg (requires >= 2 games, >= 10 att)
    - RB explosiveness: Big-carry rate from PBP lookup; falls back to peak-YPC gamelog method
    - WR/TE consistency: Inverse CV of per-game receiving yards
    """
    if rb_explosive_lookup is None:
        rb_explosive_lookup = {}

    for pid_int, games in player_gamelogs.items():
        pid = str(pid_int)
        info = raw_by_player.get(pid)
        if not info:
            continue
        pos = info["pos"]

        if pos == "QB":
            scores = []
            for g in games:
                st  = g.get("stats", {})
                att = st.get("att", 0) or 0
                if att < 10:
                    continue
                comp    = st.get("comp",    0) or 0
                pass_yds = st.get("passYds", 0) or 0
                pass_tds = st.get("passTDs", 0) or 0
                ints     = st.get("ints",    0) or 0
                cp   = comp / att
                ypa  = pass_yds / att
                # Stricter threshold (10 att vs old 5) + heavier peak weight (60 vs 50)
                score = cp * 10.0 + ypa * 5.0 + pass_tds * 2.0 - ints * 3.0
                scores.append(score)
            if len(scores) >= 2:
                season_avg = sum(scores) / len(scores)
                top_n = sorted(scores)[-min(3, len(scores)):]
                top_avg = sum(top_n) / len(top_n)
                if season_avg > 0:
                    raw_clutch = (top_avg / season_avg - 1.0) * 60.0 + season_avg * 0.25
                else:
                    raw_clutch = 0.0
                info["raw"]["clutch"] = max(0.0, raw_clutch)

        elif pos == "RB":
            # Primary: play-by-play big-carry rate
            player_name = info.get("name", "").lower()
            pbp = rb_explosive_lookup.get(player_name, {})
            total_car = pbp.get("total_carries", 0)
            big_car   = pbp.get("big_carries",   0)

            if total_car >= 30:
                big_carry_rate = big_car / total_car
                raw_expl = big_carry_rate * 90.0
            elif total_car >= 10:
                # Blend toward league mean (~10%) to avoid small-sample extremes
                blend = total_car / 30.0
                blended_rate = blend * (big_car / total_car) + (1 - blend) * 0.10
                raw_expl = blended_rate * 90.0
            else:
                # Fallback: peak-YPC gamelog dispersion (original method)
                ypc_by_game = []
                for g in games:
                    st = g.get("stats", {})
                    rush_yds = st.get("rushYds", 0) or 0
                    ypc      = st.get("ypc",     0) or 0
                    if rush_yds >= 15 and ypc > 0:
                        ypc_by_game.append(ypc)
                if len(ypc_by_game) >= 2:
                    season_avg_ypc = sum(ypc_by_game) / len(ypc_by_game)
                    peak_ypc       = max(ypc_by_game)
                    raw_expl = peak_ypc * 0.5 + (peak_ypc - season_avg_ypc) * 2.0
                else:
                    raw_expl = 0.0

            info["raw"]["explosiveness"] = max(0.0, raw_expl)

        elif pos in ("WR", "TE"):
            yds_by_game = []
            for g in games:
                st  = g.get("stats", {})
                rec = st.get("receptions", 0) or 0
                if rec >= 1:
                    yds_by_game.append(st.get("recYds", 0) or 0)
            if len(yds_by_game) >= 3:
                mean_yds = sum(yds_by_game) / len(yds_by_game)
                if mean_yds > 0:
                    variance = sum((y - mean_yds) ** 2 for y in yds_by_game) / len(yds_by_game)
                    cv = (variance ** 0.5) / mean_yds
                    # cv=0 → raw 80 (perfectly consistent); cv=1.0 → raw 20
                    info["raw"]["consistency"] = (1.0 - min(cv, 1.0)) * 60.0 + 20.0


# ── Normalization ─────────────────────────────────────────────────────────────

def _piecewise_curve(rank):
    """Map percentile rank [0,1] to rating [38,99]. Used as the within-year component."""
    if rank <= 0.05:
        return 38 + (rank / 0.05) * 9
    elif rank <= 0.20:
        return 47 + ((rank - 0.05) / 0.15) * 10
    elif rank <= 0.50:
        return 57 + ((rank - 0.20) / 0.30) * 11
    elif rank <= 0.75:
        return 68 + ((rank - 0.50) / 0.25) * 9
    elif rank <= 0.90:
        return 77 + ((rank - 0.75) / 0.15) * 7
    elif rank <= 0.97:
        return 84 + ((rank - 0.90) / 0.07) * 7
    elif rank <= 0.995:
        return 91 + ((rank - 0.97) / 0.025) * 4
    else:
        return 95 + ((rank - 0.995) / 0.005) * 4


def _absolute_threshold_rating(raw_val, milestones):
    """Map raw value to 40-99 using sorted absolute threshold list.

    milestones: list of (rating, raw_threshold) sorted ascending by rating.
    Returns 40 below floor, 99 above ceiling, linear interp in between.
    Multiple players can earn 99 if they all exceed the top threshold.
    """
    if not milestones:
        return 70
    if raw_val <= milestones[0][1]:
        return 40
    if raw_val >= milestones[-1][1]:
        return 99
    for i in range(len(milestones) - 1):
        lo_r, lo_v = milestones[i]
        hi_r, hi_v = milestones[i + 1]
        if lo_v <= raw_val <= hi_v:
            frac = (raw_val - lo_v) / (hi_v - lo_v)
            return lo_r + frac * (hi_r - lo_r)
    return 99


# Absolute thresholds: computed from 2012-2024 full corpus.
# Structure: {pos_group: {attr: [(rating, raw_threshold), ...] sorted ascending}}
# Populated by build_absolute_thresholds() on first calibration run, then hardcoded.
# Until populated, the hybrid normalization falls back to 100% relative percentile.
ABSOLUTE_THRESHOLDS = {}


def build_absolute_thresholds(raw_by_year):
    """Build ABSOLUTE_THRESHOLDS from multi-year raw score corpus.

    raw_by_year: {year: {pid: {pos, raw: {attr: float}}}}
    Returns a new thresholds dict (does not mutate global).
    """
    from collections import defaultdict
    corpus = defaultdict(lambda: defaultdict(list))  # pos -> attr -> [raw_vals]
    for year_data in raw_by_year.values():
        for info in year_data.values():
            pos = info.get("pos")
            for attr, val in info.get("raw", {}).items():
                if attr.startswith("_"):
                    continue
                corpus[pos][attr].append(val)

    MILESTONES = [40, 50, 60, 65, 70, 75, 80, 85, 90, 95, 99]
    PCT_TARGETS = [0.05, 0.15, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.965, 0.995]

    thresholds = {}
    for pos, attrs in corpus.items():
        thresholds[pos] = {}
        for attr, vals in attrs.items():
            sorted_vals = sorted(vals)
            n = len(sorted_vals)
            pts = []
            for rating, pct in zip(MILESTONES, PCT_TARGETS):
                idx = min(int(pct * n), n - 1)
                pts.append((rating, sorted_vals[idx]))
            thresholds[pos][attr] = pts
    return thresholds


def normalize_all_ratings(raw_by_player, absolute_weight=0.75):
    """Hybrid normalization: absolute_weight * absolute_threshold + (1-abs) * within-year percentile.

    Players in backup/nodata tiers (raw has '_tier' key) are skipped — their
    ratings are set by compute_backup_ratings() in fetch_and_rate.py.
    Rotation players have a '_rotation_confidence' key; their raw scores are
    blended toward the position mean before the curve is applied.
    """
    relative_weight = 1.0 - absolute_weight

    # Separate active (starter/rotation) players from special-tier
    by_pos = {}
    for pid, info in raw_by_player.items():
        if "_tier" in info.get("raw", {}):
            continue  # backup/nodata — handled separately
        pos = info["pos"]
        by_pos.setdefault(pos, []).append((pid, info["raw"]))

    # First pass: compute position mean raw for rotation blending
    pos_mean_raw = {}
    for pos, players in by_pos.items():
        attrs = SKILL_ATTRS.get(pos, [])
        means = {}
        for attr in attrs:
            if attr.startswith("_"):
                continue
            vals = [p[1].get(attr, 0) for p in players]
            means[attr] = sum(vals) / len(vals) if vals else 0.0
        pos_mean_raw[pos] = means

    # Apply rotation confidence blend
    for pos, players in by_pos.items():
        means = pos_mean_raw.get(pos, {})
        for pid, raw in players:
            conf = raw.pop("_rotation_confidence", None)
            if conf is None:
                continue
            for attr in SKILL_ATTRS.get(pos, []):
                if attr.startswith("_") or attr not in raw:
                    continue
                stat_val = raw[attr]
                mean_val = means.get(attr, stat_val)
                raw[attr] = conf * stat_val + (1.0 - conf) * mean_val

    normalized = {}
    for pos, players in by_pos.items():
        attrs = SKILL_ATTRS.get(pos, ["blocking"])
        thresholds_pos = ABSOLUTE_THRESHOLDS.get(pos, {})

        for attr in attrs:
            if attr.startswith("_"):
                continue
            vals = [p[1].get(attr, 0) for p in players]
            sorted_vals = sorted(vals)
            n = len(sorted_vals)
            if n == 0:
                continue

            milestones = thresholds_pos.get(attr, [])

            for pid, raw in players:
                v = raw.get(attr, 0)

                # Relative (within-year) component
                below = sum(1 for x in sorted_vals if x < v)
                equal = sum(1 for x in sorted_vals if x == v)
                rank  = (below + 0.5 * equal) / n
                rel_r = _piecewise_curve(rank)

                # Absolute component (falls back to relative if no thresholds yet)
                if milestones:
                    abs_r = _absolute_threshold_rating(v, milestones)
                    rating = absolute_weight * abs_r + relative_weight * rel_r
                else:
                    rating = rel_r

                rating = max(40, min(99, int(round(rating))))
                if pid not in normalized:
                    normalized[pid] = {}
                normalized[pid][attr] = rating

    return normalized


def compute_backup_ratings(pid, pos_group, stars, starter_ovr, team_ceiling_ovr):
    """Compute normalized attribute dict for backup/nodata-tier players.

    backup tier: stars-anchored below team starter OVR.
    nodata tier:  stars x team_ceiling.

    Returns {attr: int} with uniform attribute values = computed OVR.
    """
    is_backup = starter_ovr is not None
    if is_backup:
        offset = _BACKUP_STARS_OFFSET.get(min(stars, 5), -33)
        ovr = max(40, min(starter_ovr - 2, starter_ovr + offset))
    else:
        ceiling = _NODATA_STARS_CEILING.get(min(stars, 5), 40)
        ovr = min(ceiling, team_ceiling_ovr or ceiling)

    return {attr: ovr for attr in SKILL_ATTRS.get(pos_group, ["runBlock", "passBlock"])}


def compute_overall(ratings, pos_group):
    """Weighted average of normalized skill attributes → single OVR.

    Display-only skills (e.g. QB mobility) are excluded so pocket passers
    are not penalized for low mobility scores.
    """
    weights     = POSITION_OVERALL_WEIGHTS.get(pos_group, POSITION_OVERALL_WEIGHTS["RB"])
    display_only = DISPLAY_ONLY_SKILLS.get(pos_group, set())
    total = weight_sum = 0
    for attr, w in weights.items():
        if w > 0 and attr in ratings and attr not in display_only:
            total      += ratings[attr] * w
            weight_sum += w
    if weight_sum == 0:
        return 55
    return max(40, min(99, int(round(total / weight_sum))))


def generate_rating_explanation(pos_group, player_stats, ppa_val, team_quality,
                                 recruit_stars, player_usage, normalized_attrs,
                                 overall, position=None):
    """Generate 2-4 human-readable bullet strings explaining what drove the rating.

    Returns a list of strings, e.g.:
      ["8 sacks + 12 TFL on the season", "Team quality: 0.74 (strong SP+/talent)"]
    """
    lines = []
    sf = _safe_float
    snap_pct = sf(player_usage.get("overall") if player_usage else 0)
    games    = int(player_usage.get("games", 0) if player_usage else 0)
    has_stats = _has_meaningful_stats(player_stats)

    def _pct_label(val, lo, hi):
        """Map val in [lo, hi] to a qualitative label."""
        if val is None:
            return ""
        frac = (val - lo) / max(hi - lo, 1)
        if frac >= 0.85:
            return "elite"
        if frac >= 0.65:
            return "above avg"
        if frac >= 0.40:
            return "average"
        if frac >= 0.20:
            return "below avg"
        return "low"

    # --- QB ---
    if pos_group == "QB" and has_stats:
        pass_yds = sf(player_stats.get("passingYDS", player_stats.get("passingYards", 0)))
        pass_tds = sf(player_stats.get("passingTD", player_stats.get("passingTDs", 0)))
        ints     = sf(player_stats.get("passingINT", player_stats.get("interceptions", 0)))
        attempts = sf(player_stats.get("passingATT", player_stats.get("passingAttempts", 0)))
        comps    = sf(player_stats.get("passingCOMPLETIONS", player_stats.get("passingCOMP", 0)))
        comp_pct = comps / attempts * 100 if attempts >= 20 else None
        ypa      = pass_yds / attempts if attempts >= 20 else None
        if pass_yds > 0 or pass_tds > 0:
            lines.append(f"{int(pass_yds):,} pass yds · {int(pass_tds)} TDs · {int(ints)} INTs"
                         + (f" · {comp_pct:.1f}% comp" if comp_pct else ""))
        if ypa:
            lines.append(f"{ypa:.1f} YPA ({_pct_label(ypa, 5.5, 10.0)})")
        if ppa_val:
            lines.append(f"PPA: {ppa_val:+.3f} ({_pct_label(ppa_val, -0.2, 0.5)})")

    # --- RB ---
    elif pos_group in ("RB", "FB") and has_stats:
        rush_yds = sf(player_stats.get("rushingYDS", player_stats.get("rushingYards", 0)))
        rush_tds = sf(player_stats.get("rushingTD", player_stats.get("rushingTDs", 0)))
        ypc      = sf(player_stats.get("rushingYPC", player_stats.get("yardsPerRushAttempt", 0)))
        rec_yds  = sf(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))
        if rush_yds > 0:
            lines.append(f"{int(rush_yds):,} rush yds · {int(rush_tds)} TDs"
                         + (f" · {ypc:.1f} YPC ({_pct_label(ypc, 3.5, 6.5)})" if ypc > 0 else ""))
        if rec_yds > 0:
            lines.append(f"{int(rec_yds)} recv yds (pass-catching back)")
        if ppa_val:
            lines.append(f"PPA: {ppa_val:+.3f} ({_pct_label(ppa_val, -0.1, 0.4)})")

    # --- WR / TE ---
    elif pos_group in ("WR", "TE") and has_stats:
        rec_yds  = sf(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))
        rec_tds  = sf(player_stats.get("receivingTD", player_stats.get("receivingTDs", 0)))
        recs     = sf(player_stats.get("receivingREC", player_stats.get("receptions", 0)))
        ypr      = rec_yds / recs if recs >= 3 else None
        if rec_yds > 0:
            lines.append(f"{int(rec_yds):,} recv yds · {int(rec_tds)} TDs · {int(recs)} rec"
                         + (f" · {ypr:.1f} YPR ({_pct_label(ypr, 8, 18)})" if ypr else ""))
        if ppa_val:
            lines.append(f"PPA: {ppa_val:+.3f} ({_pct_label(ppa_val, -0.1, 0.5)})")

    # --- DL ---
    elif pos_group == "DL" and has_stats:
        sacks = sf(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        tfl   = sf(player_stats.get("defensiveTFL", 0))
        qbh   = sf(player_stats.get("defensiveQBH", player_stats.get("defensiveQB HUR", 0)))
        if sacks > 0 or tfl > 0:
            lines.append(f"{sacks:.1f} sacks · {tfl:.1f} TFL"
                         + (f" · {int(qbh)} QBH" if qbh > 0 else ""))
        if ppa_val:
            lines.append(f"PPA: {ppa_val:+.3f} ({_pct_label(ppa_val, -0.3, 0.2)})")

    # --- LB ---
    elif pos_group == "LB" and has_stats:
        tackles = sf(player_stats.get("defensiveTOT", player_stats.get("totalTackles", 0)))
        sacks   = sf(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        ints    = max(sf(player_stats.get("defensiveINT", 0)), sf(player_stats.get("interceptionsINT", 0)))
        pds     = sf(player_stats.get("defensivePD", 0))
        if tackles > 0:
            lines.append(f"{int(tackles)} tackles · {sacks:.1f} sacks"
                         + (f" · {int(ints)} INTs · {int(pds)} PDs" if ints + pds > 0 else ""))
        if ppa_val:
            lines.append(f"PPA: {ppa_val:+.3f} ({_pct_label(ppa_val, -0.3, 0.2)})")

    # --- DB ---
    elif pos_group == "DB" and has_stats:
        tackles = sf(player_stats.get("defensiveTOT", player_stats.get("totalTackles", 0)))
        ints    = max(sf(player_stats.get("defensiveINT", 0)), sf(player_stats.get("interceptionsINT", 0)))
        pds     = sf(player_stats.get("defensivePD", 0))
        if ints + pds + tackles > 0:
            lines.append(f"{int(ints)} INTs · {int(pds)} PDs · {int(tackles)} tackles")
        if ppa_val:
            lines.append(f"PPA: {ppa_val:+.3f} ({_pct_label(ppa_val, -0.3, 0.2)})")

    # --- K ---
    elif pos_group == "K" and has_stats:
        fgm     = sf(player_stats.get("kickingFGM", 0))
        fga     = sf(player_stats.get("kickingFGA", 0))
        longest = sf(player_stats.get("kickingLONG", 0))
        fg_pct  = fgm / max(fga, 1) * 100
        if fga > 0:
            lines.append(f"{int(fgm)}/{int(fga)} FG ({fg_pct:.1f}%)"
                         + (f" · long {int(longest)} yds" if longest > 0 else ""))

    # --- P ---
    elif pos_group == "P" and has_stats:
        punt_yds = sf(player_stats.get("puntingYDS", 0))
        punt_no  = sf(player_stats.get("puntingNO", 0))
        in20     = sf(player_stats.get("puntingIN20", 0))
        punt_avg = punt_yds / max(punt_no, 1)
        if punt_no > 0:
            lines.append(f"{int(punt_no)} punts · {punt_avg:.1f} avg"
                         + (f" · {int(in20)} inside-20" if in20 > 0 else ""))

    # --- OL (no individual stats) ---
    elif pos_group == "OL":
        ol_pos = (position or "OL").upper()
        lines.append(f"Position: {ol_pos} — rated on team run/pass efficiency + snap share")
        if recruit_stars >= 4:
            lines.append(f"{recruit_stars}-star recruit")

    # --- Context bullets (added for all positions) ---
    if snap_pct > 0:
        snap_lbl = "starter" if snap_pct >= 0.35 else ("rotation" if snap_pct >= 0.15 else "backup")
        lines.append(f"{snap_pct*100:.0f}% snap share · {games} games ({snap_lbl})")

    tq_lbl = _pct_label(team_quality, 0.0, 1.0)
    lines.append(f"Team quality: {team_quality:.2f} ({tq_lbl} SP+/talent context)")

    # Trim to max 4 lines
    return lines[:4]
