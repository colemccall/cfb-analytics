"""CFB Data Pipeline — multi-year, fetches data, rates players, writes JSON."""

import os
import json
import hashlib
import sys

from api_client import (
    load_api_key, fetch_teams, fetch_all_rosters, fetch_player_stats,
    fetch_ppa, fetch_team_stats, fetch_sp_ratings, fetch_talent, fetch_recruiting,
    fetch_player_usage, fetch_awards,
)
from rating_engine import get_position_group, compute_raw_ratings, normalize_all_ratings, compute_overall, SKILL_ATTRS

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "assets", "data")
YEARS = [2022, 2023, 2024, 2025]

FIRST_NAMES = [
    "James", "John", "Robert", "Michael", "David", "Chris", "Daniel", "Marcus",
    "Anthony", "Josh", "Brandon", "Tyler", "Justin", "Ryan", "Kevin", "Andre",
    "Malik", "Devon", "Tre", "Jalen", "Darius", "Caleb", "Cam", "Jayden",
    "Isaiah", "Bryce", "Zach", "Nate", "Tyrone", "DeShawn", "Lamar", "Kyler",
    "Trey", "Devin", "Jordan", "Ray", "Eric", "Travis", "Corey", "Aaron",
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas",
    "Moore", "Jackson", "Martin", "Lee", "Thompson", "White", "Harris",
    "Clark", "Lewis", "Robinson", "Walker", "Young", "Allen", "King",
    "Wright", "Scott", "Hill", "Green", "Adams", "Baker", "Nelson",
    "Carter", "Mitchell", "Perez", "Roberts", "Turner", "Phillips",
    "Campbell", "Parker", "Evans", "Edwards", "Collins", "Stewart",
    "Morris", "Reed", "Cook", "Morgan", "Bell", "Murphy", "Bailey",
    "Rivera", "Cooper", "Richardson", "Cox", "Howard", "Ward", "Torres",
    "Peterson", "Gray", "Ramirez", "James", "Watson", "Brooks", "Kelly",
    "Sanders", "Price", "Bennett", "Wood", "Barnes", "Ross", "Henderson",
    "Coleman", "Jenkins", "Perry", "Powell", "Long", "Patterson", "Hughes",
    "Washington", "Butler", "Simmons", "Foster", "Gonzales", "Bryant",
    "Alexander", "Russell", "Griffin", "Diaz", "Hayes",
]


def generate_public_name(player_id, used_names):
    h = int(hashlib.md5(str(player_id).encode()).hexdigest(), 16)
    first_idx = h % len(FIRST_NAMES)
    for i in range(len(LAST_NAMES)):
        last_idx = (h // len(FIRST_NAMES) + i) % len(LAST_NAMES)
        candidate = (FIRST_NAMES[first_idx], LAST_NAMES[last_idx])
        key = f"{candidate[0]} {candidate[1]}"
        if key not in used_names:
            used_names.add(key)
            return candidate
    return (FIRST_NAMES[first_idx], f"Player{h % 1000}")


def build_stat_lookup(player_stats_raw):
    """Build lookup with multiple key variants for name matching."""
    lookup = {}
    for entry in player_stats_raw:
        player_name = entry.get("player", "")
        team = entry.get("team", "")
        key = (player_name.lower(), team.lower())
        if key not in lookup:
            lookup[key] = {}
        cat = entry.get("category", "")
        stat_type = entry.get("statType", "")
        stat_name = cat + stat_type[0].upper() + stat_type[1:] if stat_type else cat
        lookup[key][stat_name] = entry.get("stat", 0)
    return lookup


def find_player_stats(stat_lookup, first_name, last_name, team):
    """Try multiple name variants to find stats."""
    team_l = team.lower()
    full = f"{first_name} {last_name}".lower()
    # Exact match
    if (full, team_l) in stat_lookup:
        return stat_lookup[(full, team_l)]
    # Try last name with all first name variants in the lookup
    for (name, t), stats in stat_lookup.items():
        if t != team_l:
            continue
        parts = name.split(" ", 1)
        if len(parts) == 2 and parts[1] == last_name.lower():
            # Same last name, check if first name is a nickname match
            roster_first = first_name.lower()
            stat_first = parts[0]
            if (roster_first.startswith(stat_first) or stat_first.startswith(roster_first)):
                return stats
    return {}


def find_player_ppa(ppa_lookup, first_name, last_name, team):
    team_l = team.lower()
    full = f"{first_name} {last_name}".lower()
    if (full, team_l) in ppa_lookup:
        return ppa_lookup[(full, team_l)]
    for (name, t), val in ppa_lookup.items():
        if t != team_l:
            continue
        parts = name.split(" ", 1)
        if len(parts) == 2 and parts[1] == last_name.lower():
            roster_first = first_name.lower()
            stat_first = parts[0]
            if roster_first.startswith(stat_first) or stat_first.startswith(roster_first):
                return val
    return 0.0


def build_ppa_lookup(ppa_raw):
    lookup = {}
    for entry in ppa_raw:
        name = entry.get("name", "")
        team = entry.get("team", "")
        key = (name.lower(), team.lower())
        avg_ppa = entry.get("averagePPA", {})
        if isinstance(avg_ppa, dict):
            val = avg_ppa.get("all", 0) or 0
        else:
            val = avg_ppa or 0
        try:
            lookup[key] = float(val)
        except (TypeError, ValueError):
            lookup[key] = 0.0
    return lookup


def build_usage_lookup(usage_raw):
    """Build lookup by player ID: {player_id: {overall, pass, rush, games}}."""
    lookup = {}
    for entry in usage_raw:
        pid = entry.get("id")
        if not pid:
            continue
        usage = entry.get("usage") or {}
        lookup[int(pid)] = {
            "overall": float(usage.get("overall") or 0),
            "pass": float(usage.get("pass") or 0),
            "rush": float(usage.get("rush") or 0),
            "games": int(entry.get("games") or 0),
        }
    return lookup


def build_awards_lookup(awards_raw):
    """Map (name_lower, team_lower) -> award tier.
    Tier 3 = All-American / major lineman award (Outland, Rimington, Lombardi)
    Tier 2 = 1st-team All-Conference
    Tier 1 = 2nd-team / Honorable Mention All-Conference
    """
    lookup = {}
    for entry in awards_raw:
        name = (entry.get("player") or entry.get("name") or "").lower().strip()
        team = (entry.get("team") or "").lower().strip()
        award = (entry.get("award") or entry.get("category") or "").lower()
        if not name or not award:
            continue
        if any(x in award for x in ["all-american", "outland", "rimington", "lombardi", "bednarik"]):
            tier = 3
        elif "all-" in award and any(x in award for x in ["first", "1st"]):
            tier = 2
        elif "all-" in award and any(x in award for x in ["second", "2nd", "honorable"]):
            tier = 1
        else:
            continue
        key = (name, team)
        if key not in lookup or lookup[key] < tier:
            lookup[key] = tier
    return lookup


def build_draft_lookup(draft_data, college_year):
    """Map name_lower -> round for OL drafted after the given college year."""
    nfl_year = college_year + 1
    lookup = {}
    for entry in draft_data:
        if entry.get("nfl_year") != nfl_year or entry.get("_comment"):
            continue
        name = (entry.get("name") or "").lower().strip()
        if name:
            lookup[name] = entry.get("round", 7)
    return lookup


def build_team_stats_lookup(team_stats_raw):
    lookup = {}
    for entry in team_stats_raw:
        team = entry.get("team", "").lower()
        if team not in lookup:
            lookup[team] = {}
        lookup[team][entry.get("statName", "")] = entry.get("statValue", 0)
    return lookup


def build_team_quality(sp_ratings, talent_data):
    sp_map = {}
    for entry in sp_ratings:
        sp_map[entry.get("team", "").lower()] = entry.get("rating", 0)
    talent_map = {}
    for entry in talent_data:
        talent_map[entry.get("team", "").lower()] = entry.get("talent", 0)

    sp_vals = list(sp_map.values()) or [0]
    sp_min, sp_max = min(sp_vals), max(sp_vals)
    sp_range = sp_max - sp_min if sp_max > sp_min else 1
    tal_vals = list(talent_map.values()) or [0]
    tal_min, tal_max = min(tal_vals), max(tal_vals)
    tal_range = tal_max - tal_min if tal_max > tal_min else 1

    quality = {}
    for team in set(list(sp_map.keys()) + list(talent_map.keys())):
        sp_norm = (sp_map.get(team, 0) - sp_min) / sp_range if team in sp_map else 0.3
        tal_norm = (talent_map.get(team, 0) - tal_min) / tal_range if team in talent_map else 0.3
        quality[team] = 0.6 * sp_norm + 0.4 * tal_norm
    return quality


def build_sp_detail(sp_ratings):
    """Build per-team SP+ sub-ratings: overall, offense, defense, specialTeams.
    Returns dict: team_name_lower → {overall, off, def, st} each normalized 0-1.
    These are opponent-adjusted (unlike raw yard totals) so they correctly
    discount stats piled up against weak schedules.
    """
    data = {}
    for entry in sp_ratings:
        team = entry.get("team", "").lower()
        off_obj = entry.get("offense") or {}
        def_obj = entry.get("defense") or {}
        st_obj = entry.get("specialTeams") or {}
        data[team] = {
            "overall": entry.get("rating", 0) or 0,
            "off": off_obj.get("rating", 0) or 0,
            # SP+ defense: lower = better (points/plays allowed), so invert
            "def": -(def_obj.get("rating", 0) or 0),
            "st": st_obj.get("rating", 0) or 0,
        }

    def norm_field(field):
        vals = [v[field] for v in data.values()]
        mn, mx = min(vals), max(vals)
        r = mx - mn if mx > mn else 1
        return {team: (data[team][field] - mn) / r for team in data}

    n_ovr = norm_field("overall")
    n_off = norm_field("off")
    n_def = norm_field("def")
    n_st = norm_field("st")

    return {
        team: {"overall": n_ovr.get(team, 0.3), "off": n_off.get(team, 0.3),
               "def": n_def.get(team, 0.3), "st": n_st.get(team, 0.3)}
        for team in data
    }


def build_recruit_lookup(api_key, year):
    """Recruiting data for classes that feed into a given season."""
    lookup = {}
    for yr in range(year - 4, year + 1):
        print(f"    Class {yr}...")
        try:
            recruits = fetch_recruiting(api_key, yr)
            for r in recruits:
                name = r.get("name", "").lower()
                team = (r.get("committedTo") or "").lower()
                stars = r.get("stars", 0) or 0
                key = (name, team)
                if stars > lookup.get(key, 0):
                    lookup[key] = stars
        except Exception as e:
            print(f"    Warning: {yr}: {e}")
    return lookup


def capture_display_stats(pos_group, player_stats):
    """Extract human-readable stats for player detail display."""
    s = player_stats
    sf = _safe_float

    if pos_group == "QB":
        pass_yds = sf(s.get("passingYDS", s.get("passingYards", 0)))
        pass_tds = int(sf(s.get("passingTD", s.get("passingTDs", 0))))
        comp = int(sf(s.get("passingCOMPLETIONS", s.get("passingCOMP", s.get("completions", 0)))))
        att = int(sf(s.get("passingATT", s.get("passingAttempts", s.get("attempts", 0)))))
        ints = int(sf(s.get("passingINT", s.get("interceptions", 0))))
        rush_yds = sf(s.get("rushingYDS", s.get("rushingYards", 0)))
        comp_pct = round(comp / att * 100, 1) if att > 0 else 0.0
        ypa = round(pass_yds / att, 1) if att > 0 else 0.0
        return {"passYds": int(pass_yds), "passTDs": pass_tds, "comp": comp, "att": att,
                "compPct": comp_pct, "ypa": ypa, "ints": ints, "rushYds": int(rush_yds)}

    elif pos_group in ("RB", "FB"):
        rush_yds = sf(s.get("rushingYDS", s.get("rushingYards", 0)))
        rush_tds = int(sf(s.get("rushingTD", s.get("rushingTDs", 0))))
        ypc = round(sf(s.get("rushingYPC", s.get("yardsPerRushAttempt", 0))), 1)
        rec_yds = sf(s.get("receivingYDS", s.get("receivingYards", 0)))
        rec_tds = int(sf(s.get("receivingTD", s.get("receivingTDs", 0))))
        return {"rushYds": int(rush_yds), "rushTDs": rush_tds, "ypc": ypc, "recYds": int(rec_yds), "recTDs": rec_tds}

    elif pos_group in ("WR", "TE"):
        rec_yds = sf(s.get("receivingYDS", s.get("receivingYards", 0)))
        rec_tds = int(sf(s.get("receivingTD", s.get("receivingTDs", 0))))
        rec = int(sf(s.get("receivingREC", s.get("receptions", 0))))
        ypr = round(rec_yds / rec, 1) if rec > 0 else 0.0
        return {"recYds": int(rec_yds), "recTDs": rec_tds, "receptions": rec, "ypr": ypr}

    elif pos_group in ("DL", "LB", "DB"):
        tackles = round(sf(s.get("defensiveTOT", s.get("totalTackles", 0))), 1)
        sacks = round(sf(s.get("defensiveSACKS", s.get("sacks", 0))), 1)
        ints = max(
            sf(s.get("defensiveINT", s.get("interceptions", 0))),
            sf(s.get("interceptionsINT", 0)),
        )
        pds = round(sf(s.get("defensivePD", s.get("passesDeflected", 0))), 1)
        tfl = round(sf(s.get("defensiveTFL", 0)), 1)
        qbh = round(sf(s.get("defensiveQBH", s.get("defensiveQB HUR", 0))), 1)
        ff = round(sf(s.get("defensiveFF", 0)), 1)
        int_tds = round(sf(s.get("interceptionsTD", 0)), 1)
        return {"tackles": tackles, "sacks": sacks, "ints": int(ints), "pds": pds, "tfl": tfl, "qbh": qbh, "ff": ff, "intTDs": int_tds if int_tds > 0 else None}

    elif pos_group == "K":
        fgm = int(sf(s.get("kickingFGM", s.get("fieldGoalsMade", 0))))
        fga = int(sf(s.get("kickingFGA", s.get("fieldGoalAttempts", 0))))
        fg_pct = round(fgm / max(fga, 1) * 100, 1) if fga > 0 else 0
        longest = int(sf(s.get("kickingLONG", s.get("longFieldGoal", 0))))
        xpm = int(sf(s.get("kickingXPM", s.get("extraPointsMade", 0))))
        xpa = int(sf(s.get("kickingXPA", s.get("extraPointAttempts", 0))))
        return {"fgm": fgm, "fga": fga, "fgPct": fg_pct, "longFG": longest, "xpm": xpm, "xpa": xpa}

    elif pos_group == "P":
        punt_yds = sf(s.get("puntingYDS", s.get("puntYards", 0)))
        punt_no = int(sf(s.get("puntingNO", s.get("punts", 0))))
        punt_avg = round(punt_yds / max(punt_no, 1), 1) if punt_no > 0 else 0
        punt_long = int(sf(s.get("puntingLONG", s.get("longPunt", 0))))
        in20 = int(sf(s.get("puntingIN20", s.get("puntsInsideTwenty", 0))))
        return {"punts": punt_no, "puntAvg": punt_avg, "longPunt": punt_long, "in20": in20}

    return {}


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def build_team_performance_scores(team_stats_lookup):
    """Normalize team performance stats to 0-1 across all FBS teams.
    Returns dict: team_name_lower → {passOff, runOff, passDef, runDef, thirdDownOff,
    thirdDownDef, havoc} each 0-1.
    """
    teams = list(team_stats_lookup.keys())
    if not teams:
        return {}

    def safe(t, k):
        try:
            return float(team_stats_lookup[t].get(k, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def normalize_vals(vals, invert=False):
        """Normalize list to 0-1. invert=True means lower is better (defensive stats)."""
        mn, mx = min(vals), max(vals)
        r = mx - mn if mx > mn else 1
        if invert:
            return [(mx - v) / r for v in vals]
        return [(v - mn) / r for v in vals]

    pass_yds = [safe(t, "netPassingYards") for t in teams]
    rush_yds = [safe(t, "rushingYards") for t in teams]
    pass_yds_allowed = [safe(t, "netPassingYardsOpponent") for t in teams]
    rush_yds_allowed = [safe(t, "rushingYardsOpponent") for t in teams]

    # Third down: higher conversion rate = better offense; lower opponent rate = better defense
    td3_conv = [safe(t, "thirdDownConversions") for t in teams]
    td3_att  = [max(safe(t, "thirdDowns"), 1) for t in teams]
    td3_opp_conv = [safe(t, "thirdDownConversionsOpponent") for t in teams]
    td3_opp_att  = [max(safe(t, "thirdDownsOpponent"), 1) for t in teams]
    td3_off_rate = [td3_conv[i] / td3_att[i] for i in range(len(teams))]
    td3_def_rate = [td3_opp_conv[i] / td3_opp_att[i] for i in range(len(teams))]  # lower = better

    # Havoc: defensive TFL + INTs caused + fumbles recovered — measures chaos creation
    tfl   = [safe(t, "tacklesForLoss") for t in teams]
    ints  = [safe(t, "interceptions") for t in teams]
    fumr  = [safe(t, "fumblesRecovered") for t in teams]
    havoc = [tfl[i] + ints[i] * 2 + fumr[i] for i in range(len(teams))]

    pn   = normalize_vals(pass_yds)
    rn   = normalize_vals(rush_yds)
    pdn  = normalize_vals(pass_yds_allowed, invert=True)
    rdn  = normalize_vals(rush_yds_allowed, invert=True)
    t3on = normalize_vals(td3_off_rate)
    t3dn = normalize_vals(td3_def_rate, invert=True)   # lower opponent 3rd-down rate = better
    havn = normalize_vals(havoc)

    scores = {}
    for i, t in enumerate(teams):
        scores[t] = {
            "passOff":      pn[i],
            "runOff":       rn[i],
            "passDef":      pdn[i],
            "runDef":       rdn[i],
            "thirdDownOff": t3on[i],
            "thirdDownDef": t3dn[i],
            "havoc":        havn[i],
        }
    return scores


def perf_to_rating(perf_score_0_1):
    """Convert 0-1 performance score to a 50-95 rating."""
    return int(round(50 + perf_score_0_1 * 45))


def compute_team_ratings(team_id, team_name_lower, players_private, ratings_list,
                         perf_scores, sp_detail):
    """Compute team category ratings.

    Three-way blend:
      40% player ratings (individual talent signal)
      35% SP+ sub-ratings (opponent-adjusted performance — corrects for weak schedules)
      25% raw stats performance (directional confirmation of SP+)

    SP+ is the dominant external signal because it's opponent-adjusted, preventing
    mid-major teams with big raw numbers against weak competition from being over-rated.
    """
    rmap = {r["playerId"]: r for r in ratings_list}
    team_players = [p for p in players_private if p["teamId"] == team_id]
    if not team_players:
        return {"overall": 50, "passOff": 50, "runOff": 50, "passDef": 50, "runDef": 50, "specialTeams": 50}

    def avg_top(players, n=5):
        vals = sorted([rmap[p["id"]]["overall"] for p in players if p["id"] in rmap], reverse=True)
        return int(round(sum(vals[:n]) / max(len(vals[:n]), 1))) if vals else 50

    qbs = [p for p in team_players if p["positionGroup"] == "QB"]
    rbs = [p for p in team_players if p["positionGroup"] in ("RB", "FB")]
    wrs = [p for p in team_players if p["positionGroup"] in ("WR", "TE")]
    ols = [p for p in team_players if p["positionGroup"] == "OL"]
    dls = [p for p in team_players if p["positionGroup"] == "DL"]
    lbs = [p for p in team_players if p["positionGroup"] == "LB"]
    dbs = [p for p in team_players if p["positionGroup"] == "DB"]
    ks =  [p for p in team_players if p["positionGroup"] in ("K", "P")]

    qb_r  = avg_top(qbs, 2)
    wr_r  = avg_top(wrs, 5)
    ol_r  = avg_top(ols, 5)
    rb_r  = avg_top(rbs, 3)
    dl_r  = avg_top(dls, 4)
    lb_r  = avg_top(lbs, 4)
    db_r  = avg_top(dbs, 5)
    k_r   = avg_top(ks, 2)

    # ── Player-based category scores ──────────────────────────────────────
    p_pass_off = qb_r * 0.45 + wr_r * 0.35 + ol_r * 0.20
    p_run_off  = rb_r * 0.40 + ol_r * 0.40 + qb_r * 0.10 + wr_r * 0.10
    p_pass_def = db_r * 0.45 + lb_r * 0.30 + dl_r * 0.25
    p_run_def  = dl_r * 0.40 + lb_r * 0.35 + db_r * 0.25

    # ── SP+ opponent-adjusted scores → 50-95 ─────────────────────────────
    sp = sp_detail.get(team_name_lower, {})
    sp_off_rating = perf_to_rating(sp.get("off", 0.3))   # overall SP+ offense
    sp_def_rating = perf_to_rating(sp.get("def", 0.3))   # opponent-adj defense

    # ── Raw performance scores (raw team stats, NOT opponent-adjusted) ────
    perf = perf_scores.get(team_name_lower, {})
    raw_pass_off  = perf_to_rating(perf.get("passOff",      0.3))
    raw_run_off   = perf_to_rating(perf.get("runOff",       0.3))
    raw_pass_def  = perf_to_rating(perf.get("passDef",      0.3))
    raw_run_def   = perf_to_rating(perf.get("runDef",       0.3))
    raw_3rd_off   = perf_to_rating(perf.get("thirdDownOff", 0.3))
    raw_3rd_def   = perf_to_rating(perf.get("thirdDownDef", 0.3))
    raw_havoc     = perf_to_rating(perf.get("havoc",        0.3))

    # ── Three-way blend (advanced stats blend into raw component) ─────────
    # Advanced stats (3rd down, havoc) are blended into the raw stats component.
    # They're correlated with raw yards but measure execution quality more precisely.
    PLAYER = 0.30
    SP_W   = 0.45   # SP+ is opponent-adjusted — most reliable for cross-conference comparison
    RAW    = 0.25   # Raw stats provide within-conference directional signal

    # Blend advanced stats into raw component: 60% yardage, 40% 3rd down/havoc
    adv_pass_off = int(round(raw_pass_off * 0.6 + raw_3rd_off * 0.4))
    adv_run_off  = int(round(raw_run_off  * 0.6 + raw_3rd_off * 0.4))
    adv_pass_def = int(round(raw_pass_def * 0.6 + raw_3rd_def * 0.2 + raw_havoc * 0.2))
    adv_run_def  = int(round(raw_run_def  * 0.6 + raw_3rd_def * 0.2 + raw_havoc * 0.2))

    pass_off = int(round(p_pass_off * PLAYER + sp_off_rating * SP_W + adv_pass_off * RAW))
    run_off  = int(round(p_run_off  * PLAYER + sp_off_rating * SP_W + adv_run_off  * RAW))
    pass_def = int(round(p_pass_def * PLAYER + sp_def_rating * SP_W + adv_pass_def * RAW))
    run_def  = int(round(p_run_def  * PLAYER + sp_def_rating * SP_W + adv_run_def  * RAW))
    special  = k_r

    overall = int(round(pass_off * 0.25 + run_off * 0.25 + pass_def * 0.25 + run_def * 0.20 + special * 0.05))
    return {
        "overall": overall, "passOff": pass_off, "runOff": run_off,
        "passDef": pass_def, "runDef": run_def, "specialTeams": special,
    }


def normalize_team_ratings(teams_private):
    """Apply a percentile curve to team ratings to spread the distribution.

    Target distribution (~130 FBS teams):
      Top 1%  (~1-2 teams)  → 94-98  (national title contenders)
      Top 10% (~13 teams)   → 88-93  (playoff-caliber)
      Top 25% (~33 teams)   → 80-87  (bowl-game quality)
      Median  (~65 teams)   → 74-75
      Bottom 25%            → 65-70
      Bottom                → 58-64

    Applied to every rating field (overall, passOff, runOff, passDef, runDef, specialTeams).
    """
    fields = ["overall", "passOff", "runOff", "passDef", "runDef", "specialTeams"]

    def curve(rank):
        """rank = 0.0 (worst) to 1.0 (best).

        Target (~133 FBS teams):
          Top ~5 teams  (rank > 0.962) → 91-94
          Next ~10 teams (0.887-0.962) → 87-91
          Bowl-caliber  (0.75-0.887)   → 85-87
          Middle        (0.50-0.75)    → 79-85
          Below avg     (0.25-0.50)    → 75-79
          Bottom        (0.00-0.05)    → 58-64
          97-99: reserved for teams with SP+ top-5 offense AND defense (separate boost)
        """
        if rank <= 0.05:
            return 58 + rank / 0.05 * 6             # 58-64
        elif rank <= 0.25:
            return 64 + (rank - 0.05) / 0.20 * 11   # 64-75
        elif rank <= 0.50:
            return 75 + (rank - 0.25) / 0.25 * 4    # 75-79
        elif rank <= 0.75:
            return 79 + (rank - 0.50) / 0.25 * 6    # 79-85
        elif rank <= 0.887:
            return 85 + (rank - 0.75) / 0.137 * 2   # 85-87 (top ~15 teams)
        elif rank <= 0.962:
            return 87 + (rank - 0.887) / 0.075 * 4  # 87-91 (top ~5-15 teams)
        else:
            return 91 + (rank - 0.962) / 0.038 * 3  # 91-94 (top ~5 teams)

    for field in fields:
        vals = [(i, t["ratings"].get(field, 70)) for i, t in enumerate(teams_private)]
        sorted_vals = sorted(vals, key=lambda x: x[1])
        n = len(sorted_vals)
        for rank_idx, (team_idx, raw_val) in enumerate(sorted_vals):
            rank = (rank_idx + 0.5) / n
            teams_private[team_idx]["ratings"][field] = max(58, min(99, int(round(curve(rank)))))

    return teams_private


def process_year(api_key, year, team_name_map, draft_data=None, prior_player_ids=None, prior_player_teams=None):
    """Process a single year and return all data dicts."""
    print(f"\n{'='*60}")
    print(f"  PROCESSING YEAR {year}")
    print(f"{'='*60}")

    print(f"\n[1/7] Fetching FBS teams...")
    teams_raw = fetch_teams(api_key, year)
    team_names = [t["school"] for t in teams_raw]
    print(f"  Found {len(team_names)} teams")

    print(f"\n[2/7] Fetching rosters...")
    rosters = fetch_all_rosters(api_key, team_names, year)

    import time as _time

    print(f"\n[3/7] Fetching player stats...")
    _time.sleep(1)
    player_stats_raw = fetch_player_stats(api_key, year)
    stat_lookup = build_stat_lookup(player_stats_raw)
    print(f"  {len(stat_lookup)} player stat entries")

    print(f"\n[3b] Fetching player usage...")
    _time.sleep(0.5)
    usage_raw = fetch_player_usage(api_key, year)
    usage_lookup = build_usage_lookup(usage_raw)
    print(f"  {len(usage_lookup)} usage entries")

    print(f"\n[3c] Fetching player awards...")
    _time.sleep(0.5)
    awards_raw = fetch_awards(api_key, year)
    awards_lookup = build_awards_lookup(awards_raw)
    print(f"  {len(awards_lookup)} award entries")

    draft_lookup = build_draft_lookup(draft_data or [], year)
    print(f"  {len(draft_lookup)} OL draft picks mapped for {year+1} NFL Draft")

    print(f"\n[4/7] Fetching PPA data...")
    _time.sleep(1)
    ppa_raw = fetch_ppa(api_key, year)
    ppa_lookup = build_ppa_lookup(ppa_raw)
    print(f"  {len(ppa_lookup)} PPA entries")

    print(f"\n[5/7] Fetching team stats + SP+ + talent...")
    _time.sleep(1)
    team_stats_raw = fetch_team_stats(api_key, year)
    _time.sleep(0.5)
    team_stats_lookup = build_team_stats_lookup(team_stats_raw)
    sp_ratings = fetch_sp_ratings(api_key, year)
    _time.sleep(0.5)
    talent_data = fetch_talent(api_key, year)
    team_quality = build_team_quality(sp_ratings, talent_data)
    sp_detail = build_sp_detail(sp_ratings)

    print(f"\n[6/7] Fetching recruiting data...")
    recruit_lookup = build_recruit_lookup(api_key, year)

    print(f"\n[7/7] Processing ratings...")
    teams_private = []
    players_private = []
    raw_ratings_all = {}
    ol_boost_signals = {}  # pid -> {draft_round, award_tier, returning}

    for team in teams_raw:
        school = team["school"]
        team_id = team.get("id", hash(school))
        roster = rosters.get(school, [])
        tq = team_quality.get(school.lower(), 0.3)

        teams_private.append({
            "id": team_id,
            "name": school,
            "abbreviation": team.get("abbreviation", ""),
            "mascot": team.get("mascot", ""),
            "conference": team.get("conference", ""),
            "primaryColor": f"#{team.get('color', '333333')}",
            "secondaryColor": f"#{team.get('alt_color', '666666')}",
            "logoUrl": (team.get("logos") or [""])[0] if team.get("logos") else "",
            "stadiumName": team.get("location", {}).get("name", "") if team.get("location") else "",
            "city": team.get("location", {}).get("city", "") if team.get("location") else "",
            "state": team.get("location", {}).get("state", "") if team.get("location") else "",
            "capacity": team.get("location", {}).get("capacity", 0) if team.get("location") else 0,
        })

        t_stats = team_stats_lookup.get(school.lower(), {})

        for p in roster:
            pid = p.get("id", 0)
            first = p.get("firstName", "")
            last = p.get("lastName", "")
            pos = p.get("position", "ATH") or "ATH"
            pos_group = get_position_group(pos)

            p_stats = find_player_stats(stat_lookup, first, last, school)
            ppa_val = find_player_ppa(ppa_lookup, first, last, school)
            full_lower = f"{first} {last}".lower()
            stars = recruit_lookup.get((full_lower, school.lower()), 0)

            usage = usage_lookup.get(pid, {})
            raw = compute_raw_ratings(pid, pos_group, p_stats, ppa_val, t_stats, tq, stars, usage)
            raw_ratings_all[pid] = {"pos": pos_group, "raw": raw, "stats": capture_display_stats(pos_group, p_stats)}

            # Collect OL boost signals: draft, awards, cross-year continuity
            if pos_group == "OL":
                draft_round = draft_lookup.get(full_lower, 0)
                award_tier = awards_lookup.get((full_lower, school.lower()), 0)
                # Continuity floor only applies to same-team returners.
                # Transfers are in a new system and their prior performance is uncertain.
                prior_team = prior_player_teams.get(pid) if prior_player_teams else None
                returning_same_team = bool(
                    prior_player_ids and pid in prior_player_ids
                    and (prior_team is None or prior_team == school)
                )
                if draft_round or award_tier or returning_same_team:
                    ol_boost_signals[pid] = {
                        "draft_round": draft_round,
                        "award_tier": award_tier,
                        "returning": returning_same_team,
                    }

            players_private.append({
                "id": pid,
                "teamId": team_id,
                "firstName": first,
                "lastName": last,
                "position": pos,
                "positionGroup": pos_group,
                "jersey": p.get("jersey", ""),
                "year": p.get("year", 0),
                "height": p.get("height", ""),
                "weight": p.get("weight", 0),
            })

    print("Normalizing ratings...")
    normalized = normalize_all_ratings(raw_ratings_all)

    # ── OL boost: apply floor ratings driven by draft status, awards, and continuity ──
    # These three signals are the best proxies we have for OL quality with no individual stats.
    # Applied as floors (never lower a player's rating, only raise it).
    OL_DRAFT_FLOORS = {1: 88, 2: 82, 3: 77, 4: 73, 5: 71, 6: 69, 7: 67}
    OL_AWARD_FLOORS = {3: 85, 2: 78, 1: 73}   # All-American=3, 1st-conf=2, 2nd/HM=1
    OL_RETURNING_FLOOR = 68                    # Any multi-year returner

    boosted_count = 0
    for pid, signals in ol_boost_signals.items():
        attrs = normalized.get(pid)
        if not attrs:
            continue
        floor = 0
        if signals["draft_round"]:
            floor = max(floor, OL_DRAFT_FLOORS.get(signals["draft_round"], 67))
        if signals["award_tier"]:
            floor = max(floor, OL_AWARD_FLOORS.get(signals["award_tier"], 73))
        if signals["returning"]:
            floor = max(floor, OL_RETURNING_FLOOR)
        if floor > 0:
            changed = False
            for skill in ("runBlock", "passBlock"):
                if skill in attrs and attrs[skill] < floor:
                    attrs[skill] = floor
                    changed = True
            if changed:
                boosted_count += 1
    if boosted_count:
        print(f"  OL boosts applied to {boosted_count} player(s) (draft/awards/continuity)")

    ratings = []
    for p in players_private:
        pid = p["id"]
        attrs = normalized.get(pid, {a: 55 for a in SKILL_ATTRS.get(p["positionGroup"], ["runBlock", "passBlock"])})
        ovr = compute_overall(attrs, p["positionGroup"])
        display_stats = raw_ratings_all.get(pid, {}).get("stats", {})
        ratings.append({"playerId": pid, "overall": ovr, **attrs, "stats": display_stats})

    # Team ratings: compute then normalize distribution
    print("Computing team ratings...")
    perf_scores = build_team_performance_scores(team_stats_lookup)
    for t in teams_private:
        t["ratings"] = compute_team_ratings(t["id"], t["name"].lower(), players_private, ratings, perf_scores, sp_detail)
    normalize_team_ratings(teams_private)

    # Public versions
    print("Generating public names...")
    teams_public = []
    for t in teams_private:
        mapping = team_name_map.get(t["name"], {})
        teams_public.append({
            **t,
            "name": mapping.get("publicName", f"{t['name']} University"),
            "abbreviation": mapping.get("publicAbbreviation", t["abbreviation"]),
            "mascot": mapping.get("publicMascot", t["mascot"]),
            "stadiumName": mapping.get("publicStadiumName", t["stadiumName"]),
        })

    players_public = []
    used_per_team = {}
    for p in players_private:
        tid = p["teamId"]
        if tid not in used_per_team:
            used_per_team[tid] = set()
        first, last = generate_public_name(p["id"], used_per_team[tid])
        players_public.append({**p, "firstName": first, "lastName": last})

    # Rating distribution
    ovrs = [r["overall"] for r in ratings]
    from collections import Counter
    buckets = Counter((v // 10) * 10 for v in ovrs)
    print(f"\n  Year {year} rating distribution:")
    for b in sorted(buckets):
        pct = buckets[b] * 100 // len(ovrs)
        bar = "#" * (pct // 2)
        print(f"    {b:2d}-{b+9}: {buckets[b]:5d} ({pct:2d}%) {bar}")
    print(f"    Min={min(ovrs)} Max={max(ovrs)} Median={sorted(ovrs)[len(ovrs)//2]}")
    print(f"  {len(teams_private)} teams, {len(players_private)} players.")

    return {
        "teams_private": teams_private,
        "teams_public": teams_public,
        "players_private": players_private,
        "players_public": players_public,
        "ratings": ratings,
    }


def write_year(year, data, output_dir):
    """Write one year's data to disk."""
    year_dir = os.path.join(output_dir, str(year))
    os.makedirs(year_dir, exist_ok=True)
    for filename, key in [
        ("teams_private.json", "teams_private"),
        ("teams_public.json", "teams_public"),
        ("players_private.json", "players_private"),
        ("players_public.json", "players_public"),
        ("ratings.json", "ratings"),
    ]:
        path = os.path.join(year_dir, filename)
        with open(path, "w") as f:
            json.dump(data[key], f, indent=2)
        print(f"  {year}/{filename}: {len(data[key])} entries")


def main():
    print("=== CFB Data Pipeline (Multi-Year) ===")
    api_key = load_api_key()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(script_dir, "team_name_map.json"), "r") as f:
        team_name_map = json.load(f)

    # Accept optional year argument: python fetch_and_rate.py 2023
    if len(sys.argv) > 1:
        years_to_run = [int(y) for y in sys.argv[1:]]
    else:
        years_to_run = YEARS

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load NFL draft data once — used across all years
    draft_path = os.path.join(script_dir, "nfl_draft.json")
    try:
        with open(draft_path) as f:
            draft_data = [e for e in json.load(f) if not str(e.get("nfl_year", "")).startswith("_")]
        print(f"  Loaded {len(draft_data)} NFL draft entries from nfl_draft.json")
    except FileNotFoundError:
        draft_data = []

    for year in years_to_run:
        # Cross-year continuity: load prior year's player IDs and team mapping
        prior_player_ids = set()
        prior_player_teams = {}  # pid -> school name (to detect transfers)
        prior_year_path = os.path.join(OUTPUT_DIR, str(year - 1), "players_private.json")
        prior_teams_path = os.path.join(OUTPUT_DIR, str(year - 1), "teams_private.json")
        if os.path.exists(prior_year_path):
            with open(prior_year_path) as f:
                prior_players = json.load(f)
            prior_player_ids = {p["id"] for p in prior_players}
            # Build team_id → school name from prior year's teams file
            prior_team_id_to_name = {}
            if os.path.exists(prior_teams_path):
                with open(prior_teams_path) as tf:
                    for t in json.load(tf):
                        prior_team_id_to_name[t["id"]] = t["name"]
            prior_player_teams = {p["id"]: prior_team_id_to_name.get(p["teamId"], "") for p in prior_players}
            print(f"  Loaded {len(prior_player_ids)} player IDs from {year-1} for continuity check")

        data = process_year(api_key, year, team_name_map, draft_data=draft_data,
                            prior_player_ids=prior_player_ids, prior_player_teams=prior_player_teams)
        print(f"\nWriting to {os.path.abspath(OUTPUT_DIR)}/")
        write_year(year, data, OUTPUT_DIR)

    # Update years index with all available years
    existing_years = set()
    for d in os.listdir(OUTPUT_DIR):
        if d.isdigit() and os.path.isdir(os.path.join(OUTPUT_DIR, d)):
            existing_years.add(int(d))
    all_years = sorted(existing_years)
    with open(os.path.join(OUTPUT_DIR, "years.json"), "w") as f:
        json.dump(all_years, f)

    print(f"\nDone! Processed {len(years_to_run)} season(s). Available: {all_years}")


if __name__ == "__main__":
    main()
