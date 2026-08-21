"""Estimator studies against synthetic ground truth.

Every other phase of this project measures PUBG. This one measures *the methods* —
and it is the one thing real data cannot do, because you can only ask "how wrong
is this estimator" when you already know the right answer.

Five questions, each ending in a number:

1. How many observations before a win-rate estimate means anything?
2. Does shrinkage help, and where does it start over-smoothing real effects?
3. How much bias does an unmeasured confounder introduce? (the MP5K effect,
   reproduced from first principles)
4. What does point-in-time leakage buy a model that isn't real?
5. How wrong are confidence intervals when correlated observations are treated
   as independent?

No numpy: sample sizes here are small enough for the standard library, and one
fewer dependency is one fewer reason for this to stop running.
"""

import math
import random
from dataclasses import dataclass, field

from .ratings import MatchGroup, run_ratings


def _binomial(n: int, p: float, rng: random.Random) -> int:
    return sum(1 for _ in range(n) if rng.random() < p)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return float("nan")
    i = min(len(s) - 1, int(q * len(s)))
    return s[i]


def auc(scores: list[float], labels: list[int]) -> float:
    """Probability a random positive outscores a random negative (Mann-Whitney).

    Ties count as half, which matters here: unrated players share an identical
    prior, and scoring those as wins would flatter the model.
    """
    pos = [s for s, y in zip(scores, labels, strict=True) if y == 1]
    neg = [s for s, y in zip(scores, labels, strict=True) if y == 0]
    if not pos or not neg:
        return float("nan")
    wins = ties = 0
    neg_sorted = sorted(neg)
    for p in pos:
        # Count negatives strictly below, and negatives exactly equal.
        lo = _bisect_left(neg_sorted, p)
        hi = _bisect_right(neg_sorted, p)
        wins += lo
        ties += hi - lo
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def _bisect_left(a: list[float], x: float) -> int:
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _bisect_right(a: list[float], x: float) -> int:
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi) // 2
        if a[mid] <= x:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------- study 1


def study_sample_size_floor(
    true_rate: float = 0.60, replicates: int = 2000, seed: int = 11
) -> list[dict]:
    """How many fights before a matchup win rate is trustworthy?

    Draws `n` fights from a matchup whose true rate we chose, and reports how far
    the estimate typically lands from it. This is the number to quote when someone
    ranks matchups from a table with a dozen observations in some cells.
    """
    rng = random.Random(seed)
    out = []
    for n in (10, 25, 50, 100, 200, 500, 1000, 2000, 5000):
        errors = [abs(_binomial(n, true_rate, rng) / n - true_rate) for _ in range(replicates)]
        out.append(
            {
                "n": n,
                "mean_abs_error": round(_mean(errors), 4),
                "p90_abs_error": round(_quantile(errors, 0.90), 4),
                "within_2pp": round(sum(1 for e in errors if e <= 0.02) / replicates, 3),
                "within_5pp": round(sum(1 for e in errors if e <= 0.05) / replicates, 3),
            }
        )
    return out


# ---------------------------------------------------------------- study 2


def study_shrinkage_crossover(
    n_cells: int = 4000, prior_weights=(0, 50, 200, 500, 1000), seed: int = 23
) -> list[dict]:
    """Does shrinkage help, and where does it start hiding real effects?

    Builds a population of matchup cells with heterogeneous true rates and very
    heterogeneous sample sizes — the realistic case, where a few cells are huge
    and most are tiny. Then compares raw against shrunk estimates by RMSE, split
    by how much evidence each cell actually had.

    This is what justifies (or refutes) the prior weight the production mart uses.
    """
    rng = random.Random(seed)
    cells = []
    for _ in range(n_cells):
        # True rates cluster near 0.5 with a real tail — most matchups are close,
        # a few are lopsided.
        true_rate = min(0.95, max(0.05, rng.gauss(0.5, 0.12)))
        # Sample sizes span orders of magnitude, as observed in the real mart.
        n = max(1, int(math.exp(rng.gauss(3.5, 1.6))))
        wins = _binomial(n, true_rate, rng)
        cells.append((true_rate, n, wins))

    strata = (("tiny (n<25)", 0, 25), ("small (25-99)", 25, 100),
              ("medium (100-499)", 100, 500), ("large (500+)", 500, 10**9))

    out = []
    for k in prior_weights:
        for label, lo, hi in strata:
            errs = []
            for true_rate, n, wins in cells:
                if not (lo <= n < hi):
                    continue
                est = (wins + k * 0.5) / (n + k)
                errs.append((est - true_rate) ** 2)
            if errs:
                out.append(
                    {
                        "prior_weight": k,
                        "stratum": label,
                        "cells": len(errs),
                        "rmse": round(math.sqrt(_mean(errs)), 4),
                    }
                )
    return out


# ---------------------------------------------------------------- study 3


def study_confounded_rate(n_fights: int = 40_000, seed: int = 31) -> dict:
    """The MP5K effect, from first principles.

    Two weapons with *identical* true lethality. One is used mostly against weak
    opponents, the other mostly against strong ones. The naive win rate ranks them
    far apart; stratifying by opponent strength shows them equal.

    This is the synthetic twin of the real finding in this project — the MP5K
    ranked first on raw kills and sixth once bot victims and tutorial matches were
    excluded.
    """
    rng = random.Random(seed)
    p_weak, p_strong = 0.80, 0.30  # win probability vs each opponent type
    mix = {"weapon_a": 0.85, "weapon_b": 0.15}  # share of fights vs weak opponents

    tally: dict[str, dict[str, list[int]]] = {
        w: {"weak": [0, 0], "strong": [0, 0]} for w in mix
    }
    for weapon, weak_share in mix.items():
        for _ in range(n_fights):
            opponent = "weak" if rng.random() < weak_share else "strong"
            p = p_weak if opponent == "weak" else p_strong
            won = rng.random() < p
            tally[weapon][opponent][0] += int(won)
            tally[weapon][opponent][1] += 1

    res = {}
    for weapon, t in tally.items():
        wins = t["weak"][0] + t["strong"][0]
        n = t["weak"][1] + t["strong"][1]
        naive = wins / n
        # Stratified: average the within-stratum rates, weighting strata equally
        # so the estimate no longer reflects who each weapon happened to face.
        adjusted = _mean(
            [t[s][0] / t[s][1] for s in ("weak", "strong") if t[s][1] > 0]
        )
        res[weapon] = {"naive": round(naive, 4), "adjusted": round(adjusted, 4)}

    res["true_rates"] = {"vs_weak": p_weak, "vs_strong": p_strong}
    res["naive_gap_pp"] = round(
        100 * abs(res["weapon_a"]["naive"] - res["weapon_b"]["naive"]), 2
    )
    res["adjusted_gap_pp"] = round(
        100 * abs(res["weapon_a"]["adjusted"] - res["weapon_b"]["adjusted"]), 2
    )
    return res


# ---------------------------------------------------------------- study 4


@dataclass
class LeakageResult:
    auc_correct: float = 0.0
    auc_leaky: float = 0.0
    observations: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def inflation(self) -> float:
        return round(self.auc_leaky - self.auc_correct, 4)


def study_leakage_cost(
    n_players: int = 80, n_matches: int = 600, seed: int = 41
) -> LeakageResult:
    """What does point-in-time leakage buy a model that isn't real?

    Runs the *production* rating engine over a synthetic league, then predicts each
    match result twice: once from the rating known before the match (legitimate),
    once from the rating computed after it (leakage). The gap is what a leaked
    feature would add to an offline score and take away in production.
    """
    rng = random.Random(seed)
    players = [f"p{i:03d}" for i in range(n_players)]
    true_skill = {p: rng.gauss(0, 1) for p in players}

    groups = []
    for m in range(n_matches):
        picked = rng.sample(players, 20)
        rosters = [picked[i * 4 : (i + 1) * 4] for i in range(5)]
        strength = [
            (sum(true_skill[p] for p in r) / 4 + rng.gauss(0, 0.8), i)
            for i, r in enumerate(rosters)
        ]
        strength.sort(reverse=True)
        groups.append(
            MatchGroup(
                f"m{m:04d}", m,
                [(idx, rank + 1, rosters[idx]) for rank, (_, idx) in enumerate(strength)],
            )
        )

    updates = run_ratings(groups)

    # Label: did this player's team finish in the better half of the lobby?
    pre, post, labels = [], [], []
    for u in updates:
        won_half = 1 if u["team_rank"] <= u["teams_in_match"] / 2 else 0
        pre.append(u["ordinal_pre"])
        post.append(u["ordinal_post"])
        labels.append(won_half)

    res = LeakageResult(
        auc_correct=round(auc(pre, labels), 4),
        auc_leaky=round(auc(post, labels), 4),
        observations=len(labels),
    )
    res.notes.append(
        "ordinal_post already encodes this match's result, so its AUC is not a "
        "forecast — it is the answer read back."
    )
    return res


# ---------------------------------------------------------------- study 5


def study_cluster_design_effect(
    n_matches: int = 600, per_match: int = 9, replicates: int = 400, seed: int = 53
) -> dict:
    """How wrong are confidence intervals when clustering is ignored?

    Phases within a match are not independent observations — economy, momentum and
    lobby composition are shared. Treating them as independent shrinks the standard
    error by the square root of the design effect, which is how a null result gets
    published as significant.
    """
    rng = random.Random(seed)
    naive_ses, true_spread = [], []

    for _ in range(replicates):
        obs = []
        for _ in range(n_matches):
            # Match-level random effect shared by every phase in that match.
            match_effect = rng.gauss(0, 1.0)
            for _ in range(per_match):
                obs.append(match_effect + rng.gauss(0, 1.0))
        n = len(obs)
        mean = _mean(obs)
        var = sum((x - mean) ** 2 for x in obs) / (n - 1)
        naive_ses.append(math.sqrt(var / n))  # assumes independence
        true_spread.append(mean)

    # The honest standard error is the actual spread of the estimate.
    m = _mean(true_spread)
    empirical_se = math.sqrt(sum((x - m) ** 2 for x in true_spread) / (len(true_spread) - 1))
    naive_se = _mean(naive_ses)
    design_effect = (empirical_se / naive_se) ** 2

    return {
        "observations_per_replicate": n_matches * per_match,
        "phases_per_match": per_match,
        "naive_se": round(naive_se, 5),
        "empirical_se": round(empirical_se, 5),
        "se_understated_by": round(empirical_se / naive_se, 3),
        "design_effect": round(design_effect, 3),
        "ci_too_narrow_pct": round(100 * (1 - naive_se / empirical_se), 1),
    }
