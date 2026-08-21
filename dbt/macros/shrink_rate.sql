{% macro shrink_rate(successes, trials, prior_rate, prior_weight=10) %}
{#-
    Empirical-Bayes shrinkage toward a prior rate.

    A matchup seen 12 times might show a 75% win rate. That number is noise, and
    presenting it next to one computed from 12,000 fights implies they carry equal
    weight. Shrinking pulls small samples toward the global rate in proportion to
    how little evidence backs them, so a sparse cell can no longer top a ranking
    on the strength of a coin flip.

    prior_weight is the number of pseudo-observations. Do not guess it — for a
    prior centred on p with variance s2, the equivalent sample size is

        k = p(1-p)/s2 - 1

    where s2 is the variance of the *true* rates, estimated by subtracting the
    average binomial sampling variance from the observed variance of the rates.
    On this project's interpretable matchup cells that gives s2 = 0.0279
    (sd 0.167), so k = 8. Rounded to 10 for stability, since the estimate itself
    rests on only 58 cells.

    The first version of this macro defaulted to 200, which the estimator study
    showed was worse than k=20 in *every* sample-size stratum — real matchups
    differ far more than that prior allowed, and shrinking that hard destroys the
    signal it was meant to protect.
-#}
    (
        ({{ successes }} + {{ prior_weight }} * ({{ prior_rate }}))
        / nullif({{ trials }} + {{ prior_weight }}, 0)
    )
{% endmacro %}
