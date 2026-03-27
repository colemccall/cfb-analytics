"""CFB Data Pipeline — multi-year, fetches data, rates players, writes JSON."""

import os
import json
import hashlib
import sys

from api_client import (
    load_api_key, fetch_teams, fetch_all_rosters, fetch_player_stats,
    fetch_ppa, fetch_team_stats, fetch_sp_ratings, fetch_talent, fetch_recruiting,
)
from rating_engine import get_position_group, compute_raw_ratings, normalize_all_ratings, compute_overall

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "assets", "data")
YEARS = [2022, 2023, 2024]

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


def compute_team_ratings(team_id, players_private, ratings_list):
    """Compute team category ratings: overall, passOff, runOff, passDef, runDef, specialTeams."""
    rmap = {r["playerId"]: r for r in ratings_list}
    team_players = [p for p in players_private if p["teamId"] == team_id]
    if not team_players:
        return {"overall": 50, "passOff": 50, "runOff": 50, "passDef": 50, "runDef": 50, "specialTeams": 50}

    def avg_top(players, n=8):
        vals = sorted([rmap[p["id"]]["overall"] for p in players if p["id"] in rmap], reverse=True)
        return int(round(sum(vals[:n]) / max(len(vals[:n]), 1))) if vals else 50

    qbs = [p for p in team_players if p["positionGroup"] == "QB"]
    rbs = [p for p in team_players if p["positionGroup"] in ("RB", "FB")]
    wrs = [p for p in team_players if p["positionGroup"] in ("WR", "TE")]
    ols = [p for p in team_players if p["positionGroup"] == "OL"]
    dls = [p for p in team_players if p["positionGroup"] == "DL"]
    lbs = [p for p in team_players if p["positionGroup"] == "LB"]
    dbs = [p for p in team_players if p["positionGroup"] == "DB"]
    ks = [p for p in team_players if p["positionGroup"] in ("K", "P")]

    qb_r = avg_top(qbs, 2)
    wr_r = avg_top(wrs, 5)
    ol_r = avg_top(ols, 5)
    rb_r = avg_top(rbs, 3)
    dl_r = avg_top(dls, 4)
    lb_r = avg_top(lbs, 4)
    db_r = avg_top(dbs, 5)
    k_r = avg_top(ks, 2)

    pass_off = int(round(qb_r * 0.45 + wr_r * 0.35 + ol_r * 0.20))
    run_off = int(round(rb_r * 0.40 + ol_r * 0.40 + qb_r * 0.10 + wr_r * 0.10))
    pass_def = int(round(db_r * 0.45 + dl_r * 0.25 + lb_r * 0.30))
    run_def = int(round(dl_r * 0.40 + lb_r * 0.35 + db_r * 0.25))
    special = k_r
    overall = int(round(pass_off * 0.25 + run_off * 0.25 + pass_def * 0.25 + run_def * 0.20 + special * 0.05))

    return {
        "overall": overall, "passOff": pass_off, "runOff": run_off,
        "passDef": pass_def, "runDef": run_def, "specialTeams": special,
    }


def process_year(api_key, year, team_name_map):
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

    print(f"\n[6/7] Fetching recruiting data...")
    recruit_lookup = build_recruit_lookup(api_key, year)

    print(f"\n[7/7] Processing ratings...")
    teams_private = []
    players_private = []
    raw_ratings_all = {}

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

            raw = compute_raw_ratings(pid, pos_group, p_stats, ppa_val, t_stats, tq, stars)
            raw_ratings_all[pid] = {"pos": pos_group, "raw": raw}

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

    ratings = []
    for p in players_private:
        pid = p["id"]
        attrs = normalized.get(pid, {a: 55 for a in ["speed","strength","agility","awareness","throwing","catching","carrying","blocking","tackling","kickPower"]})
        ovr = compute_overall(attrs, p["positionGroup"])
        ratings.append({"playerId": pid, "overall": ovr, **attrs})

    # Team ratings
    print("Computing team ratings...")
    for t in teams_private:
        t["ratings"] = compute_team_ratings(t["id"], players_private, ratings)

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

    for year in years_to_run:
        data = process_year(api_key, year, team_name_map)
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
