# CFB Data Pipeline

Fetches real CFB data from the College Football Data API and generates static JSON assets for the app.

## Setup

```bash
cd data-pipeline
pip install -r requirements.txt
```

Create a `.env` file in this directory (or the project root):

```
CFB_API_KEY=your_api_key_here
```

Get a free API key at: https://collegefootballdata.com/key

## Running

```bash
python fetch_and_rate.py
```

Outputs land in `../app/assets/data/`:

| File | Contents |
|------|----------|
| `teams_private.json` | Real team names, colors, logos |
| `teams_public.json` | Anonymized team names |
| `players_private.json` | Real player names + physical info |
| `players_public.json` | Anonymized player names |
| `ratings.json` | All player ratings (same for both modes) |

## Re-running for a new season

Change the `YEAR` constant at the top of `fetch_and_rate.py` and re-run.

## Notes

- The pipeline only targets FBS teams (currently 134 teams)
- Players with no individual stats (primarily linemen) receive ratings based on team-level proxy stats with deterministic ±5 variance
- Rating normalization uses min-max scaling per position group, with a curve applied so the top 1% land in 90–99, median in 55–65, and bottom 10% in 40–50
