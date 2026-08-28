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

## Phase 1.5: the cloud collector

Collection had three single points of failure, all of them the laptop: sleep,
changing networks, and a corporate firewall doing TLS inspection. Matches age out
of the PUBG API after **14 days**, so uptime is not a nicety — it is the difference
between having data and not.

`infra/` provisions the whole thing as code: S3 for the lake, DynamoDB for the
ledger, a Lambda on a 2-hour EventBridge schedule, the API key in an SSM
SecureString, log retention, budget alerts and an error alarm.

### Two design decisions worth the words

**The Lambda has zero dependencies.** `orjson` and `httpx` ship compiled wheels,
so running them on Lambda's ARM Linux runtime from a macOS laptop means
cross-compilation, a layer, or a container image. None of that buys anything for a
function that fetches JSON and writes bytes — so it uses `urllib` and `json` from
the standard library, with `ThreadPoolExecutor` for concurrency instead of asyncio.
The result is a ~10 KB zip and no build step. (`boto3` is already in the runtime.)

**State moves from SQLite to DynamoDB.** Lambda's filesystem is ephemeral and not
shared between invocations, so a local ledger file would forget everything each
run. A GSI on `status` lets it query oldest-pending rather than scan the table —
with the caveat, documented in `infra/README.md` rather than discovered later, that
a four-value partition key concentrates writes and would need sharding at millions
of items.

### Cost, decided rather than discovered

| Choice | Why |
|---|---|
| **No VPC, so no NAT Gateway** | ~$32/month to do nothing. The classic surprise bill |
| SSM Parameter Store, not Secrets Manager | Free vs $0.40/secret/month for the same job |
| arm64 | ~20% cheaper per GB-second, and the workload is IO-bound anyway |
| Explicit log retention | CloudWatch logs default to *never* expiring |
| Budget alerts at 25 / 80 / forecast-100% | The forecast alert catches a runaway before it costs anything |

Realistically under **$2/month**, trending up with S3 as the lake grows. `tofu
destroy` stops all charges — which is the actual reason this is code rather than
clicked together in a console.

### What's yours to do

Creating an AWS account and handling credentials aren't things I can do. The
prerequisites and the four commands are in [`infra/README.md`](infra/README.md).
Note that `var.aws_profile` has no default on purpose, so a stray `default`
profile — possibly a work account — can never be picked up by accident.

## The scale test: was AUC 0.54 a data problem?

The corpus went from 3,556 matches to **31,875** — 1.76M rating updates, 273,721
players with rating history, up from 10,698.

| model | AUC at 3,556 matches | AUC at 31,875 |
|---|---|---|
| point-in-time | 0.5412 | **0.5872** |
| leaked (`ordinal_post`) | 0.9516 | **0.7956** |
| **leakage gap** | **+0.410** | **+0.208** |

The honest model improved. The *leaked* model got much worse — which is the
prediction from Phase 5 confirmed: leakage looks most impressive exactly when the
legitimate features are weakest, so as real signal appears, the leak's marginal
contribution collapses. It is also now badly miscalibrated (ECE 0.162 against
0.010), scoring far better on AUC while lying about its probabilities.

### The headline was 50% noise

Segmenting the test set by how much history each player actually had:

| Rating history | Test rows | AUC (rating alone) | AUC (prior form) |
|---|---|---|---|
| **0 — prior only** | **220,389** | **0.5000** | n/a |
| 1–4 | 141,362 | 0.5474 | 0.5539 |
| 5–9 | 24,430 | 0.6202 | 0.6354 |
| 10–29 | 23,882 | 0.7111 | 0.7325 |
| **30+** | **30,512** | **0.7414** | **0.7657** |

Half the test set has *no* history, so its rating is a constant and its AUC is
exactly 0.5000. Among players with 30+ rated games a **single feature** reaches
**0.74** — and the plateau lands at ~30 matches, which is precisely what the
synthetic convergence study predicted before any of this data existed.

**So: a data problem, decisively.** Not the engine, not the features.

### Two things this also exposed

**A plain historical average beats the rating system.** `prior_mean_finish_pct` —
"how has this player finished before" — outscores the Plackett-Luce ordinal at
*every* history level (0.766 vs 0.741 at 30+). The team-aware Bayesian machinery
is not yet earning its complexity over a running mean. That may change with more
depth per player, but as of now it is the honest result.

**The `finished_top_half` label misbehaves in small human fields.** Top-half rate
by history bucket runs 0.516 → 0.412, which looks like experienced players
finishing *worse*. It is an artifact: with only three human teams the percentiles
are 0.0, 0.5, 1.0, and `<= 0.5` captures two of three. Bot-heavy lobbies have tiny
human fields, so their label is inflated. A percentile target, or a minimum
human-team count, would remove it.

## Phase 7: measuring before reaching for Spark

The roadmap assumed the position layer was where a single machine gives up. That
assumption deserved measuring, because *"I used Spark on data that fit in RAM"* is
a tell, and knowing when **not** to reach for it is the more useful judgement.

The position stream shredded to **207,508,882 rows in 3.0 GB** of Parquet across
31,875 matches. On one laptop, via DuckDB (`scripts/bench_positions.py`):

| query | wall |
|---|---|
| full scan count | 0.02 s |
| group by match (31,875 groups) | 0.08 s |
| grid heatmap (bucket + aggregate) | 0.45 s |
| **rotation distance** (window over every row, per player per match) | **29.94 s** |
| **kills × positions, ±10 s range join** → 269,741,806 pairs | **2.85 s** |

A 270-million-pair range join in under three seconds. And the skew that was
supposed to make this painful isn't there: pairs per match run min 4, avg 8,579,
max 18,647 — **max/avg of 2.2×**, nowhere near a hot partition.

**Verdict: Spark is not warranted at this scale.** Not "not yet convenient" —
genuinely not needed. The hardest operation in the project takes 30 seconds on a
machine that costs nothing to run.

### Where the crossover actually is

Extrapolating from the 30-second window scan, and at ~6,510 position rows per
match:

| corpus | position rows | est. hardest query |
|---|---|---|
| 31,875 matches (today) | 207M | 30 s |
| ~300k matches | ~2B | ~5 min |
| ~3M matches | ~20B | ~50 min, plus memory pressure |

So distributed compute starts to earn its keep somewhere around **300k–3M
matches** — 10× to 100× the current corpus. The cloud collector runs 800 matches
per invocation, twelve times a day: **roughly a month of uptime reaches the lower
bound.** Which makes this a deferral with a date, not an excuse.

The genuine finding from the benchmark isn't about Spark at all: **22.7 million
kilometres** of player movement across 136.9M measured steps.

## The position layer, and three confounds it hid

207M rows became answerable via `slv_player_position` (a **view** — nothing queries
it at row grain, so a 3 GB copy would buy nothing) and `mart_player_rotation`
(match × player, 1.79M rows). The first version of that mart produced confident
nonsense three separate times.

**1. Position logging starts on the aircraft.** Mean altitude in the first 60 s is
**574 m**, max 1,506 m, across 22.9M ticks. Naive distance ÷ time gave **45 m/s —
162 km/h** — for players who died early, because their whole tracked window was
flight. Fixed by counting only positions at or after each player's own landing.

**2. The safe-zone radius collapses 5,637 m → 110 m** across phases, a 51× shrink.
So distance-from-centre ÷ radius measured *survival*, not zone discipline — it
inflated for anyone who lasted into the small circles. Replaced with absolute
distance plus a scale-free "was outside the zone" share.

**3. Redeploys put players back in the air mid-match.** After fixing (1), a range
test still caught **6 players at 64–76 m/s** — every one with 2–4 parachute
landings and single steps of 257–787 m/s. Fixed with a physical bound (nothing
drivable in PUBG exceeds ~40 m/s), with the excluded steps *counted* in
`implausible_steps`, because a silent filter is how a known artifact becomes an
unknown one.

That third one was caught by a test I added specifically to guard against the
first: `mean_speed_mps` between 0 and 60. It failed on the very next build.

### What the data says once it's clean

Speeds now land at **1.26–3.65 m/s** — a person walking and running.

The naive view still can't separate cause from effect: surviving longer both
enables movement and results from it. So holding survival roughly fixed —
squad players alive 300–600 s — and splitting by time spent outside the safe zone:

| Outside-zone quartile | Players | Share outside | Avg finish | Kills |
|---|---|---|---|---|
| 1–2 | 120,330 | 0.000 | 0.573 | 0.78 |
| 3 | 60,164 | 0.010 | 0.566 | 0.79 |
| **4** | 60,164 | **0.292** | **0.605** | **0.36** |

Players who spent ~29% of the same survival window outside the circle finished
**3.3 percentage points worse and got less than half the kills**. Being caught in
the gas means running, not fighting.

Note the measure is **zero-inflated** — about half of all players are *never*
outside the zone, so quartiles 1 and 2 are the same group split arbitrarily. The
real contrast is Q4 against everyone else, and saying so is more honest than
presenting four tidy quartiles.

### One portability bug worth recording

Making `slv_player_position` a view exposed something subtle: the dbt source used
a **relative** external path. That is harmless for materialised models, where the
path is read once at build time, but a view re-resolves it on **every query** — so
it worked from `dbt/` and failed from everywhere else. Now baked absolute via
`PUBG_BRONZE`.

## Spark and Delta: a labelled exercise that overturned my own prediction

`scripts/spark_delta_exercise.py` runs local PySpark 4.2 with Delta Lake 4.4 on
the same position data. It is explicitly **not** the recommended path — the
benchmark above showed DuckDB is sufficient at this scale. It exists because the
API and Delta semantics transfer everywhere, and because running both and
reporting the numbers beats assuming.

### The prediction was wrong

I expected Spark to lose on one machine. It didn't:

| engine | wall | steps computed |
|---|---|---|
| DuckDB | 25.32 s | 136,916,051 |
| **Spark (local[*], 8 GB)** | **16.30 s** | 136,916,051 |

**Spark was 1.6× faster**, and both engines returned an identical step count, so
the comparison is real rather than two different questions. The query is a window
partitioned by (match, player) — very high cardinality — and Spark's
shuffle-and-sort across 64 partitions parallelises that better than I assumed.

Two things that stops short of proving:

- It is **one query shape**. DuckDB did the 270-million-pair range join in 2.85 s;
  Spark was never measured on that, so no claim is made either way.
- **Total cost isn't wall time.** Spark needed a 300 MB JDK, a JAR download, an
  8 GB driver, a shuffle-partition decision, and ~15 s of session startup. DuckDB
  needed `import duckdb`. At this scale that overhead dwarfs a 9-second win.

The lesson isn't "Spark is faster" or "DuckDB is faster" — it's that engine choice
is measurable, and the measurement disagreed with the person making the guess.

### What Delta actually buys

| Operation | Result |
|---|---|
| `WRITE` | 1,474,504 rows → v0 |
| `DELETE` in-vehicle rows | 1,328,549 rows → v1 |
| read `versionAsOf=0` | **1,474,504 rows — history survived the delete** |
| `MERGE` 5,000 rows | 1,333,549 rows → v2 |
| `OPTIMIZE` | **active files 22 → 1** → v3 |

Time travel, ACID upserts, and compaction over object storage — the things plain
Parquet cannot do, and the actual content of the word "lakehouse".

### A measurement bug worth keeping

The first version of the OPTIMIZE check counted `*.parquet` on disk and reported
**24 → 25 files** — implying compaction made things *worse*. It hadn't: OPTIMIZE
writes compacted files and **tombstones** the originals, which stay on disk until
`VACUUM` runs. Reading `describe detail` for active files shows the real answer,
22 → 1.

Globbing a Delta directory measures the filesystem. The transaction log is the
table. They disagree by design, and only one of them is the answer.

## The collector that ran perfectly and collected nothing

An audit found the cloud collector had made **11 invocations with 0 errors** and
was reporting, every single time:

```
{"discovered": 0, "attempted": 0, "queued": 0}
```

Healthy, scheduled, permissioned, and completely idle. `/samples` returns a pool
that only refreshes periodically, so once the queue drained the collector spent
every run rediscovering ids it already had. Nothing alarmed, because nothing was
wrong — a function doing nothing looks identical to a function with nothing to do.

This also corrected a projection given earlier: 800 matches × 12 runs/day ≈ 9,600
per day assumed a full queue every run. Actual throughput was ~2,600/day and
falling to zero.

### The fix, and the fix's problem

Ported player-history expansion into the Lambda — the same mechanism that took the
local corpus from 3,556 to 31,875. Matches name their participants; a sample of
those accounts goes into a `players` table; later runs call `/players` to pull each
account's own match history. Matches yield players, players yield matches.

It could not start itself. The players table fills from fetched matches, and
matches only get fetched once something is queued — an empty table plus a stale
`/samples` is a permanent standstill. `scripts/seed_cloud_cohort.py` bootstraps it
from the 900k accounts the local warehouse already knows.

The first run after seeding:

```
{"discovered": 1009, "players_expanded": 50, "cohort_discovered": 43291,
 "attempted": 800, "queued": 800, "ok": 800}
```

**50 players yielded 43,291 matches — 43× what `/samples` found in the same run.**

And that immediately created the opposite problem. Discovery runs at ~43k per
invocation; fetching manages ~1,500. With 450 seeds still unexpanded the queue
would have grown to hundreds of thousands of ids — every one of which **expires
after 14 days**, converting discovery directly into `gone` rows rather than data.

### Backpressure

Discovery now pauses above a queue depth the fetcher can actually drain:

```
cohort paused: 24248+ already pending
```

`/samples` still runs every invocation (one cheap call), cohort expansion only when
there is room, and `MAX_FETCH` went 800 → 1,500 after measuring that 800 used just
354s of the 900s ceiling.

The general lesson is not about PUBG: **a pipeline stage that is 50× cheaper than
the stage below it will happily bury it.** Rate-matching is a design decision, and
here the cost of getting it wrong is measured in expired data rather than a
backlog.

## Roadmap

| Phase | What |
|---|---|
| 1 | Collector ✅ — running on a 2h schedule |
| 1.5 | Cloud collector live: Lambda + S3 + DynamoDB, cohort discovery, backpressure ✅ |
| 2 | Bronze → typed Parquet: 8 tables, 1.53M rows ✅ |
| 2.5 | Silver in dbt: 8 models, 35 tests, dedup + integrity flags ✅ |
| 3 | Gold: 3 dims, 3 facts, 91 dbt nodes ✅ |
| 4 | Ratings: Plackett-Luce fold, PIT-tested, + player-cohort collector ✅ |
| 5 | Prediction + calibration harness; found the placement-scale bug ✅ |
| 6 | Balance marts: engagement matrix, drop zones, zone luck ✅ |
| 6.5 | Estimator study — found and fixed a real shrinkage bug ✅ |
| 7 | Position layer: 207M rows, Silver + rotation mart, 3 confounds caught ✅ |
| 7b | Spark + Delta exercise: Spark 1.6x faster on the window query; Delta time travel / MERGE / OPTIMIZE ✅ |
| 7.5 | Induce skew and fix it — waits on 7b; measured skew today is only 2.2x |

## Stack

Python · httpx · DuckDB · dbt · Parquet · OpenSkill · Spark on Databricks Free Edition · Terraform
