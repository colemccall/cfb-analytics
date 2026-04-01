"""CFB Data API client — fetches teams, rosters, stats, and PPA data.
Includes file-based caching so re-runs don't re-fetch."""

import os
import json
import time
import hashlib
import requests
from dotenv import load_dotenv

BASE_URL = "https://api.collegefootballdata.com"
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".cache")


def load_api_key():
    load_dotenv()
    key = os.getenv("CFB_API_KEY")
    if not key:
        raise RuntimeError("CFB_API_KEY not found in .env")
    return key


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _cache_key(path, params):
    key_str = path + json.dumps(params or {}, sort_keys=True)
    return hashlib.md5(key_str.encode()).hexdigest()


def _get(api_key, path, params=None, retries=5):
    # Check cache first
    os.makedirs(CACHE_DIR, exist_ok=True)
    ck = _cache_key(path, params)
    cache_file = os.path.join(CACHE_DIR, f"{ck}.json")
    if os.path.exists(cache_file):
        print(f"    [cache] {path} {params or ''}")
        with open(cache_file, "r") as f:
            return json.load(f)

    print(f"    GET {path} {params or ''}")
    for attempt in range(retries):
        resp = requests.get(f"{BASE_URL}{path}", headers=_headers(api_key), params=params)
        if resp.status_code == 429:
            wait = 5 * (2 ** attempt)  # 5, 10, 20, 40, 80s
            print(f"    Rate limited, waiting {wait}s... (attempt {attempt+1}/{retries})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        n = len(data) if isinstance(data, list) else "ok"
        print(f"    -> {n} records, cached.")
        with open(cache_file, "w") as f:
            json.dump(data, f)
        return data
    raise RuntimeError(f"Still rate-limited after {retries} retries: {path}")


def fetch_teams(api_key, year):
    return _get(api_key, "/teams/fbs", {"year": year})


def fetch_roster(api_key, team_name, year):
    return _get(api_key, "/roster", {"team": team_name, "year": year})


def fetch_all_rosters(api_key, team_names, year):
    rosters = {}
    for i, name in enumerate(team_names):
        print(f"  Fetching roster {i+1}/{len(team_names)}: {name}")
        try:
            rosters[name] = fetch_roster(api_key, name, year)
        except requests.HTTPError as e:
            print(f"    WARNING: Failed to fetch {name}: {e}")
            rosters[name] = []
        time.sleep(0.75)
    return rosters


def fetch_player_stats(api_key, year):
    return _get(api_key, "/stats/player/season", {
        "year": year, "seasonType": "regular",
    })


def fetch_ppa(api_key, year):
    return _get(api_key, "/ppa/players/season", {"year": year})


def fetch_team_stats(api_key, year):
    return _get(api_key, "/stats/season", {"year": year})


def fetch_sp_ratings(api_key, year):
    return _get(api_key, "/ratings/sp", {"year": year})


def fetch_talent(api_key, year):
    return _get(api_key, "/talent", {"year": year})


def fetch_recruiting(api_key, year):
    return _get(api_key, "/recruiting/players", {"year": year})


def fetch_player_usage(api_key, year):
    return _get(api_key, "/player/usage", {"year": year, "seasonType": "regular"})


def _fetch_safe(api_key, path, params):
    """Call _get() and return [] on any error or non-list response."""
    try:
        result = _get(api_key, path, params)
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"    WARNING: {path} {params} failed: {e}")
        return []


def fetch_awards(api_key, year):
    return _fetch_safe(api_key, "/awards", {"year": year})


def fetch_games(api_key, year):
    return _fetch_safe(api_key, "/games", {"year": year, "seasonType": "regular"})


def fetch_game_player_stats_for_team(api_key, team, year):
    """Fetch per-game box score stats for one team. API requires team param."""
    return _fetch_safe(api_key, "/games/players", {
        "year": year, "seasonType": "regular", "team": team
    })


def fetch_all_game_player_stats(api_key, team_names, year):
    """Fetch game player stats for all teams; deduplicates by gameId."""
    seen_game_ids = set()
    results = []
    for i, team in enumerate(team_names):
        print(f"  Fetching game stats {i+1}/{len(team_names)}: {team}")
        entries = fetch_game_player_stats_for_team(api_key, team, year)
        for entry in entries:
            gid = entry.get("id")
            if gid not in seen_game_ids:
                seen_game_ids.add(gid)
                results.append(entry)
        time.sleep(0.5)
    return results


def fetch_drives_for_team(api_key, team, year):
    """Fetch all drives for one team across weeks 1-16."""
    all_drives = []
    for week in range(1, 17):
        drives = _fetch_safe(api_key, "/drives", {
            "year": year, "week": week, "team": team, "seasonType": "regular"
        })
        all_drives.extend(drives)
    return all_drives


def fetch_plays_for_team(api_key, team, year):
    """Fetch all plays for one team across weeks 1-16."""
    all_plays = []
    for week in range(1, 17):
        plays = _fetch_safe(api_key, "/plays", {
            "year": year, "week": week, "team": team, "seasonType": "regular"
        })
        all_plays.extend(plays)
    return all_plays


def fetch_all_drives(api_key, team_names, year):
    """Fetch drives for all teams; returns dict keyed by team name."""
    all_drives = {}
    for i, team in enumerate(team_names):
        print(f"  Fetching drives {i+1}/{len(team_names)}: {team}")
        all_drives[team] = fetch_drives_for_team(api_key, team, year)
        time.sleep(0.5)
    return all_drives


def fetch_all_plays(api_key, team_names, year):
    """Fetch plays for all teams; returns dict keyed by team name."""
    all_plays = {}
    for i, team in enumerate(team_names):
        print(f"  Fetching plays {i+1}/{len(team_names)}: {team}")
        all_plays[team] = fetch_plays_for_team(api_key, team, year)
        time.sleep(0.5)
    return all_plays
