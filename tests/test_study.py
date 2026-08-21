"""Tests for the estimator studies.

Each study exists to produce a number, so each test asserts the *direction* that
number must have. If shrinkage stopped helping small samples, or a leaked feature
stopped inflating a score, the study would be measuring something other than what
it claims.
"""

from pubg_analytics.study import (
    auc,
    study_cluster_design_effect,
    study_confounded_rate,
    study_leakage_cost,
    study_sample_size_floor,
    study_shrinkage_crossover,
)


def test_auc_is_half_for_random_scores_and_one_for_perfect():
    assert auc([1.0, 2.0, 3.0, 4.0], [0, 0, 1, 1]) == 1.0
    assert auc([4.0, 3.0, 2.0, 1.0], [0, 0, 1, 1]) == 0.0
    # All-tied scores carry no information and must score 0.5, not 1.0.
    assert auc([1.0, 1.0, 1.0, 1.0], [0, 0, 1, 1]) == 0.5


def test_estimate_error_shrinks_as_sample_grows():
    rows = study_sample_size_floor(replicates=400)
    errors = [r["mean_abs_error"] for r in rows]
    assert errors == sorted(errors, reverse=True), "error should fall monotonically with n"
    # A dozen fights tells you almost nothing; thousands pin it down.
    assert rows[0]["within_2pp"] < 0.35
    assert rows[-1]["within_2pp"] > 0.90


def test_shrinkage_helps_small_cells_and_costs_large_ones():
    rows = study_shrinkage_crossover(n_cells=3000)
    by = {(r["prior_weight"], r["stratum"]): r["rmse"] for r in rows}
    tiny = [s for (_, s) in by if s.startswith("tiny")][0]
    large = [s for (_, s) in by if s.startswith("large")][0]

    # On tiny cells, any shrinkage beats the raw rate.
    assert by[(200, tiny)] < by[(0, tiny)]
    # On large cells, heavy shrinkage starts pulling real effects toward 0.5.
    assert by[(1000, large)] > by[(0, large)]


def test_confounding_creates_a_gap_that_stratifying_removes():
    res = study_confounded_rate(n_fights=20_000)
    # The two weapons are identical by construction, yet look far apart.
    assert res["naive_gap_pp"] > 20, res
    # Stratifying by opponent strength collapses the difference.
    assert res["adjusted_gap_pp"] < 2, res


def test_leaked_feature_inflates_the_score():
    res = study_leakage_cost(n_players=60, n_matches=200)
    assert res.observations > 1000
    assert res.auc_leaky > res.auc_correct, "post-match rating must look better"
    assert res.inflation > 0.05, res.inflation
    # The legitimate feature should still be genuinely predictive, not noise.
    assert res.auc_correct > 0.55


def test_ignoring_clustering_understates_standard_errors():
    res = study_cluster_design_effect(n_matches=200, replicates=120)
    assert res["se_understated_by"] > 1.5, res
    assert res["design_effect"] > 2.0, res
