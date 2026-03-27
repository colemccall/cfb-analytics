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
        with open(cache_file, "r") as f:
            return json.load(f)

    for attempt in range(retries):
        resp = requests.get(f"{BASE_URL}{path}", headers=_headers(api_key), params=params)
        if resp.status_code == 429:
            wait = 15 * (2 ** attempt)  # 15, 30, 60, 120, 240s
            print(f"    Rate limited, waiting {wait}s... (attempt {attempt+1}/{retries})")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        # Cache the result
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
