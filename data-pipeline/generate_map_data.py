"""
generate_map_data.py — Generate per-year map data for the CFB app.

For each year in YEARS:
  - recruits_{year}.json  : recruiting class that year (hometown lat/lon)
  - transfers_{year}.json : transfer portal entries that cycle (origin hometown or school coords)

Uses the existing .cache system from api_client.py so no data is re-fetched.
Transfer portal data is only available from the API starting ~2019; earlier years
will have empty or partial transfer files.

Usage:
    cd data-pipeline
    python generate_map_data.py
"""

import json
import os
import re
import hashlib
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

# ── CONFIG ────────────────────────────────────────────────────────────────────
YEARS = list(range(2012, 2027))
# Transfer portal era: API has data from 2019 onward (sparse before 2020)
TRANSFER_YEARS = list(range(2019, 2027))

CACHE_DIR = Path(__file__).parent / ".cache"
OUT_DIR   = Path(__file__).parent.parent / "app" / "assets" / "data" / "map"
BASE_URL  = "https://api.collegefootballdata.com"


# ── CACHE HELPERS (mirrors api_client.py) ─────────────────────────────────────
def _cache_key(path, params):
    key_str = path + json.dumps(params or {}, sort_keys=True)
    return hashlib.md5(key_str.encode()).hexdigest()


def _load_cache(path, params):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = CACHE_DIR / f"{_cache_key(path, params)}.json"
    if cf.exists():
        with open(cf) as f:
            return json.load(f)
    return None


def _save_cache(path, params, data):
    cf = CACHE_DIR / f"{_cache_key(path, params)}.json"
    with open(cf, "w") as f:
        json.dump(data, f)


def _api_get(headers, path, params, retries=5):
    """Fetch from API with caching + retry logic."""
    cached = _load_cache(path, params)
    if cached is not None:
        print(f"    [cache] {path} {params}", flush=True)
        return cached

    print(f"    GET {path} {params}", flush=True)
    for attempt in range(retries):
        resp = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=30)
        if resp.status_code == 429:
            wait = 5 * (2 ** attempt)
            print(f"    Rate limited, waiting {wait}s...", flush=True)
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            print(f"    404 — no data for {path} {params}", flush=True)
            _save_cache(path, params, [])
            return []
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            data = []
        print(f"    -> {len(data)} records, cached.", flush=True)
        _save_cache(path, params, data)
        time.sleep(0.3)
        return data
    raise RuntimeError(f"Still rate-limited after {retries} retries: {path}")


# ── NORMALIZER ────────────────────────────────────────────────────────────────
def normalize_name(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"\b(jr\.?|sr\.?|ii|iii|iv)\b", "", name)
    name = re.sub(r"[^a-z\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


# ── SCHOOL COORDS ─────────────────────────────────────────────────────────────
def build_school_coords(headers) -> dict:
    """Build name -> (lat, lon) from /teams endpoint (not year-specific)."""
    raw = _api_get(headers, "/teams", {})
    coords = {}
    for t in raw:
        name = t.get("school")
        loc  = t.get("location") or {}
        lat  = loc.get("latitude")
        lon  = loc.get("longitude")
        if name and lat and lon:
            coords[name] = (float(lat), float(lon))
    print(f"  School coords: {len(coords)} schools", flush=True)
    return coords


# ── HOMETOWN LOOKUP ───────────────────────────────────────────────────────────
def build_hometown_lookup(headers, years) -> dict:
    """
    Build player name -> hometown lat/lon from historical recruiting data.
    Uses all available cached recruit years as the lookup pool.
    """
    print(f"Building hometown lookup from {years[0]}-{years[-1]}...", flush=True)
    lookup = {}
    for year in years:
        raw = _api_get(headers, "/recruiting/players", {"year": year})
        for r in raw:
            hometown = r.get("hometownInfo") or {}
            lat = hometown.get("latitude") or r.get("latitude")
            lon = hometown.get("longitude") or r.get("longitude")
            if lat is None or lon is None:
                continue
            name = r.get("name") or ""
            if not name:
                continue
            key    = normalize_name(name)
            rating = r.get("rating") or 0.0
            if key not in lookup or rating > lookup[key].get("_rating", 0):
                lookup[key] = {
                    "lat":     float(lat),
                    "lon":     float(lon),
                    "city":    r.get("city") or "",
                    "state":   r.get("stateProvince") or "",
                    "_rating": rating,
                }
    print(f"  Lookup: {len(lookup)} unique player names", flush=True)
    return lookup


# ── PER-YEAR RECRUITS ─────────────────────────────────────────────────────────
def build_recruits_for_year(headers, year) -> list:
    raw = _api_get(headers, "/recruiting/players", {"year": year})
    out = []
    for r in raw:
        hometown = r.get("hometownInfo") or {}
        lat = hometown.get("latitude") or r.get("latitude")
        lon = hometown.get("longitude") or r.get("longitude")
        if lat is None or lon is None:
            continue  # skip records with no coords
        out.append({
            "name":        r.get("name") or "",
            "pos":         r.get("position") or "",
            "stars":       r.get("stars") or 0,
            "rating":      round(r.get("rating") or 0.0, 4),
            "committedTo": r.get("committedTo") or "",
            "year":        year,
            "city":        r.get("city") or "",
            "state":       r.get("stateProvince") or "",
            "lat":         float(lat),
            "lon":         float(lon),
            "type":        "recruit",
        })
    return out


# ── PER-YEAR TRANSFERS ────────────────────────────────────────────────────────
def build_transfers_for_year(headers, year, hometown_lookup, school_coords) -> list:
    raw = _api_get(headers, "/player/portal", {"year": year})
    if not raw:
        return []

    matched = fallback = no_coords = 0
    out = []
    for t in raw:
        origin    = t.get("origin") or ""
        dest      = t.get("destination") or ""
        full_name = f"{t.get('firstName', '')} {t.get('lastName', '')}".strip()

        key      = normalize_name(full_name)
        hometown = hometown_lookup.get(key)

        if hometown:
            lat   = hometown["lat"]
            lon   = hometown["lon"]
            city  = hometown["city"]
            state = hometown["state"]
            matched += 1
        else:
            coords = school_coords.get(origin) or school_coords.get(dest)
            if coords:
                lat, lon = coords
                fallback += 1
            else:
                lat = lon = None
                no_coords += 1
            city = state = ""

        if lat is None:
            continue  # skip unplaceable transfers

        out.append({
            "name":   full_name,
            "pos":    t.get("position") or "",
            "stars":  t.get("stars") or 0,
            "rating": round(t.get("rating") or 0.0, 4),
            "origin": origin,
            "dest":   dest,
            "date":   (t.get("transferDate") or "")[:10],
            "lat":    lat,
            "lon":    lon,
            "city":   city,
            "state":  state,
            "type":   "transfer",
        })

    print(f"    Hometown match: {matched} | School fallback: {fallback} | No coords: {no_coords}", flush=True)
    return out


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    load_dotenv()
    api_key = os.getenv("CFB_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: Set CFB_API_KEY in .env")

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Build shared lookups once
    school_coords   = build_school_coords(headers)
    hometown_lookup = build_hometown_lookup(headers, YEARS)

    for year in YEARS:
        print(f"\n-- Year {year} --", flush=True)

        # Recruits
        print(f"  Building recruits_{year}.json...", flush=True)
        recruits = build_recruits_for_year(headers, year)
        out_path = OUT_DIR / f"recruits_{year}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(recruits, f, separators=(",", ":"))
        print(f"  Saved {len(recruits)} recruits -> {out_path.name}", flush=True)

        # Transfers (only for portal era years)
        if year in TRANSFER_YEARS:
            print(f"  Building transfers_{year}.json...", flush=True)
            transfers = build_transfers_for_year(headers, year, hometown_lookup, school_coords)
            out_path  = OUT_DIR / f"transfers_{year}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(transfers, f, separators=(",", ":"))
            print(f"  Saved {len(transfers)} transfers -> {out_path.name}", flush=True)
        else:
            # Write empty file for years before portal era so app doesn't 404
            out_path = OUT_DIR / f"transfers_{year}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            print(f"  transfers_{year}.json: empty (pre-portal era)", flush=True)

    print("\nDone! All map data generated.", flush=True)


if __name__ == "__main__":
    main()
