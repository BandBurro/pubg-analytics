# pubg-analytics

**A battle-royale telemetry warehouse — and a running record of every wrong answer it produced first.**

Collects real PUBG match telemetry, models it into a tested analytics warehouse, and uses it to
measure player skill, matchmaking fairness and game balance. Runs entirely on a laptop plus a
~$2/month AWS footprint.

| | |
|---|---|
| Matches collected | **35,538** |
| Raw telemetry events | **1,519,135,431** |
| Position samples | **231,681,133** |
| dbt models + tests | **155 nodes** |
| Python tests | **31** |
| Cost | **< $2/month** (Lambda + S3 + DynamoDB) |

---

## The part that matters

Any pipeline can produce numbers. This one is built to catch the numbers that are *wrong*, and
the README keeps score. Four examples, each found by measuring rather than assuming:

**The most lethal weapon in PUBG isn't.** The MP5K ranks **#2 by raw kill count and #5** once you
exclude bot victims and non-competitive lobbies — it loses **60% of its kills** (222,671 → 89,106).
Filtering discards 39% of all kills in the dataset. → [Data quality](#data-quality-findings)

**A mart nearly shipped a fake finding.** Throwables showed a win rate of *exactly 1.000 across
2,343 fights*. No real matchup goes undefeated — the tell was the number itself. `victim_weapon`
records what the victim was **holding when they died**, and nobody dies holding a grenade. →
[The observability trap](#the-trap-that-nearly-shipped)

**A synthetic study found a bug in this repo's own code.** The engagement matrix shrank estimates
toward 0.5 with a prior weight of 200, chosen by feel. Swept against known truth, k=200 was worse
than k=20 *in every sample-size stratum* — it was distorting a 896-observation estimate by 7
percentage points. → [Estimator study](#estimator-study-measuring-the-methods)

**A collector ran perfectly and collected nothing.** Eleven invocations, zero errors, and every one
reporting `{"discovered": 0, "attempted": 0, "queued": 0}`. Nothing alarmed, because a function
doing nothing looks identical to a function with nothing to do. →
[The idle collector](#the-collector-that-ran-perfectly-and-collected-nothing)

---

## Architecture

```
PUBG API ──┬─► local collector (httpx, async)  ─┐
           └─► AWS Lambda (stdlib, EventBridge) ─┤
                                                 ▼
                      raw/  JSON.gz, immutable, append-only
                                                 │
                                        shredder (Python)
                                                 ▼
                    bronze/  8 typed Parquet tables + positions
                                                 │
                                          dbt + DuckDB
                                                 ▼
              silver → gold → marts     155 models & tests
                                                 │
                                     ┌───────────┴───────────┐
                                     ▼                       ▼
                       OpenSkill rating engine        balance marts
                        (sequential, not SQL)     weapons · drops · zones
```

**Three layers, one rule each.** Raw is immutable — nothing edits it, so the warehouse can be
rebuilt from scratch when the cleaning logic turns out to be wrong. Silver types and dedupes but
*attaches* integrity flags rather than filtering, so nobody silently loses 40% of the corpus without
knowing. Gold and marts apply the filters explicitly.

**Two collectors, on purpose.** `/samples` gives breadth — random matches across the whole player
base. It also goes stale within a day, which is why the collector also walks player histories via
`/players` for depth. 50 players yielded **43,291 matches** in one invocation, 43× what `/samples`
returned in the same run.

---

## Quickstart

```bash
brew install uv just
just setup
```

Register a free key at [developer.pubg.com](https://developer.pubg.com) and paste it into `.env`.

```bash
just collect 200      # discover + fetch matches
just shred            # raw JSON -> typed Parquet
just shred-positions  # the 231M-row position stream (separate pass)
just pipeline         # dbt build -> ratings -> dbt build
just predict          # train + calibration report
just study            # estimator studies vs synthetic ground truth
```

`just --list` shows everything. Cloud deployment lives in [`infra/`](infra/README.md).

---

## What's in the warehouse

| Table | Grain | Rows |
|---|---|---|
| `slv_player_position` | player × 10s tick | 231,681,133 |
| `fact_kill` | one death | 2,990,554 |
| `fact_player_match` | player × match | 2,952,781 |
| `fact_rating_update` | player × match × system | 1,909,857 |
| `mart_player_rotation` | player × match | 1,929,603 |
| `fact_match_phase` | match × zone phase | — |
| `mart_engagement_matrix` | weapon class × class × range | — |
| `mart_drop_zone` | map × 500m grid cell | — |

`slv_player_position` is a **view, not a table** — nothing queries 231M rows at row grain, so
materialising a second 3 GB copy would buy nothing.

---

## Data quality findings

Real telemetry, real mess. None of this is visible from the API docs.

### 1. How you collect decides how much of your corpus is usable

Eight `match_type` values arrive, and only two are real competitive games:

| `match_type` | Share now | Usable? |
|---|---|---|
| `official` | 68.3% | yes |
| `competitive` | 20.0% | yes — ranked |
| `airoyale` | 4.2% | no — bot-heavy AI Royale |
| `event` (`ibr`) | 3.2% | no — 15–20 players, ~420 s |
| `tutorialatoz` | 3.0% | no — tutorial, not a match |
| `trainingroom` | 1.1% | no — practice range |
| `arcade` (`tdm`) | 0.2% | no — **Team Deathmatch: respawns, no zone, no placement** |
| `custom` | 0.0% | no |

`/samples` makes no distinction, and this is where collection strategy shows up as data quality: the
early `/samples`-only corpus was **57% usable**, while walking real players' match histories brings
it to **88%**. Real accounts play real matches; a random sample includes everyone's tutorial.

TDM having respawns also explains the 4,768 players who appeared to die twice in one match.

### 2. Bots are everywhere, and humans farm them

PUBG backfills lobbies with bots (`ai.NNNN` vs `account.<hex>`). Measured over 5.5M
`kill_participant` rows:

| Role | Bot share |
|---|---|
| victim | **33.9%** |
| killer | 23.6% |

Victims are 1.4× more likely to be bots than killers — humans farm them. Any weapon statistic that
doesn't condition on a human victim is measuring that, not the game. And since bots occupy placement
slots, ratings need **human-relative** placement, not raw `winPlace`.

(These shares were 64% / 38% on the `/samples`-only corpus. Player-history collection halved them,
for the same reason it lifted usable match share from 57% to 88%.)

### 3. 39.5% of landing events are duplicate emissions

Identical timestamp, identical distance, coordinates ~18 cm apart. Deduped on
`(match_id, account_id, event_ts)`; genuine redeploys survive because their timestamps differ.
Bronze keeps the duplicates deliberately — deduping at ingest would hide a 30% defect behind a
clean-looking table — 5,861,935 raw rows become 3,544,464.

Notably this is **event-type-specific**: positions have *12* duplicates in 231 million rows.

### Also worth knowing

- `attack_id` is **−1 on 31% of kills** and repeats within a match — not a key.
- `killer` is absent on **6.8%** of deaths (blue zone, falls, drowning). An inner join drops them.
- `killer_team_id` **never** equals `victim_team_id`, yet 895 kills carry a team-killer. Team kills
  must come from `teamKillers_AccountId`.
- `victim_weapon` carries an engine instance suffix (`WeapRPD_C_7`) where `killer_damage_causer`
  does not (`WeapRPD_C`). Zero of 150,040 joined until normalised.
- Coordinates are in **centimetres**; `roster.won` arrives as the string `"true"`.

---

## Balance analytics

### Weapon classes recover themselves

`weapon_class` comes from a curated seed — an asset name can't tell you whether a gun is a DMR. But
engagement distance was never told what any weapon is, and it reproduces weapon roles exactly:

| Class | HvH kills | Median | p90 |
|---|---|---|---|
| melee | 2,228 | 2 m | 7 m |
| pistol | 5,895 | 5 m | 14 m |
| shotgun | 148,080 | 7 m | 15 m |
| smg | 249,164 | 12 m | 44 m |
| assault_rifle | 580,553 | 20 m | 57 m |
| lmg | 204,553 | 26 m | 76 m |
| dmr | 102,749 | 100 m | 225 m |
| sniper_rifle | 114,368 | 131 m | 263 m |

Perfectly monotonic across eight classes. The 7,904 `unclassified` kills land at 112 m median —
between DMR and sniper — which is itself a hint about what they are.

### Dominance reverses with range

`mart_engagement_matrix` pairs the killer's weapon class against what the victim held, **in both
directions**, with empirical-Bayes shrinkage.

| Matchup | 0–10 m | 10–50 m | 50–100 m | 100–200 m |
|---|---|---|---|---|
| AR vs sniper | — | **0.70** | 0.27 | — |
| AR vs DMR | **0.69** | 0.66 | 0.27 | — |
| shotgun vs AR | **0.63** | — | — | — |
| sniper vs LMG | — | — | 0.65 | **0.82** |

Assault rifles beat snipers 70% inside 50 m and lose 73% beyond it. Clean, symmetric, encoded
nowhere — it falls out of 1.5M observed human-vs-human engagements.

### The trap that nearly shipped

The first working version reported **throwables beating ARs at a raw win rate of exactly 1.000
across 2,343 fights.** That is not a matchup result. `victim_weapon` is what the victim was *holding
when they died*, and nobody dies holding a grenade — so throwables appear as killer constantly and
as victim never.

A win rate of exactly 1.0 over thousands of trials is the tell. The fix requires both classes
observed on both sides:

| Filter | Cells |
|---|---|
| All | 434 |
| ≥200 fights | 216 |
| **Interpretable (symmetric)** | **124** |

**43% of the cells that pass a naive sample-size check are still meaningless.** Row count is not
evidence.

---

## Battle-royale-specific analysis

### Drop choice is worth ~32 percentile points

Erangel, squad only, 500 m cells, humans in analytical matches. `avg_finish` is percentile among
human teams — 0.0 won, 1.0 last.

| | Cell | Drops | Avg finish | Avg kills | Avg survival |
|---|---|---|---|---|---|
| deadliest | 2_12 | 382 | **0.654** | 0.77 | 534 s |
| deadliest | 9_14 | 271 | 0.612 | 0.15 | 699 s |
| safest | 2_4 | 263 | 0.349 | 0.88 | 1,086 s |
| safest | 12_8 | 220 | **0.335** | 0.84 | 1,004 s |

A **32 percentile-point** spread, and survival doubles across it. Note the kills column: the
deadliest cells produce *fewer* kills, not more. Hot drops don't earn you fights — they end your
match sooner.

### Zone luck is small, and points the other way

Distance from the drop point to the **second** circle, in radii. (Phase 1's circle averages 5,485 m
on an 8 km map — everyone is inside it, so it can't measure luck at all.)

| Drop position | Player-matches | Avg finish | Avg survival | Distance travelled |
|---|---|---|---|---|
| deep inside | 221,888 | 0.5133 | 650 s | 1,947 m |
| inside | 540,065 | 0.5228 | 635 s | 2,099 m |
| just outside | 526,075 | 0.5139 | 667 s | 2,502 m |
| one radius out | 339,076 | 0.4954 | 721 s | 2,993 m |
| far outside | 281,793 | **0.4815** | 780 s | 3,593 m |

Landing **far** from the circle is associated with a **better** finish — ~3 percentile points — not
worse. Plausible reading: landing inside the circle means landing where everyone else is headed.
Caveat: drop choice isn't independent of the circle, so this bounds the association rather than
isolating a cause.

### The match arc

| Phase | Alive | Zone radius | Deaths/min | Median engagement |
|---|---|---|---|---|
| 1 | 86 | 5,636 m | **4.87** | 16 m |
| 2 | 48 | 1,886 m | 3.30 | 28 m |
| 6 | 11 | 254 m | 2.14 | 49 m |
| 9 | 5 | 73 m | **8.40** | 32 m |

A U-shaped violence curve, and engagement distance that rises as the game opens then collapses as
the circle squeezes.

### Movement, after three confounds

`mart_player_rotation` produced confident nonsense three times before it was right:

1. **Position logging starts on the aircraft.** Mean altitude in the first 60 s is **574 m** across
   22.9M ticks, so naive distance÷time gave **45 m/s — 162 km/h** for early deaths.
2. **The zone radius collapses 5,637 m → 110 m**, so distance÷radius measured *survival*, not zone
   discipline.
3. **Redeploys put players back in the air.** A range test added to guard against (1) caught 6
   players at 64–76 m/s — all with 2–4 parachutes and steps of 257–787 m/s. **It failed on the very
   next build**, which is the entire argument for writing the guard.

With survival held roughly fixed (squad, alive 300–600 s), players who spent ~29% of that window
outside the circle finished **3.3 points worse with less than half the kills** (0.36 vs 0.78).
The measure is zero-inflated — half of players are *never* outside the zone — so the honest contrast
is the top quartile against everyone else, not four tidy groups.

---

## Skill rating and prediction

Plackett-Luce over human-relative team placements, run as a **sequential fold in Python, not SQL** —
match N's update depends on the state match N−1 left behind. Every update carries pre- *and*
post-state, which is what makes point-in-time correctness enforceable rather than remembered. A dbt
test asserts `mu_pre` equals the previous `mu_post` across each player's history.

### What leakage actually buys

| model | log loss | Brier | AUC | ECE |
|---|---|---|---|---|
| base rate | 0.69307 | 0.24996 | 0.5000 | 0.00192 |
| point-in-time | 0.67822 | 0.24295 | **0.5818** | 0.01306 |
| leaked (`ordinal_post`) | 0.64045 | 0.21637 | **0.7863** | 0.15665 |

The leaked model scores far better on AUC while being **badly miscalibrated** (ECE 0.157 vs 0.013).
Ranking and honesty are different axes, which is why this harness reports both.

Earlier, at 3,556 matches, the leak was worth **+0.41 AUC**. At 35,538 it's **+0.204**. The gap
*halved* as real signal appeared:

> **Leakage looks most impressive exactly when your legitimate features are weakest.** A model with
> no real signal plus a leak looks like a triumph.

### The headline was half noise

| Rating history | Test rows | AUC (rating alone) |
|---|---|---|
| **0 — prior only** | **257,847** | **0.5000** |
| 1–4 | 144,143 | 0.5454 |
| 5–9 | 22,715 | 0.6139 |
| 10–29 | 22,384 | 0.7096 |
| **30+** | **30,376** | **0.7366** |

Half the test set has *no* history, so its rating is a constant and its AUC is exactly 0.5000. Among
players with 30+ rated games a single feature reaches **0.74** — and the plateau lands at ~10–30
matches, precisely where the synthetic study predicted before this data existed.

**Two results recorded rather than buried:** a plain historical average (`prior_mean_finish_pct`)
*beats* the Plackett-Luce ordinal at every history level (0.759 vs 0.737 at 30+) — the Bayesian
machinery isn't yet earning its complexity. And `finished_top_half` is inflated in small human
fields, because with three human teams the percentiles are 0.0/0.5/1.0 and `<= 0.5` captures two of
three.

---

## Estimator study: measuring the methods

`just study` runs five studies against synthetic data with known truth — the one thing real data
*cannot* do, because you can only ask "how wrong is this estimator" when you already know the answer.

**How much evidence a rate needs.** A 200-observation cell still carries a 90th-percentile error of
**5.5 pp**. That's the number to quote when someone ranks matchups from sparse cells.

**It found a bug in this repo.** The engagement matrix used a shrinkage prior of k=200, chosen by
feel:

| prior weight | overall RMSE | tiny | large |
|---|---|---|---|
| 0 | 0.1600 | 0.2354 | 0.0179 |
| **20** | **0.0748** | 0.0988 | 0.0179 |
| 200 | 0.0973 | 0.1141 | 0.0298 |

k=200 was worse than k=20 in **every** stratum. The fix wasn't a better guess: for a prior centred
on p with true-rate variance s², `k = p(1−p)/s² − 1`, and s² is estimable from the data. On the real
cells that gives **k = 8**. Cost of the original mistake: a 896-observation estimate reported as
**0.817 instead of 0.883** — the over-shrinkage distorted the *best-evidenced* cells most.

**Three more numbers.** An unmeasured confounder opened a **34.8 pp** gap between two
identically-lethal weapons that stratifying closed to 0.3 pp. A leaked post-match rating bought
**+0.038 AUC** on synthetic data. And treating 9 correlated phases per match as independent
understates standard errors by **2.17×**, leaving intervals 54% too narrow — which is how a null
result gets reported as significant.

---

## Engine choice, measured

The roadmap assumed the 231M-row position layer was where one machine gives up. That deserved
measuring, because *"I used Spark on data that fit in RAM"* is a tell.

| query (DuckDB, one laptop) | wall |
|---|---|
| full scan count | 0.02 s |
| grid heatmap (bucket + aggregate) | 0.45 s |
| rotation distance (window over every row) | 29.94 s |
| kills × positions ±10 s range join → **269,741,806 pairs** | **2.85 s** |

A 270-million-pair range join in under three seconds, and the skew that was supposed to hurt isn't
there: pairs per match run min 4, avg 8,579, max 18,647 — **max/avg of 2.2×**.

**Spark isn't warranted at this scale.** But running it anyway ([`scripts/spark_delta_exercise.py`](scripts/spark_delta_exercise.py))
overturned the prediction:

| engine | wall | steps |
|---|---|---|
| DuckDB | 25.32 s | 136,916,051 |
| **Spark** (local, 8 GB) | **16.30 s** | 136,916,051 |

**Spark was 1.6× faster** on a high-cardinality window, with identical step counts. Two limits on
that: it's *one query shape*, and wall time excludes a 300 MB JDK, JAR downloads, an 8 GB driver and
~15 s of startup — overhead that dwarfs a 9-second win here.

Delta features exercised: WRITE → DELETE → `versionAsOf=0` (history survived) → MERGE → OPTIMIZE
with **active files 22 → 1**. One measurement bug worth keeping: the first OPTIMIZE check globbed
the directory and reported 24 → **25** files, implying compaction made things worse. OPTIMIZE
*tombstones* originals until VACUUM — the filesystem and the transaction log disagree by design, and
only the log is the table.

---

## The collector that ran perfectly and collected nothing

An audit found **11 invocations, 0 errors**, each reporting
`{"discovered": 0, "attempted": 0, "queued": 0}`. `/samples` returns a pool that refreshes only
periodically, so once the queue drained the collector spent every run rediscovering ids it already
had.

The fix — walking player histories — worked immediately, and created the opposite problem.
Discovery runs at ~43k matches per invocation; fetching manages ~1,500. Since **PUBG deletes matches
after 14 days**, an unbounded queue converts discovery directly into expired rows. So discovery now
pauses above a depth the fetcher can drain:

```
cohort paused: 24248+ already pending
```

> **A pipeline stage 50× cheaper than the one below it will bury it.** Rate-matching is a design
> decision, and here the cost of getting it wrong is measured in expired data.

### And one corruption bug

All 3,663 cloud-collected matches failed to shred. Failing at the *first byte* meant format, not
content:

```
local-written:  one gunzip → b'[{"_T":"LogM'      (JSON)
cloud-written:  one gunzip → b'\x1f\x8b\x08...'   (gzip again)
```

PUBG's CDN serves gzip regardless of `Accept-Encoding`. `httpx` decompresses transparently; `urllib`
does not — so the stdlib-only Lambda gzipped already-gzipped bytes. That is the direct cost of
dropping `httpx` to keep the deployment package dependency-free, and it hid for days because the
output was *valid gzip of a plausible size*. Manifests were unaffected (plain JSON endpoint), which
is why the fault surfaced in exactly one of the two writes.

---

## Stack

**Collection** Python 3.13 · httpx · orjson · Pydantic · Polars
**Warehouse** DuckDB · dbt-core · Parquet · dbt-utils
**Modelling** OpenSkill (Plackett-Luce) · scikit-learn
**Cloud** AWS Lambda · S3 · DynamoDB · EventBridge · SSM · OpenTofu/Terraform
**Also** PySpark 4 + Delta Lake 4 (as a labelled exercise) · pytest · ruff · just

---

## Roadmap

| Phase | Status |
|---|---|
| Collector, local + cloud | ✅ |
| Bronze shredder — 47 event types → 8 typed tables | ✅ |
| Silver — dedup, typing, integrity flags | ✅ |
| Gold — conformed dims, phase-grain facts | ✅ |
| Skill ratings — Plackett-Luce, PIT-tested | ✅ |
| Prediction + calibration harness | ✅ |
| Balance marts | ✅ |
| Estimator study vs synthetic truth | ✅ |
| Position layer + engine benchmark | ✅ |
| **Lambda shreds to Parquet in S3** — one copy of the truth instead of two | next |
| Induce and fix real shuffle skew | waits on ~300k matches |

---

## A note on what this repo is

The findings above are real, but the more transferable content is the **method**: every headline
number here survived an attempt to break it, and the ones that didn't survive are documented next to
the ones that did.

Seventeen bugs were found building this. Roughly two-thirds were mine — a wrong shrinkage prior, a
team-rank divided by a player count, an `is_bot(None)` returning `False`, a relative path inside a
view, a double-gzip from a dependency I removed on purpose. Each is written up where it happened,
because a pipeline's trustworthiness is not the absence of mistakes but the presence of the tests
that caught them.
