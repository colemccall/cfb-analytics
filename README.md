# CFB Analytics

A single-page college football analytics and dynasty simulation app — no backend required. Browse team rosters, player ratings, schedules, drive charts, and play-by-play data for every FBS season from 2010 to the present, plus a projected 2026 roster built from the transfer portal and recruiting class.

**Live demo**: [colemccall.github.io/cfb-analytics](https://colemccall.github.io/cfb-analytics)

---

## Features

- **Team browser** — search and filter all FBS teams by conference and year
- **Player ratings** — proprietary rating system across 8+ attributes per position (Speed, Strength, Awareness, Catching, Throwing, etc.), with radar chart visualization
- **Depth chart** — auto-generated depth charts per position group
- **Schedule & game detail** — full season schedule with score results, top performers per game, drive-by-drive breakdown with play-by-play (EPA per play)
- **Top Players** — cross-team leaderboard filterable by position and attribute
- **Conference view** — rankings across all teams in a conference
- **Team compare** — side-by-side team comparison with bar chart ratings
- **2026 projected season** — roster built from transfer portal entries + recruiting class + returning players, with projected rating ranges
- **Three themes** — Dark (default), Standard, Vintage — switchable without page reload
- **Mobile-friendly** — responsive layout with bottom tab bar on small screens

---

## Data Coverage

| Season | Roster | Stats | Schedule | Drives | Play-by-Play |
|--------|--------|-------|----------|--------|--------------|
| 2010–2025 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 2026 (projected) | ✓ | — | — | — | — |

Play-by-play data is split into weekly `.json.gz` files and loaded on-demand when you drill into a game.

---

## Tech Stack

- **Frontend**: Vanilla HTML/CSS/JS — single `index.html`, no framework, no build step
- **Charts**: [Chart.js 4](https://www.chartjs.org/) (CDN) for radar and future charts
- **Fonts**: [Barlow Condensed](https://fonts.google.com/specimen/Barlow+Condensed) via Google Fonts
- **Data pipeline**: Python 3 — fetches from [College Football Data API](https://collegefootballdata.com/), rates players, writes JSON
- **Hosting**: GitHub Pages (static, no server)

---

## Running Locally

Open `index.html` directly in a browser, or use any static file server:

```bash
# Python
python -m http.server 8080

# Node
npx serve .
```

Then open `http://localhost:8080`.

---

## Data Pipeline

The pipeline fetches data from the [CFB Data API](https://collegefootballdata.com/) and writes compressed JSON to `app/assets/data/{year}/`.

### Setup

```bash
cd data-pipeline
pip install requests python-dotenv
echo "CFB_API_KEY=your_key_here" > .env
```

Get a free API key at [collegefootballdata.com](https://collegefootballdata.com/).

### Run

```bash
python fetch_and_rate.py
```

Processes all years (2010–2025 + 2026 projected). Results are cached in `data-pipeline/.cache/` so re-runs skip already-fetched API calls.

### Output files per year

| File | Description |
|------|-------------|
| `teams_private.json(.gz)` | Full team data with colors, logos, ratings |
| `players_private.json(.gz)` | Full player data with ratings |
| `ratings.json(.gz)` | Flat rating map keyed by player ID |
| `team_schedule.json(.gz)` | Game results and opponent SP+ ratings |
| `player_gamelog.json(.gz)` | Per-player, per-game stats |
| `team_drives.json(.gz)` | Drive summaries per team |
| `team_plays_wk{01-16}.json.gz` | Play-by-play by week (weekly splits for size) |

---

## Rating System

Player ratings are computed in `data-pipeline/rating_engine.py` using:

- **Season stats** (yards, TDs, efficiency metrics) from the CFB Data API
- **PPA** (Predicted Points Added) per play
- **Recruiting star rating** as a signal for young/unproven players
- **SP+ team quality multiplier** — same stats on a stronger team score higher
- **Year-over-year trajectory** for returning players
- **Draft position / All-American awards** as floor signals for elite players

Offensive linemen are excluded from the roster view — no reliable individual OL stats are publicly available.

---

## Project Structure

```
index.html              # Entire frontend (HTML + CSS + JS)
app/
  assets/
    data/
      years.json        # Available season list
      years_meta.json   # Seasons vs projected years
      {year}/           # Per-season data files
data-pipeline/
  fetch_and_rate.py     # Main pipeline entry point
  api_client.py         # CFB Data API client with caching
  rating_engine.py      # Player rating computation
  .cache/               # API response cache (gitignored)
```

---

## License

MIT
