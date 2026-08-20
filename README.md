# pubg-analytics

A PUBG telemetry warehouse: collect real match data, model it properly, then use it
to answer questions about skill, matchmaking fairness, and game balance.

Personal project. Nothing here touches work accounts, repos, or infrastructure.

## Why the collector came first

**PUBG match records age out of the API and cannot be backfilled.** Every day the
collector isn't running is a day of data that is gone permanently. So it ships before
the warehouse, the models, or anything else — it just needs to be saving matches to disk.

## Setup

```bash
brew install uv just
```

Then:

```bash
just setup
```

Register a free API key at <https://developer.pubg.com> (use a **personal** email) and
paste it into `.env`.

## Usage

```bash
just discover        # find new match ids via /samples
just fetch 200       # download match detail + telemetry for pending ids
just collect 200     # discover then fetch — this is what a schedule should call
just status          # how much have we collected
```

## How collection works

Two classes of endpoint, handled differently:

| Endpoint | Rate limited? | Handling |
|---|---|---|
| `/samples`, `/players`, `/seasons` | Yes — 10 req/min on a free key | Sliding-window limiter |
| `/matches/{id}`, telemetry assets | **No — exempt** | Concurrency cap only |

That exemption is the reason bulk collection is practical at all.

Every fetched match id is recorded in a SQLite ledger, so:

- re-running `fetch` never re-downloads what already landed
- a crash mid-run loses at most the matches in flight
- matches that 404 are marked `gone` and never retried
- repeatedly failing matches stop after 3 attempts instead of wedging the queue

Writes go to a temp file and are then renamed, so a half-written blob is impossible.

## Data layout

```
data/
├── ledger.sqlite
└── raw/
    ├── matches/shard=steam/dt=2026-08-16/<match_id>.json.gz
    └── telemetry/shard=steam/dt=2026-08-16/<match_id>.json.gz
```

**Bronze is immutable.** Nothing modifies these files after they land. If the cleaning
logic turns out to be wrong six months from now, the warehouse gets rebuilt from here.

## Data quality findings

Real telemetry, real mess. These were measured on the first 2,145 matches and all
three must be handled in Silver — none of them are visible from the API docs.

### 1. Only ~62% of collected matches are real competitive games

| `match_type` | Share | Usable? |
|---|---|---|
| `official` | 46.6% | yes |
| `competitive` | 14.9% | yes — ranked, best for skill work |
| `airoyale` | 17.6% | no — bot-heavy AI Royale |
| `tutorialatoz` | 16.0% | no — tutorial, not a match |
| `trainingroom` | 4.9% | no — practice range |

`/samples` makes no distinction, so `match_type` has to gate every analytical query.

### 2. Bots are everywhere, and humans farm them

PUBG backfills lobbies with bots (`playerId` = `ai.NNNN`; humans are `account.<hex>`).
Median bot share per match is only 2%, but **30% of matches are more than half bots**.

Measured over `kill_participant`:

| Role | Bot share |
|---|---|
| victim | **64%** |
| killer | 38% |
| finisher | 38% |

Kills skew heavily toward humans killing bots. Any weapon or combat statistic that
doesn't condition on the victim being human is measuring bot-farming, not the game.

There's a subtler consequence for skill rating: bots occupy placement slots, so
placing 20th in a lobby of 50 bots is not the same result as 20th against humans.
Ratings need **human-relative** placement, not raw `winPlace`.

### 3. 22% of `LogParachuteLanding` rows are duplicate emissions

The same logical event is emitted twice — identical timestamp, identical distance,
coordinates differing by ~18 cm. Dedup on `(match_id, account_id, event_ts)`.
Genuine repeat landings (redeploys) survive that key because their timestamps differ.

Bronze keeps the duplicates on purpose. Deduping at ingest would have hidden a 30%
data defect behind a clean-looking table.

### Also worth knowing

- `killer` is absent on **6.8%** of kills (10,632 of 157,290) — blue zone, fall
  damage and suicides have a victim but no killer. An inner join on killer
  silently drops those deaths.
- `attack_id` is **-1 on 31% of kills** and repeats within a match, so it is not a
  key. `kill_participant` looked 6.3% duplicated until the timestamp was added to
  the key — those rows were distinct events sharing an `attack_id`, not duplicates.
- `killer_team_id` **never** equals `victim_team_id`, yet 895 kills carry a
  team-killer. Team kills must be detected via `teamKillers_AccountId`, not by
  comparing team ids.
- Coordinates are in **centimetres** (the 8×8 km maps run to 800,000).
- `roster.won` arrives as the string `"true"`/`"false"`, not a boolean.
- Match length varies from 729 to 74,708 events — the short tail is aborted matches
  and needs a match-integrity flag.

## First real result

Top weapons by kill count, naive versus filtered to analytical matches with human
victims:

| Weapon | Naive rank | Filtered rank | Shift | Median engagement |
|---|---|---|---|---|
| RPD | 2 | **1** | +1 | 25 m |
| AUG | 3 | 2 | +1 | 23 m |
| Beryl M762 | 5 | 3 | +2 | 20 m |
| **MP5K** | **1** | **6** | **−5** | 15 m |
| M249 | 11 | 7 | +4 | 26 m |
| Frag grenade | 12 | 8 | +4 | 37 m |

**The MP5K is the single most lethal weapon in the raw data and sixth once the data
is honest**, losing 68% of its kills. It is concentrated in tutorial matches and
bot lobbies. Filtering discards 44% of all kills (146,658 → 81,304).

An independent sanity check that the pipeline is sound: median engagement distance
separates snipers (M24 130 m, Kar98k 133 m) from shotguns (Winchester 7 m) and SMGs
(15 m) without being told what any weapon is.

## Roadmap

| Phase | What |
|---|---|
| 1 | Collector ✅ — running on a 2h schedule |
| 1.5 | Lift the collector to AWS Lambda + EventBridge + S3, via Terraform |
| 2 | Bronze → typed Parquet: 8 tables, 1.53M rows ✅ |
| 2.5 | Silver in dbt: 8 models, 35 tests, dedup + integrity flags ✅ |
| 3 | **Gold: dims, facts, phase grain** ← you are here |
| 4 | Skill ratings — OpenSkill Plackett-Luce over 1–100 placements |
| 5 | Win/placement prediction + calibration harness |
| 6 | Balance marts: weapon matchups, drop-spot survival, patch-over-patch |
| 6.5 | Estimator study against synthetic ground truth |
| 7 | Scale out: S3 + Delta/Iceberg + Spark for the position layer (~750M rows at 100k matches) |
| 7.5 | Deliberately induce skew and fix it — where distributed intuition comes from |

## Stack

Python · httpx · DuckDB · dbt · Parquet · OpenSkill · Spark on Databricks Free Edition · Terraform
