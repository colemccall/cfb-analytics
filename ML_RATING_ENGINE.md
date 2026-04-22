# ML Rating Engine — Implementation Plan

## Overview

The hand-crafted formula rating engine (`rating_engine.py`) stays forever. ML ratings are **additive** — new fields (`mlOverall`, `mlAttributes`, `winsAdded`, `projectedOverall`, `trajectory`) sit alongside `overall`. Users see both. We never replace, only augment.

---

## Why XGBoost

- Non-linear relationships (e.g., "elite team + low individual stats + high snap% → system contributor")
- Human-readable feature importance — we can explain every rating
- No GPU required, trains in seconds on 250k player-seasons
- Works well on tabular data with mixed numeric/categorical features

---

## Ground Truth Labels (No Draft Data)

Three label sources combined:

**A) Award Tier** (~100–150/year, already fetched via `/awards`)
- Heisman winner/finalist, Biletnikoff, Butkus, Bednarik, Nagurski, Outland, Rimington, Thorpe
- Winner = tier 1, finalist = tier 2, All-American = tier 3
- Non-negotiable anchors: award winners must rank #1 at their position

**B) PPA** (continuous signal, all players with stats)
- Already computed per player — primary regression target for skill positions

**C) User-provided labels** (`data-pipeline/ground_truth.json`)
```json
[
  {"playerId": "4890973", "year": 2024, "position": "RB", "trueRating": 97, "notes": "Jeanty — 2601 yds, Heisman finalist"}
]
```
~50–100 labels anchor the model on borderline players.

---

## Phase A: Data Preparation

**Goal**: Clean feature matrix, career linking, award tier per player.

**Tasks**:
1. `data-pipeline/feature_extractor.py` — reads `ratings.json` + `player_gamelog.json` for 2019–2024, builds one row per player-season
2. Career linking — match player across years by `(firstName, lastName, school)` fuzzy match + playerId
3. Store `awardTier` field in pipeline output (data is already fetched)
4. Build `ground_truth.json` schema, pre-populate with ~20 obvious anchors
5. `winsAdded` already done (Track 3): `ppa * snap_pct * games / 15.0`

**Output**: `data-pipeline/features/features_2019_2024.csv`

**~62 feature columns**:
- Identity: year, position group, year-in-school
- Stats: all skill attrs (passYds, rushYds, recYds, tackles, etc.)
- PPA: avgPpa, totalPpa, passRushPpa, runGamePpa
- Usage: snapPct, passSnap, rushSnap, games
- Context: teamSP, opp-weighted stats, recruit stars, conference tier (P4/G5)
- Prior year: priorOverall, priorPpa, YoY deltas for each stat
- Award: awardTier (0/1/2/3)

---

## Phase B: Model 1 — Per-Position Attribute Predictor

**Goal**: XGBoost models that predict per-attribute scores from features.

**Architecture**:
- One model per position group: QB, RB, WR/TE, DL, LB, DB, OL, K, P
- Input: ~62 features
- Output: per-attribute scores (same attributes as current formula)
- Training: 2019–2024 (~90k records with full features)

**Key advantages**:
- OL: learns from team blocking efficiency + stars + snap% without explicit formula
- Pre-2019 DL: uses team-level signals to impute individual ratings, removing `noRating` gap
- Non-linear: handles system contributors, scheme-dependent players correctly

**Validation**:
- Award winners rank #1 at their position
- PPA correlation > 0.7 for skill positions
- Formula vs ML correlation > 0.85 (should mostly agree, ML fixes outliers)

**New pipeline output fields**:
```json
{
  "mlPassRush": 91,
  "mlRunStop": 87,
  "mlOverall": 89,
  "formulaOverall": 86,
  "awardTier": 0
}
```

**New files**:
- `data-pipeline/ml_engine.py` — training + inference
- `data-pipeline/models/xgb_{position}.joblib` — saved models

---

## Phase C: Cross-Position OVR Calibration

**Goal**: Ensure OVR is comparable across positions.

**Problem**: Is an 85 OVR DL equivalent to an 85 OVR QB in actual game value? No — QB has far more leverage per play.

**Solution**: Positional value offset layer
- Train small linear model on award-tier anchors across positions
- Heisman-winner QB should rate higher than Outland-winner OL in absolute terms
- Baked into `mlOverall`, not `formulaOverall`

**Also enables**: "Who is the best college football player of all time?" — cross-position calibrated comparison.

---

## Phase D: Model 3 — Trajectory Predictor

**Goal**: Project returning player ratings for next season (powers 2026 projected roster).

**Architecture**:
- Gradient boosted trees on year-over-year deltas
- Input: current rating, year-in-school (Fr/So/Jr/Sr), YoY stat deltas, position age curve
- Output: predicted N+1 `mlOverall`
- Label: actual observed year N+1 rating (self-supervised)
- Training: ~40k multi-year player records

**New pipeline output fields**:
```json
{
  "projectedOverall": 96,
  "trajectory": "+3",
  "trajectoryDir": "up"
}
```

**UI additions**:
- Trajectory arrow (↑ / → / ↓) next to OVR on player card
- Player modal shows: Formula OVR | ML OVR | Projected OVR
- 2026 roster uses `projectedOverall` as primary display rating

---

## Phase E: Labeling Tool

**CLI**: `data-pipeline/label_players.py`
- Shows player name, position, year, stats, formula rating, current ML rating
- User inputs true rating (or Enter to skip)
- Prioritizes players where formula and ML disagree most
- Saves to `ground_truth.json`

---

## Sequencing

| Phase | Task | Effort | Depends On |
|---|---|---|---|
| A | Feature extraction + career linking | 1 session | Nothing |
| B | Model 1: per-position attribute predictor | 1–2 sessions | Phase A |
| C | Cross-position OVR calibration | 0.5 session | Phase B |
| E | Labeling tool CLI | 0.5 session | Phase B |
| D | Trajectory model + UI | 1–2 sessions | Phase B |

---

## Core Constraint

> The existing hand-crafted rating formula (`rating_engine.py`) **always stays**. `overall` is always the formula rating. ML ratings are always additive new fields. Never replace, only augment.
