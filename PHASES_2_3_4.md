# CFB Video Game — Development Roadmap: Phases 2, 3, and 4

This document captures the agreed-upon roadmap following completion of Phase 1. Each phase builds on the last and is designed to be self-contained for a single development session.

---

## Phase 1 — Completed ✓

- OL position-specific ratings (LT/RT/OG/C) with sack attribution by position
- Stat volume thresholds for QB/RB/WR/TE (low-attempt players penalized beyond games-played)
- Freshman/backup OVR ceiling: low-snap players with no stats capped at starter's OVR
- Kicker and punter formula improvements (FG%, in-20 rate, longest FG)
- RB receiving blend improvement (rec TDs + receptions added)
- LB MLB/OLB formula differentiation (OLB skews passRush, MLB/ILB skews runStop)
- Flexible depth chart: auto-detects whether team uses generic (DL/LB/DB) or specific (DT/DE/MLB/OLB/CB/S) positions and renders accordingly
- Removed public/private toggle (always use real names/data)

---

## Phase 2 — Game Log Viewer

**Goal:** Add game-by-game stats for players and teams to enable drill-down analysis.

### Pipeline additions (`fetch_and_rate.py` + `api_client.py`)

1. **`api_client.py`**: Add `fetch_game_player_stats(api_key, year)` → `/games/players`
   - Returns per-game, per-player stats for every game in a season
   - Filter to `seasonType=regular`

2. **`api_client.py`**: Add `fetch_games(api_key, year)` → `/games`
   - Returns game results, scores, opponent, date, neutral site
   - Filter to `seasonType=regular`

3. **Pipeline output** — new files per year:
   - `player_gamelog.json`: `{playerId: [{week, opponent, gameId, stats...}, ...]}`
   - `team_schedule.json`: `[{gameId, week, homeTeam, awayTeam, homeScore, awayScore, date, neutralSite}]`

4. **Data architecture**: Load `player_gamelog.json` and `team_schedule.json` lazily (only when a player card or team schedule is opened). Do NOT include in the initial page load.

### Viewer additions (`index.html`)

1. **Player card — Game Log tab**: new tab in the player modal showing week-by-week stats table
   - Columns: Week, Opponent, Result (W/L/score), key stats by position (same display_stats layout)
   - Color-code performances (big games = green highlight)

2. **Team schedule drill-down**: new tab or panel on the team view
   - Table: Week, Opponent, Result, Margin, Opponent SP+ rank
   - Click a game → show that game's box score (top performers for both teams)

3. **Trajectory rating** (requires 2+ years of data):
   - For returning players with 2+ years of data, show "Career Trajectory" projection in the player card
   - Algorithm: `projected = current_ovr + clamp((yoy_growth * 0.4 + momentum_bonus) * regression_dampener, -5, +8)`
   - Where `momentum_bonus` is derived from YoY improvement in key efficiency stats
   - `regression_dampener = 1.0 - (current_ovr - 60) / 100` (elite players improve less)
   - Show as "Current: 84 | Trajectory: ~90" with a model disclaimer

---

## Phase 3 — Play-by-Play Advanced Metrics

**Goal:** Leverage `/plays` endpoint to compute advanced metrics without loading raw play data into the browser.

### Architecture principle
Raw play-by-play data (`/plays`) is **never stored in `/data/` or loaded in the browser**. All play data is consumed in the pipeline and aggregated into compact per-player/per-team summary files.

### Pipeline additions

1. **`api_client.py`**: Add `fetch_plays(api_key, year, team)` → `/plays`
   - Fetch per team (to avoid rate limiting), cached per-team-per-year
   - Key fields: `playType`, `yardsGained`, `down`, `distance`, `yardLine`, `clock`, `scoreDiff`, `players`

2. **Derived metrics computed in pipeline** → `player_advanced.json`:
   - **Explosive play rate**: % of touches/targets going for 20+ yards
   - **Success rate**: % of plays moving the chains (100% on 1st+5 yds, 50% on 2nd down conversions, 100% 3rd/4th down conversions) — per player and team
   - **EPA proxy**: simplified expected points added using down/distance tables (not full model, but directional)
   - **Red zone efficiency**: TD rate on possessions inside the 20 per player (QB/RB/WR)
   - **Clutch factor**: performance when score differential is within 7 in 4th quarter

3. **Team advanced** → blended into `team_schedule.json` enrichment:
   - Team success rate, explosive rate, havoc rate per game

### Viewer additions

1. **Player card — Advanced tab**: EPA proxy, success rate, explosive rate, clutch factor
   - Shown as normalized ratings vs FBS average (above/below average indicators)
2. **Team schedule — game detail**: per-game advanced stats for both teams
3. **Rating improvement**: use explosive play delta year-over-year to power the trajectory rating (Phase 2)

---

## Phase 4 — Deep Analytics Dashboard

**Goal:** Build sophisticated cross-team and cross-player analytics that make the app genuinely useful for franchise-mode decision-making.

### Feature: Matchup Predictor
- Compare any two teams across all SP+ components, player ratings, recent form
- Output: predicted margin of victory with confidence range
- Uses: SP+ offense vs defense matchup, OL rating vs DL rating, QB rating vs DB rating

### Feature: Recruiting Class Grader
- Per team, per year: grade the recruiting class using normalized recruiting composite scores
- Rank classes nationally and by conference
- Track hit rate (how many recruits became starters, reached 80+ OVR)
- Viewer: "Recruiting" tab on team view

### Feature: Transfer Portal Impact
- Compare player ratings pre- and post-transfer across years
- Identify transfers who upgraded (moved to better conference, higher OVR) vs downgraded
- Surface as a "Transfer Report" per team showing net portal gain/loss in OVR

### Feature: Draft Projection Model
- For current players (juniors/seniors), use trajectory rating + position value + team quality
- Project likely NFL draft round or undrafted
- Viewer: show draft projection badge in player card for eligible players

### Feature: Play Type Tendencies (from Phase 3 play data)
- Team run/pass ratio by down and distance
- Formation tendencies (from play type categories in `/plays`)
- Viewer: "Tendencies" tab on team view — useful for game planning

### Data architecture evolution
- Consider migrating from multiple JSON files to a single SQLite database served via `sql.js` (WASM)
- Alternatively: Service Worker caching strategy so repeat visitors load nothing
- Evaluate IndexedDB for caching large play aggregate files client-side

---

## Notes for Next Session

- Clear `.cache/` in the pipeline if re-fetching with new endpoints (Phase 2+ adds new API calls)
- `player_gamelog.json` and `team_schedule.json` should be loaded lazily using dynamic `fetch()` calls in the viewer, not included in the initial `loadData()` parallel fetch
- The `/plays` endpoint is rate-limited and expensive — cache aggressively and process per-team, not per-game
- For trajectory rating: only show for players who appear in at least 2 consecutive years in the data (use player ID matching across years)
- Red zone efficiency (Phase 3): track yard line ≤ 20 on play start, flag TD outcomes per player — this is the most impactful individual signal not in the current model
