"""Rating engine — computes raw ratings, normalizes, and calculates overalls.

Key design decisions:
- Only RELEVANT attributes are computed per position
- Team quality (SP+, talent) scales stat-based ratings so weak-conference stats don't inflate
- Recruiting stars ONLY provide a floor for players with very low/no stats
- Physical measurables are NOT used (short players can be fast/strong)
- Players WITH individual stats get rated from those stats
- Players WITHOUT stats (linemen, backups) get team-proxy + wide deterministic jitter
  so teammates aren't clones
- Normalization curve targets median ~70
"""

import hashlib

ATTRIBUTES = [
    "speed", "strength", "agility", "awareness",
    "throwing", "catching", "carrying", "blocking",
    "tackling", "kickPower",
]

RELEVANT_ATTRS = {
    "QB":  ["throwing", "awareness", "speed", "agility", "carrying", "strength"],
    "RB":  ["carrying", "speed", "agility", "strength", "awareness", "catching"],
    "FB":  ["blocking", "strength", "carrying", "speed", "agility", "awareness", "catching"],
    "WR":  ["catching", "speed", "agility", "awareness", "carrying"],
    "TE":  ["catching", "blocking", "strength", "speed", "agility", "awareness"],
    "OL":  ["blocking", "strength", "awareness", "agility", "speed"],
    "DL":  ["tackling", "strength", "speed", "awareness", "agility"],
    "LB":  ["tackling", "speed", "strength", "awareness", "agility", "catching"],
    "DB":  ["speed", "agility", "awareness", "tackling", "catching"],
    "K":   ["kickPower", "awareness", "strength"],
    "P":   ["kickPower", "awareness", "strength"],
}

IRRELEVANT_DEFAULTS = {
    "QB":  {"catching": 40, "blocking": 38, "tackling": 35, "kickPower": 30},
    "RB":  {"throwing": 30, "blocking": 45, "tackling": 35, "kickPower": 25},
    "FB":  {"throwing": 28, "tackling": 42, "kickPower": 25},
    "WR":  {"throwing": 30, "blocking": 42, "strength": 48, "tackling": 32, "kickPower": 25},
    "TE":  {"throwing": 28, "carrying": 45, "tackling": 38, "kickPower": 25},
    "OL":  {"throwing": 25, "catching": 30, "carrying": 32, "tackling": 40, "kickPower": 25},
    "DL":  {"throwing": 25, "catching": 30, "carrying": 28, "blocking": 45, "kickPower": 25},
    "LB":  {"throwing": 28, "carrying": 32, "blocking": 42, "kickPower": 25},
    "DB":  {"throwing": 28, "carrying": 35, "blocking": 35, "strength": 48, "kickPower": 25},
    "K":   {"speed": 42, "agility": 42, "catching": 30, "carrying": 30, "blocking": 28, "tackling": 30},
    "P":   {"speed": 42, "agility": 42, "catching": 30, "carrying": 30, "blocking": 28, "tackling": 30},
}

POSITION_OVERALL_WEIGHTS = {
    # Primary attribute gets ~0.55 weight so truly elite production reaches 95+
    "QB":  {"throwing": 0.55, "awareness": 0.20, "carrying": 0.08, "speed": 0.07,
            "agility": 0.07, "strength": 0.03, "catching": 0.0, "blocking": 0.0,
            "tackling": 0.0, "kickPower": 0.0},
    "RB":  {"carrying": 0.50, "speed": 0.20, "agility": 0.13, "strength": 0.07,
            "awareness": 0.05, "catching": 0.05, "blocking": 0.0, "throwing": 0.0,
            "tackling": 0.0, "kickPower": 0.0},
    "FB":  {"blocking": 0.40, "strength": 0.25, "carrying": 0.15, "speed": 0.08,
            "agility": 0.07, "awareness": 0.05, "catching": 0.0, "tackling": 0.0,
            "throwing": 0.0, "kickPower": 0.0},
    "WR":  {"catching": 0.50, "speed": 0.22, "agility": 0.12, "awareness": 0.10,
            "carrying": 0.06, "strength": 0.0, "blocking": 0.0, "throwing": 0.0,
            "tackling": 0.0, "kickPower": 0.0},
    "TE":  {"catching": 0.35, "blocking": 0.25, "strength": 0.15, "awareness": 0.12,
            "speed": 0.08, "agility": 0.05, "carrying": 0.0, "tackling": 0.0,
            "throwing": 0.0, "kickPower": 0.0},
    "OL":  {"blocking": 0.55, "strength": 0.28, "awareness": 0.10, "agility": 0.05,
            "speed": 0.02, "tackling": 0.0, "carrying": 0.0, "catching": 0.0,
            "throwing": 0.0, "kickPower": 0.0},
    "DL":  {"tackling": 0.45, "strength": 0.28, "speed": 0.12, "agility": 0.10,
            "awareness": 0.05, "blocking": 0.0, "carrying": 0.0, "catching": 0.0,
            "throwing": 0.0, "kickPower": 0.0},
    "LB":  {"tackling": 0.45, "awareness": 0.20, "speed": 0.13, "strength": 0.12,
            "agility": 0.07, "catching": 0.03, "blocking": 0.0, "carrying": 0.0,
            "throwing": 0.0, "kickPower": 0.0},
    "DB":  {"speed": 0.30, "awareness": 0.28, "tackling": 0.18, "agility": 0.14,
            "catching": 0.10, "strength": 0.0, "blocking": 0.0, "carrying": 0.0,
            "throwing": 0.0, "kickPower": 0.0},
    "K":   {"kickPower": 0.60, "awareness": 0.28, "strength": 0.08, "agility": 0.04,
            "speed": 0.0, "blocking": 0.0, "carrying": 0.0, "catching": 0.0,
            "tackling": 0.0, "throwing": 0.0},
    "P":   {"kickPower": 0.60, "awareness": 0.28, "strength": 0.08, "agility": 0.04,
            "speed": 0.0, "blocking": 0.0, "carrying": 0.0, "catching": 0.0,
            "tackling": 0.0, "throwing": 0.0},
}

POSITION_MAP = {
    "QB": "QB", "RB": "RB", "FB": "FB", "WR": "WR", "TE": "TE",
    "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL", "G": "OL", "T": "OL",
    "DL": "DL", "DT": "DL", "DE": "DL", "NT": "DL",
    "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB",
    "DB": "DB", "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB",
    "K": "K", "PK": "K", "P": "P",
    "EDGE": "DL", "ATH": "RB", "LS": "OL",
}


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
                        team_stats, team_quality, recruit_stars, player_usage=None):
    """Compute raw ratings. No physicals — stats only.

    Players with individual stats get rated from those stats.
    Players without stats get team-proxy ratings with wide jitter.
    Recruiting stars only act as a floor for low-stat players.
    """
    raw = {}
    has_stats = _has_meaningful_stats(player_stats)

    # Snap/usage multiplier: scales stat-based ratings by play participation rate.
    # Players with low usage (backups, garbage time) get proportionally lower raw scores.
    # Only applies to players with actual stats. If no usage data, neutral (1.0).
    if player_usage and has_stats:
        overall_usage = float(player_usage.get("overall") or 0)
        snap_mult = max(0.5, min(1.15, 0.5 + 0.65 * overall_usage))
    else:
        snap_mult = 1.0

    # Team quality multiplier: good team → 1.0x, bad team → 0.65x
    tq_mult = 0.65 + 0.35 * team_quality

    # Combined quality + snap context multiplier for stat-based players
    combined_mult = tq_mult * snap_mult

    # DL with individual defensive stats should be rated like LB/DB, not team-proxy
    has_defensive_stats = has_stats and any(
        k.startswith("defensive") and _safe_float(player_stats.get(k, 0)) > 0
        for k in player_stats
    )
    is_lineman = pos_group in ("OL", "DL") and not has_defensive_stats

    if pos_group == "QB" and has_stats:
        pass_yds = _safe_float(player_stats.get("passingYDS", player_stats.get("passingYards", 0)))
        pass_tds = _safe_float(player_stats.get("passingTD", player_stats.get("passingTDs", 0)))
        rush_yds = _safe_float(player_stats.get("rushingYDS", player_stats.get("rushingYards", 0)))
        completions = _safe_float(player_stats.get("passingCOMPLETIONS", player_stats.get("passingCOMP", player_stats.get("completions", 0))))
        ints = _safe_float(player_stats.get("passingINT", player_stats.get("interceptions", 0)))

        raw["throwing"] = (pass_yds * 0.01 + pass_tds * 2.0 + completions * 0.04 - ints * 1.5) * combined_mult
        raw["awareness"] = (pass_tds * 1.5 + ppa_val * 3.0 - ints * 2.0 + completions * 0.03) * combined_mult
        raw["speed"] = rush_yds * 0.02 * combined_mult
        raw["agility"] = rush_yds * 0.015 * combined_mult + ppa_val * 0.8
        raw["strength"] = 1.5 + rush_yds * 0.002
        raw["carrying"] = rush_yds * 0.015 * combined_mult

    elif pos_group in ("RB", "FB") and has_stats:
        rush_yds = _safe_float(player_stats.get("rushingYDS", player_stats.get("rushingYards", 0)))
        rush_tds = _safe_float(player_stats.get("rushingTD", player_stats.get("rushingTDs", 0)))
        ypc = _safe_float(player_stats.get("rushingYPC", player_stats.get("yardsPerRushAttempt", 0)))
        rec_yds = _safe_float(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))

        raw["carrying"] = (rush_yds * 0.01 + rush_tds * 2.0 + ypc * 1.5) * combined_mult
        raw["speed"] = (ypc * 2.0 + rush_yds * 0.005) * combined_mult
        raw["agility"] = (ypc * 1.5 + ppa_val * 2.0) * combined_mult
        raw["strength"] = (rush_yds * 0.005 + rush_tds * 0.8) * combined_mult
        raw["awareness"] = (ppa_val * 3.0 + rush_tds * 1.2) * combined_mult
        raw["catching"] = rec_yds * 0.02 * combined_mult
        if pos_group == "FB":
            raw["blocking"] = 3.0 * combined_mult

    elif pos_group in ("WR", "TE") and has_stats:
        rec_yds = _safe_float(player_stats.get("receivingYDS", player_stats.get("receivingYards", 0)))
        rec_tds = _safe_float(player_stats.get("receivingTD", player_stats.get("receivingTDs", 0)))
        receptions = _safe_float(player_stats.get("receivingREC", player_stats.get("receptions", 0)))

        raw["catching"] = (rec_yds * 0.01 + rec_tds * 2.0 + receptions * 0.1) * combined_mult
        raw["speed"] = (rec_yds * 0.007 + rec_tds * 1.2) * combined_mult
        raw["agility"] = (receptions * 0.12 + ppa_val * 2.0) * combined_mult
        raw["awareness"] = (ppa_val * 3.0 + rec_tds * 1.2 + receptions * 0.06) * combined_mult
        raw["carrying"] = rec_yds * 0.003 * combined_mult
        if pos_group == "TE":
            raw["blocking"] = 3.0 * combined_mult
            raw["strength"] = 2.5 * combined_mult

    elif (pos_group in ("LB", "DB") or (pos_group == "DL" and has_defensive_stats)) and has_stats:
        tackles = _safe_float(player_stats.get("defensiveTOT", player_stats.get("totalTackles", 0)))
        sacks = _safe_float(player_stats.get("defensiveSACKS", player_stats.get("sacks", 0)))
        ints = _safe_float(player_stats.get("defensiveINT", player_stats.get("interceptions", 0)))
        pds = _safe_float(player_stats.get("defensivePD", player_stats.get("passesDeflected", 0)))
        tfl = _safe_float(player_stats.get("defensiveTFL", 0))
        qbh = _safe_float(player_stats.get("defensiveQBH", 0))
        ff = _safe_float(player_stats.get("defensiveFF", 0))

        raw["tackling"] = (tackles * 0.08 + sacks * 2.5 + tfl * 1.5) * combined_mult
        raw["speed"] = (sacks * 1.5 + ints * 2.0 + qbh * 0.8 + ppa_val * 1.0) * combined_mult
        raw["awareness"] = (ints * 2.5 + pds * 1.2 + ppa_val * 2.5 + ff * 0.8) * combined_mult
        raw["strength"] = (tackles * 0.04 + sacks * 1.5 + tfl * 1.0) * combined_mult
        raw["agility"] = (ints * 2.0 + pds * 1.0 + qbh * 1.0 + ppa_val * 1.0) * combined_mult
        raw["catching"] = (ints * 2.0 + pds * 0.5) * combined_mult

    elif pos_group == "K" and has_stats:
        fgm = _safe_float(player_stats.get("kickingFGM", player_stats.get("fieldGoalsMade", 0)))
        fga = _safe_float(player_stats.get("kickingFGA", player_stats.get("fieldGoalAttempts", 0)))
        longest = _safe_float(player_stats.get("kickingLONG", player_stats.get("longFieldGoal", 0)))
        xpm = _safe_float(player_stats.get("kickingXPM", player_stats.get("extraPointsMade", 0)))
        xpa = _safe_float(player_stats.get("kickingXPA", player_stats.get("extraPointAttempts", 0)))
        fg_pct = fgm / max(fga, 1)
        xp_pct = xpm / max(xpa, 1)

        # Distance = power, accuracy = awareness
        raw["kickPower"] = (longest * 0.45 + fgm * 1.2 + fga * 0.25) * combined_mult
        raw["awareness"] = (fg_pct * 10.0 + xp_pct * 4.0 + fgm * 0.4) * combined_mult
        raw["strength"] = (longest * 0.20 + fgm * 0.5) * combined_mult

    elif pos_group == "P" and has_stats:
        punt_yds = _safe_float(player_stats.get("puntingYDS", player_stats.get("puntYards", 0)))
        punt_no = _safe_float(player_stats.get("puntingNO", player_stats.get("punts", 0)))
        punt_long = _safe_float(player_stats.get("puntingLONG", player_stats.get("longPunt", 0)))
        punt_in20 = _safe_float(player_stats.get("puntingIN20", player_stats.get("puntsInsideTwenty", 0)))
        punt_avg = punt_yds / max(punt_no, 1)
        in20_rate = punt_in20 / max(punt_no, 1)

        raw["kickPower"] = (punt_avg * 0.45 + punt_long * 0.12) * combined_mult
        raw["awareness"] = (in20_rate * 9.0 + punt_no * 0.08) * combined_mult
        raw["strength"] = punt_avg * 0.12 * combined_mult

    elif is_lineman:
        pass  # handled below
    elif not has_stats:
        pass  # no-stat non-lineman, handled below

    # Linemen AND no-stat players: team proxy + wide jitter
    if is_lineman or not raw:
        team_rush = _safe_float(team_stats.get("rushingYards", 0))
        team_sacks_allowed = _safe_float(team_stats.get("sacksAllowed", 0))
        team_sacks = _safe_float(team_stats.get("sacks", 0))

        # No-stat linemen: modest team proxy + small jitter so they cluster ~60-70 OVR
        # Jitter ±4 keeps teammates from being clones without inflating anyone into elite range
        jitter_mag = 4

        if pos_group == "OL":
            base = (team_rush * 0.001 + max(0, 5 - team_sacks_allowed * 0.04)) * tq_mult
            raw["blocking"] = base + _hash_jitter(player_id, "blocking", jitter_mag)
            raw["strength"] = base * 0.9 + _hash_jitter(player_id, "strength", jitter_mag)
            raw["awareness"] = base * 0.5 + _hash_jitter(player_id, "awareness", jitter_mag)
            raw["agility"] = base * 0.3 + _hash_jitter(player_id, "agility", jitter_mag)
            raw["speed"] = base * 0.2 + _hash_jitter(player_id, "speed", jitter_mag)
        elif pos_group == "DL":
            base = (team_sacks * 0.08 + 2) * tq_mult
            raw["tackling"] = base + _hash_jitter(player_id, "tackling", jitter_mag)
            raw["strength"] = base * 0.9 + _hash_jitter(player_id, "strength", jitter_mag)
            raw["speed"] = base * 0.5 + _hash_jitter(player_id, "speed", jitter_mag)
            raw["awareness"] = base * 0.4 + _hash_jitter(player_id, "awareness", jitter_mag)
            raw["agility"] = base * 0.4 + _hash_jitter(player_id, "agility", jitter_mag)
        else:
            # Non-lineman with no stats: low baseline + jitter
            base = 1.5 * tq_mult
            for attr in RELEVANT_ATTRS.get(pos_group, ATTRIBUTES[:5]):
                raw[attr] = base + _hash_jitter(player_id, attr, jitter_mag)

    # Recruiting star floor: only for players with very low raw scores
    # 5-star → floor of ~4, 4-star → ~2.5, 3-star → ~1, below → 0
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
        relevant = RELEVANT_ATTRS.get(pos, ATTRIBUTES)
        defaults = IRRELEVANT_DEFAULTS.get(pos, {})

        for attr in relevant:
            vals = sorted([p[1].get(attr, 0) for p in players])
            n = len(vals)
            if n == 0:
                continue

            for pid, raw in players:
                v = raw.get(attr, 0)
                below = sum(1 for x in vals if x < v)
                equal = sum(1 for x in vals if x == v)
                rank = (below + 0.5 * equal) / n

                # Curve: median (0.5) → 70, top 10% → 88+ (impact), top 3% → 95+ (dynasty)
                if rank <= 0.05:
                    rating = 40 + (rank / 0.05) * 10        # 40-50
                elif rank <= 0.20:
                    rating = 50 + ((rank - 0.05) / 0.15) * 10   # 50-60
                elif rank <= 0.50:
                    rating = 60 + ((rank - 0.20) / 0.30) * 10   # 60-70 (median=70)
                elif rank <= 0.75:
                    rating = 70 + ((rank - 0.50) / 0.25) * 10   # 70-80
                elif rank <= 0.90:
                    rating = 80 + ((rank - 0.75) / 0.15) * 8    # 80-88
                elif rank <= 0.97:
                    rating = 88 + ((rank - 0.90) / 0.07) * 7    # 88-95 (impact tier)
                elif rank <= 0.995:
                    rating = 95 + ((rank - 0.97) / 0.025) * 3   # 95-98 (dynasty tier)
                else:
                    rating = 98 + ((rank - 0.995) / 0.005) * 1  # 98-99

                rating = max(40, min(99, int(round(rating))))
                if pid not in normalized:
                    normalized[pid] = {}
                normalized[pid][attr] = rating

        for pid, raw in players:
            if pid not in normalized:
                normalized[pid] = {}
            for attr in ATTRIBUTES:
                if attr not in normalized[pid]:
                    base = defaults.get(attr, 45)
                    jitter = _hash_jitter(pid, f"default_{attr}", 4)
                    normalized[pid][attr] = max(25, min(55, base + jitter))

    return normalized


def compute_overall(ratings, pos_group):
    weights = POSITION_OVERALL_WEIGHTS.get(pos_group, POSITION_OVERALL_WEIGHTS["RB"])
    total = 0
    weight_sum = 0
    for attr, w in weights.items():
        if w > 0 and attr in ratings:
            total += ratings[attr] * w
            weight_sum += w
    if weight_sum == 0:
        return 55
    return max(40, min(99, int(round(total / weight_sum))))
