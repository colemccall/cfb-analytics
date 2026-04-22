# Apps & Use Cases

Three concrete applications built on top of the analytics platform.

---

## App 1: Transfer Portal Planner

**Concept**: For a given team + year, model the impact of gaining/losing portal players on projected roster quality.

**Features**:
- Current roster projected OVR by position group (bar chart)
- "If [player] leaves" toggle — shows projected drop in position group strength
- "Add portal target" search — browse available portal players, see projected impact
- Rank portal targets by: position need score × projected fit rating
- Output: "Your top 3 portal priorities based on current roster projection"

**Data needed**:
- Transfer portal entries (already fetched via `/player/portal`)
- ML ratings + projections (ML Phase B + D)
- Position need score: current avg OVR by position vs league average

**Depends on**: ML Phase B (attribute predictor) + Phase D (trajectory model)

---

## App 2: Player Swap / "What If" Tool

**Concept**: Replace any player on your team with any historical or portal player and see the projected team impact.

**Features**:
- Select team + position
- Browse historical players (2006–2025) or current portal players
- Swap player X for player Y → projected team OVR change
- Cross-year comparison: "2024 Jeanty vs 2019 Jonathan Taylor at RB — who helps more?"
- Cross-position: "Would you rather have a 94 OVR DL or 94 OVR QB?" (answered by winsAdded)

**Data needed**:
- Full historical ratings (all 250k player-seasons — already built)
- `winsAdded` per player (already implemented in Track 3)
- Cross-position calibrated `mlOverall` (ML Phase C)

**UI**: "Swap Player" button on team roster page — opens player search across all years + positions.

**Depends on**: ML Phase C (cross-position calibration)

---

## App 3: Program Analytics Dashboard (Partnership)

**Concept**: Custom private dashboard built for a partnering program, in exchange for GPS/wearable tracking data.

**Features** (program-specific):
- Roster health + trajectory projections (improving vs regressing players)
- Portal departure risk score (players whose ratings plateau + haven't re-signed)
- Opponent tendency charts for upcoming week (already built via Track 3 tendencies)
- Recruiting class grade projection (how will this class look in 2 years?)

**Contingent on**: Outreach to Boise State / Indiana analytics departments.

**Pitch**:
- Free custom analytics dashboard built specifically for their program
- NDA signed — their data stays private
- Research partnership credit if published
- Access to full platform showing 2006–2025 FBS coverage

**Who to contact**:
- Director of Football Analytics (not coaching staff)
- Boise State and Indiana have been progressive with data analytics under recent staffs

---

## Sequencing

| App | Key Dependency | When to Build |
|---|---|---|
| Transfer Portal Planner | ML Phase B + D | After trajectory model |
| Player Swap | ML Phase C | After cross-position calibration |
| Program Dashboard | Partnership outreach success + GPS data | TBD |

---

## winsAdded — The Unifying Metric

`winsAdded = ppa × snap_pct × games / 15.0`

This formula-based metric (no ML needed, already implemented) is the key to cross-position comparisons in the Player Swap app. It answers:

- "Was Jeanty's 2024 season worth 3.1 wins?"
- "Does an elite DL contribute more wins than an elite WR?"
- "Which position group has the most leverage for a given team?"

Already displayed in the player modal under **Advanced**.
