# CFB Analytics

A college football analytics and dynasty exploration platform — no backend required. Browse team rosters, player ratings, schedules, drive charts, and play-by-play data for every FBS season from 2006 to the present, plus a projected 2026 roster built from the transfer portal and recruiting class.

**Live**: [colemccall.github.io/cfb-analytics](https://colemccall.github.io/cfb-analytics)

---

## Features

- **Team browser** — search and filter all FBS teams by conference and year
- **Player ratings** — stat-derived ratings across 6–8 position-specific attributes, with skill bars and career comparison
- **Depth chart** — auto-generated depth charts per position group with skill chips
- **Schedule & game detail** — full season schedule, per-player box score (Passing/Rushing/Receiving/Defense), drive-by-drive breakdown with play-by-play (EPA per play)
- **Career tab** — year-by-year stats, position history, transfer history, two-way player detection
- **Top Players** — cross-team leaderboard filterable by position
- **Conference view** — rankings across all teams in a conference
- **Team compare** — side-by-side team comparison
- **2026 projected season** — roster from transfer portal + recruiting class + returning players
- **Four themes** — Analytics Dark/Light, Dynasty Dark/Light — switchable without page reload
- **Mobile-friendly** — responsive layout with bottom tab bar on small screens

---

## Data Coverage

| Season | Roster | Stats | Schedule | Drives | Play-by-Play |
|--------|--------|-------|----------|--------|--------------|
| 2006–2025 | ✓ | ✓ | ✓ | ✓ | ✓ |
| 2026 (projected) | ✓ | — | — | — | — |

Play-by-play is split into weekly `.json.gz` files and loaded on-demand when you drill into a game.

---

## Project Structure

```
cfb-analytics/
├── index.html                     # Dynasty UI — main entry point
├── css/
│   └── main.css                   # All styles (themes, components, layout)
├── js/                            # Shared JS modules (future extraction)
├── app/
│   ├── assets/data/{year}/        # Per-season JSON data files
│   ├── style-matchups/            # Style Matchup Explorer tool (Phase 3)
│   └── player-swap/               # Player Swap Simulator tool (Phase 4)
├── analyses/
│   ├── boise-state-eras/          # Long-form article (Phase 5)
│   └── indiana-blueprint/         # Long-form article (Phase 5)
├── data-pipeline/
│   ├── fetch_and_rate.py          # Main pipeline entry point
│   ├── api_client.py              # CFB Data API client with caching
│   ├── rating_engine.py           # Player rating computation
│   └── .cache/                    # API response cache (gitignored)
└── README.md
```

---

## Tech Stack

- **Frontend**: Vanilla HTML/CSS/JS — no framework, no build step
- **CSS**: External `css/main.css` with CSS custom properties for theming
- **Fonts**: [Inter](https://fonts.google.com/specimen/Inter) (UI) + [Barlow Condensed](https://fonts.google.com/specimen/Barlow+Condensed) (display headings) via Google Fonts
- **Charts**: [Chart.js 4](https://www.chartjs.org/) (CDN)
- **Data pipeline**: Python 3 — fetches from [College Football Data API](https://collegefootballdata.com/), rates players, writes compressed JSON
- **Hosting**: GitHub Pages (static)

---

## Running Locally

```bash
# Python
python -m http.server 8080

# Node
npx serve .
```

Then open `http://localhost:8080`.

---

## Data Pipeline

### Setup

```bash
cd data-pipeline
pip install requests python-dotenv
echo "CFB_API_KEY=your_key_here" > .env
```

Get an API key at [collegefootballdata.com](https://collegefootballdata.com/).

### Run

```bash
# All years
python fetch_and_rate.py

# Specific year(s)
python fetch_and_rate.py 2024 2025
```

API responses are cached in `data-pipeline/.cache/` — re-runs skip already-fetched calls.

### Output files per year

| File | Description |
|------|-------------|
| `teams_private.json(.gz)` | Team data with colors, logos, ratings |
| `players_private.json(.gz)` | Player data with ratings and status |
| `ratings.json(.gz)` | Flat rating map keyed by player ID |
| `team_schedule.json(.gz)` | Game results and opponent SP+ ratings |
| `player_gamelog.json(.gz)` | Per-player, per-game stats |
| `team_drives.json(.gz)` | Drive summaries per team |
| `team_plays_wk{01–16}.json.gz` | Play-by-play by week |

---

## Rating System

Ratings are 100% stat-derived — no made-up physical attributes. Computed in `data-pipeline/rating_engine.py` using:

- **Season stats** (yards, TDs, efficiency) from the CFB Data API
- **PPA** (Predicted Points Added) per play
- **SP+ team quality multiplier** — same stats on a stronger team score higher
- **Recruiting rating** as signal for young/unproven players
- **Draft position / All-American awards** as floor signals for elite players

Offensive linemen are excluded from roster view (no reliable individual OL stats available).

---

## Roadmap

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Bug fixes, trajectory removal, UI modernization | ✅ Done |
| 1 | Repo restructure (CSS/JS extraction, directory layout) | ✅ Done |
| 2 | Core feature completion (recruiting, portal view, schedule enhancements) | Upcoming |
| 3 | Style Matchup Explorer (2021+ ML clustering) | Planned |
| 4 | Player Swap Tool (2021+ EPA model) | Planned |
| 5 | Analytical articles (Boise State eras, Indiana blueprint) | Planned |
| 6 | In-season evaluations (Fall 2026) | Planned |

---

## License

MIT
