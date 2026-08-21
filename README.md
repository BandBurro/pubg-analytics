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

## Gold layer results

### Weapon classes recover themselves from the data

`weapon_class` comes from a curated seed — asset names don't reveal whether a gun
is a DMR. But engagement distance was never told what any weapon is, and it
reproduces weapon roles exactly:

| Class | HvH kills | Median | p90 |
|---|---|---|---|
| shotgun | 5,533 | 7 m | 15 m |
| smg | 12,228 | 13 m | 43 m |
| assault_rifle | 28,305 | 20 m | 57 m |
| lmg | 15,531 | 25 m | 77 m |
| dmr | 6,327 | 106 m | 229 m |
| sniper_rifle | 6,984 | 131 m | 262 m |

Perfectly monotonic. Seed coverage is 99.6% of firearm kills; the 591 unclassified
kills sit at 127 m median, which suggests they are sniper-class.

### Drop choice moves expected finish by ~26 percentile points

Erangel, squad only, 500 m grid cells, humans in analytical matches, min 80 drops.
`avg_finish` is percentile among human teams: 0.0 = won, 1.0 = last.

| | Cell | Drops | Avg finish | Avg human kills | Avg survival |
|---|---|---|---|---|---|
| deadliest | 6_6 | 1,515 | **0.602** | 1.00 | 600 s |
| deadliest | 12_9 | 1,085 | 0.585 | 1.13 | 560 s |
| safest | 4_9 | 87 | 0.351 | 0.49 | 1,020 s |
| safest | 11_2 | 264 | **0.345** | 0.94 | 1,065 s |

The deadliest cells are also the most popular — the hot-drop pattern. Kills are
~1.0 in **both** groups, so hot drops don't earn you more kills; they just end your
match sooner. Survival time is 77% longer in the safe cells.

Squad-only on purpose: placement percentile is only comparable within a team mode.

### The match arc, recovered

| Phase | Alive | Zone radius | Deaths/min | Median engagement |
|---|---|---|---|---|
| 1 | 83 | 5,290 m | **5.19** | 17 m |
| 2 | 45 | 1,819 m | 2.82 | 31 m |
| 4 | 29 | 617 m | 3.16 | 41 m |
| 6 | 13 | 241 m | 2.39 | 49 m |
| 9 | 5 | 73 m | **8.88** | 34 m |

A U-shaped violence curve: the landing bloodbath, a quieter mid-game, then the
final circle at nearly double phase 1's death rate. Engagement distance rises as
the game opens up, then collapses as the circle squeezes players together.

## Phase 4: the rating engine, and why sampling strategy decides everything

The engine is Plackett-Luce over human-relative team placements, run as a
sequential fold in Python rather than SQL — match N's update depends on the state
match N-1 left behind. It emits `fact_rating_update` with pre- and post-state per
player per match, which is what makes point-in-time correctness *enforceable*:
`ordinal_pre` is the only rating a model may use to predict that match.

A dbt test asserts the chain directly — a player's `mu_pre` must equal their
previous `mu_post`. If that ever drifts, every feature built on it is leaking the
future into the past.

### The engine works. The data couldn't feed it.

The real corpus can't validate a rating system: **no player had more than 4
matches in it**, so every rating sat on its prior. So the engine is validated
against a synthetic population with skill we chose, measuring how well it recovers
an ordering it was never told:

| Matches per player | Spearman ρ vs true skill |
|---|---|
| 17 | 0.780 |
| 33 | 0.850 |
| 133 | **0.967** |
| 400 | 0.974 |

**A usable rating needs ~30 matches per player**, with diminishing returns past
~130. The `/samples` corpus provided **1.02**. Of 83,596 rated players, zero
converged; average sigma moved from 8.333 to 8.32.

That is not an engine problem. It is a sampling problem, and it was invisible
until the rating engine existed to expose it.

### The fix: collect along players, not matches

`/samples` optimises for breadth — random matches across the whole player base, so
the same player essentially never recurs. Skill rating needs depth. The `/players`
endpoint returns a player's own match history, 10 accounts per request:

| Strategy | Per API call | Repeated players |
|---|---|---|
| `/samples` | ~900 random matches | almost none |
| `/players` (`just cohort`) | ~5,350 matches for 10 known players | **~535 each** |

Fifty seed players expanded to **26,761 distinct matches — 535 per player** in five
API calls. That is well past the convergence threshold. Breadth and depth are both
useful, so both collectors are kept: `just collect` for map and mode coverage,
`just cohort` for rating depth.

## Phase 6: balance marts

Four marts, all restricted to analytical matches with human victims.

### Weapon dominance reverses with range

`mart_engagement_matrix` pairs the killer's weapon class against what the victim
was holding, in both directions, with empirical-Bayes shrinkage toward 0.5.

| Matchup | 0–10 m | 10–50 m | 50–100 m | 100–200 m |
|---|---|---|---|---|
| AR vs sniper | — | **0.70** | 0.27 | — |
| AR vs DMR | **0.69** | 0.66 | 0.27 | — |
| shotgun vs AR | **0.63** | — | — | — |
| sniper vs LMG | — | — | 0.65 | **0.82** |

Assault rifles beat snipers 70% of the time inside 50 m and lose 73% of the time
past it. The reversal is clean, symmetric, and was never encoded anywhere — it
falls out of 250,997 observed engagements.

### The trap that nearly produced a fake finding

The first version of this mart reported **throwables beating assault rifles at a
raw win rate of exactly 1.000 across 2,343 fights**. That is not a matchup result.
`victim_weapon` is what the victim was *holding when they died*, and nobody dies
holding a grenade — so throwables appear as killer constantly and as victim never.

A win rate of exactly 1.0 over thousands of trials is the tell. Real matchups
don't go undefeated.

The fix is `has_symmetric_observation`: both classes must be seen on both sides at
least 100 times. The funnel is worth stating plainly:

| Filter | Cells |
|---|---|
| All cells | 343 |
| ≥200 fights | 120 |
| **Interpretable (symmetric)** | **58** |

**Half the cells that pass a naive sample-size check are still meaningless.** Row
count is not evidence.

### Zone luck: small, and pointing the other way

Placement by how far the drop point sat from the *second* circle, in radii.
(Phase 1's circle averages 5,485 m on an 8 km map — everyone is inside it, so the
first circle cannot measure luck at all.)

| Drop position | Player-matches | Avg finish | Avg survival | Distance travelled |
|---|---|---|---|---|
| deep inside | 20,963 | 0.521 | 644 s | 1,996 m |
| inside | 49,384 | 0.524 | 642 s | 2,200 m |
| just outside | 46,004 | 0.510 | 682 s | 2,707 m |
| one radius out | 28,651 | 0.491 | 739 s | 3,336 m |
| far outside | 22,832 | **0.480** | 784 s | 4,044 m |

Landing **far** from the circle is associated with a **better** finish — about 4
percentile points — not a worse one. Those players also survive 22% longer. The
plausible reading: landing inside the circle means landing where everyone else is
headed, so the circle draw is less a lottery than a crowding signal.

Caveat unchanged: drop choice isn't independent of the circle, since players see it
before they jump. This bounds the association rather than isolating a cause.

## Phase 6.5: the estimator study

Every other phase measures PUBG. This one measures the methods — and it is the one
thing real data cannot do, because you can only ask *how wrong is this estimator*
when you already know the answer. Five studies, `just study`, five numbers.

### 1. How much evidence a win rate actually needs

True rate 0.60, 2,000 replicates per row.

| n | mean abs error | p90 abs error | within 5pp |
|---|---|---|---|
| 25 | 0.0781 | 0.1600 | 45% |
| 100 | 0.0392 | 0.0800 | 68% |
| 200 | 0.0275 | 0.0550 | 84% |
| 500 | 0.0171 | 0.0360 | 98% |
| 2,000 | 0.0086 | 0.0180 | 100% |

**A 200-observation cell still has a 90th-percentile error of 5.5 pp.** That is the
number to quote when someone ranks matchups whose sparsest cells hold a few dozen
fights.

### 2. The study found a bug in this project's own code

The engagement matrix shipped with a shrinkage prior of **k = 200**, chosen by
feel. Sweeping it against known truth:

| prior weight | overall RMSE | tiny | small | medium | large |
|---|---|---|---|---|---|
| 0 | 0.1600 | 0.2354 | 0.0714 | 0.0357 | 0.0179 |
| 10 | 0.0766 | 0.1016 | 0.0611 | 0.0345 | 0.0178 |
| **20** | **0.0748** | 0.0988 | 0.0602 | 0.0347 | 0.0179 |
| 200 | 0.0973 | 0.1141 | 0.0963 | 0.0658 | 0.0298 |

k=200 was worse than k=20 in **every** stratum. Shrinkage itself is clearly worth
it — it more than halves RMSE on tiny cells — but that prior was far too strong.

The fix isn't to guess better. For a prior centred on p with true-rate variance s²,
the equivalent sample size is `k = p(1-p)/s² - 1`, and s² is estimable by
subtracting average binomial sampling variance from the observed variance of the
rates. On the real matchup cells: s² = 0.0279 (sd 0.167), so **k = 8**. The mart now
uses 10.

The practical cost of the original mistake: sniper-vs-LMG at 100–200 m, a cell with
896 observations, was being reported as **0.817 instead of 0.883**. The
over-shrinkage was distorting the best-evidenced results most.

### 3. Confounding, from first principles

Two weapons with **identical** true lethality, one used mostly against weak
opponents:

| weapon | naive rate | stratified rate |
|---|---|---|
| A (85% vs weak) | 0.7214 | 0.5452 |
| B (15% vs weak) | 0.3733 | 0.5483 |

A **34.8 pp** gap collapses to **0.3 pp** once you stratify by who they faced. This
is the MP5K finding reproduced from first principles — the entire apparent
difference was matchmaking, not the weapon.

### 4. What leakage buys that isn't real

Running the production rating engine over a synthetic league, then predicting each
result twice:

- from the rating known **before** the match: AUC **0.5870**
- from the rating computed **after** it: AUC **0.6253**
- inflation: **+0.038** over 12,000 observations

A free 4-point AUC gain that evaporates in production, because `ordinal_post`
already contains the answer.

### 5. Confidence intervals that lie

Nine correlated phases per match, 5,400 observations:

- naive standard error (assuming independence): 0.01921
- actual standard error: 0.04179
- **understated by 2.17x**, design effect 4.73

Intervals come out **54% too narrow**. That is how a null result gets reported as
significant.

## Phase 5: prediction, calibration, and a bug that inverted two findings

Target: does a player finish in the better half of the human field? Split **by time
and by match** — a random split would let the future inform the past and scatter
one match's shared outcome across both sides.

| model | log loss | Brier | AUC | ECE |
|---|---|---|---|---|
| base rate | 0.69309 | 0.24997 | 0.5000 | 0.00329 |
| point-in-time model | 0.68934 | 0.24814 | **0.5412** | 0.01182 |
| leaked model (`ordinal_post`) | 0.32130 | 0.07766 | **0.9516** | 0.05677 |

**The honest model is barely better than a coin flip**, and that is the correct
answer to report right now: ratings have not converged, so `ordinal_pre` is the
prior for almost every player and carries nearly no signal.

The leaked model looks spectacular. On real data the leak is worth **+0.41 AUC**,
against **+0.038** in the synthetic study. The difference is the lesson:

> **Leakage looks most impressive exactly when your legitimate features are
> weakest.** A model with no real signal plus a leak looks like a triumph.

Note also that the leaked model is *worse calibrated* (ECE 0.057 vs 0.012) while
scoring far better on AUC. Ranking and honesty are different axes, which is why
this harness reports both.

### The bug this phase found

The model's first run showed a base rate of **0.9334** — 93% of players finishing
"in the top half". That is impossible, and it exposed a real bug in Silver.

`human_placement` is a *team*-level rank (teammates share a `win_place`, so
`dense_rank` collapses them), but I divided it by `human_count`, a *player* count.
The scale compressed by roughly the team size:

| mode | avg `human_placement_pct` before | after |
|---|---|---|
| solo | 0.494 | 0.503 |
| duo | 0.274 | 0.498 |
| squad | 0.179 | 0.510 |

Solo was right, which is why nothing looked obviously broken. Worse than the
compression: it was *mode-dependent*, so pooling squad, duo and solo averaged three
different scales together.

Two published findings changed when it was fixed. The drop-zone spread went from
0.117–0.206 to **0.345–0.602**, and the zone-luck effect **reversed direction** —
landing far from the circle turns out to be associated with better finishes, not
worse. Both sections above have been corrected.

The bug survived a `dbt_utils.accepted_range(0, 1)` test, because every value was
inside [0, 1]. It took a downstream model producing an absurd base rate to surface
it. Range tests check that a number is *possible*, not that it is *right*.

## Roadmap

| Phase | What |
|---|---|
| 1 | Collector ✅ — running on a 2h schedule |
| 1.5 | Lift the collector to AWS Lambda + EventBridge + S3, via Terraform |
| 2 | Bronze → typed Parquet: 8 tables, 1.53M rows ✅ |
| 2.5 | Silver in dbt: 8 models, 35 tests, dedup + integrity flags ✅ |
| 3 | Gold: 3 dims, 3 facts, 91 dbt nodes ✅ |
| 4 | Ratings: Plackett-Luce fold, PIT-tested, + player-cohort collector ✅ |
| 5 | Prediction + calibration harness; found the placement-scale bug ✅ |
| 6 | Balance marts: engagement matrix, drop zones, zone luck ✅ |
| 6.5 | Estimator study — found and fixed a real shrinkage bug ✅ |
| 7 | Scale out: S3 + Delta/Iceberg + Spark for the position layer (~750M rows at 100k matches) |
| 7.5 | Deliberately induce skew and fix it — where distributed intuition comes from |

## Stack

Python · httpx · DuckDB · dbt · Parquet · OpenSkill · Spark on Databricks Free Edition · Terraform
