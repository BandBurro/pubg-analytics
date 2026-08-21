"""Placement prediction, and the harness that decides whether to believe it.

The model here is deliberately plain — regularised logistic regression on a
handful of features. The interesting part is the evaluation, because a matchmaking
system needs probabilities that are *true*, not merely well-ordered. A model with
0.62 AUC and honest probabilities is more useful than one at 0.68 that is
systematically overconfident, and AUC cannot tell those apart.

So this reports log loss, Brier score, expected calibration error and a
reliability curve, and it compares against two references:

* **base rate** — predicting the training-set average every time. Any model that
  cannot beat this has learned nothing.
* **a deliberately leaked model** — identical except it uses the rating computed
  *after* each match. Its score is the size of the trap, measured on real data
  rather than argued about.

Two splitting rules that matter more than the model:

* **Split by time, not at random.** A random split lets the future inform the
  past, which is the same leak the feature store is built to prevent.
* **Split by match, not by row.** Players in one match share its outcome, so
  scattering them across train and test leaks too.
"""

import math
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

MODEL_VERSION = "logistic.v1"

NUMERIC_FEATURES = [
    "ordinal_pre",
    "sigma_pre",
    "games_played_before",
    "teammate_mean_ordinal_pre",
    "prior_mean_finish_pct",
    "prior_matches",
    "human_count",
    "team_size",
]

FEATURE_QUERY = """
select
    f.feature_sk,
    f.match_id,
    f.account_id,
    f.match_start_ts,
    f.ordinal_pre,
    f.sigma_pre,
    f.games_played_before,
    f.teammate_mean_ordinal_pre,
    f.prior_mean_finish_pct,
    f.prior_matches,
    f.human_count,
    f.team_size,
    f.perspective,
    f.finished_top_half,
    -- Fetched only to build the leaked reference model. Never a real feature.
    r.ordinal_post
from main_gold.feat_player_match f
left join main_gold.fact_rating_update r
    on f.match_id = r.match_id and f.account_id = r.account_id
order by f.match_start_ts, f.match_id
"""


@dataclass
class Metrics:
    name: str
    n: int
    log_loss: float
    brier: float
    auc: float
    ece: float
    reliability: list[dict] = field(default_factory=list)


def expected_calibration_error(
    y_true: list[int], y_prob: list[float], bins: int = 10
) -> tuple[float, list[dict]]:
    """Mean gap between predicted and observed frequency, weighted by bin size.

    This is the number that says whether "70% likely" happens 70% of the time.
    The curve is returned alongside it because a single number hides *where* a
    model is overconfident, which is usually the actionable part.
    """
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(y_prob, y_true, strict=True):
        idx = min(bins - 1, int(p * bins))
        buckets[idx].append((p, y))

    total = len(y_prob)
    ece = 0.0
    curve = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        mean_pred = sum(p for p, _ in b) / len(b)
        observed = sum(y for _, y in b) / len(b)
        ece += (len(b) / total) * abs(mean_pred - observed)
        curve.append(
            {
                "bin": f"{i / bins:.1f}-{(i + 1) / bins:.1f}",
                "n": len(b),
                "mean_predicted": round(mean_pred, 4),
                "observed_rate": round(observed, 4),
                "gap": round(observed - mean_pred, 4),
            }
        )
    return ece, curve


def score(name: str, y_true: list[int], y_prob: list[float]) -> Metrics:
    ece, curve = expected_calibration_error(y_true, y_prob)
    # AUC is undefined for a constant predictor, which the base rate is.
    try:
        auc = roc_auc_score(y_true, y_prob)
    except ValueError:
        auc = float("nan")
    return Metrics(
        name=name,
        n=len(y_true),
        log_loss=round(log_loss(y_true, y_prob, labels=[0, 1]), 5),
        brier=round(brier_score_loss(y_true, y_prob), 5),
        auc=round(auc, 4) if not math.isnan(auc) else float("nan"),
        ece=round(ece, 5),
        reliability=curve,
    )


def load_features(warehouse: Path) -> pl.DataFrame:
    con = duckdb.connect(str(warehouse), read_only=True)
    df = con.execute(FEATURE_QUERY).pl()
    con.close()
    return df


def temporal_split(df: pl.DataFrame, holdout_frac: float = 0.25) -> tuple:
    """Split by match and by time — never by row, never at random.

    Cutting at a match boundary keeps every player in a match on the same side of
    the split, so a match's shared outcome cannot leak across it.
    """
    matches = (
        df.select("match_id", "match_start_ts")
        .unique(subset=["match_id"])
        .sort("match_start_ts", "match_id")
    )
    cut = int(len(matches) * (1 - holdout_frac))
    train_ids = set(matches["match_id"][:cut].to_list())
    is_train = df["match_id"].is_in(train_ids)
    return df.filter(is_train), df.filter(~is_train)


def _matrix(df: pl.DataFrame, features: list[str]) -> list[list[float]]:
    """Impute missing values with an explicit indicator column per feature.

    A null rating means "this player has no history", which is information. Filling
    it silently with a median throws that away; filling it *and* flagging it keeps
    both the value and the fact that it was missing.
    """
    cols = []
    for f in features:
        s = df[f].cast(pl.Float64)
        med = s.median()
        cols.append(s.fill_null(med if med is not None else 0.0).to_list())
        cols.append([1.0 if v is None else 0.0 for v in s.to_list()])
    cols.append([1.0 if p == "fpp" else 0.0 for p in df["perspective"].to_list()])
    return [list(row) for row in zip(*cols, strict=True)]


def fit_and_score(
    train: pl.DataFrame, test: pl.DataFrame, features: list[str], name: str
) -> tuple[Metrics, list[float]]:
    x_train = _matrix(train, features)
    x_test = _matrix(test, features)
    y_train = [int(v) for v in train["finished_top_half"].to_list()]
    y_test = [int(v) for v in test["finished_top_half"].to_list()]

    model = Pipeline(
        [
            ("scale", StandardScaler()),
            ("lr", LogisticRegression(max_iter=1000, C=1.0)),
        ]
    )
    model.fit(x_train, y_train)
    probs = [float(p) for p in model.predict_proba(x_test)[:, 1]]
    return score(name, y_test, probs), probs


def run(warehouse: Path, out_dir: Path) -> dict:
    df = load_features(warehouse)
    train, test = temporal_split(df)

    results: list[Metrics] = []

    # Reference 1: the training base rate, predicted for everyone.
    base = float(sum(train["finished_top_half"].cast(pl.Int8).to_list())) / len(train)
    y_test = [int(v) for v in test["finished_top_half"].to_list()]
    results.append(score("base rate", y_test, [base] * len(y_test)))

    # The real model.
    honest, probs = fit_and_score(train, test, NUMERIC_FEATURES, "point-in-time model")
    results.append(honest)

    # Reference 2: the same model with the post-match rating swapped in.
    leaky_features = ["ordinal_post"] + [f for f in NUMERIC_FEATURES if f != "ordinal_pre"]
    leaky, _ = fit_and_score(train, test, leaky_features, "leaked model (ordinal_post)")
    results.append(leaky)

    out_dir.mkdir(parents=True, exist_ok=True)
    preds = test.select("feature_sk", "match_id", "account_id", "match_start_ts").with_columns(
        pl.Series("predicted_top_half_prob", probs),
        pl.Series("actual_top_half", y_test),
        pl.lit(MODEL_VERSION).alias("model_version"),
    )
    path = out_dir / "fact_match_prediction.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    preds.write_parquet(tmp, compression="zstd")
    tmp.replace(path)

    return {
        "train_rows": len(train),
        "test_rows": len(test),
        "train_matches": train["match_id"].n_unique(),
        "test_matches": test["match_id"].n_unique(),
        "base_rate": round(base, 4),
        "results": results,
        "prediction_path": str(path),
    }
