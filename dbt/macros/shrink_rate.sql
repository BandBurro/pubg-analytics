{% macro shrink_rate(successes, trials, prior_rate, prior_weight=200) %}
{#-
    Empirical-Bayes shrinkage toward a prior rate.

    A matchup seen 12 times might show a 75% win rate. That number is noise, and
    presenting it next to one computed from 12,000 fights implies they carry equal
    weight. Shrinking pulls small samples toward the global rate in proportion to
    how little evidence backs them, so a sparse cell can no longer top a ranking
    on the strength of a coin flip.

    prior_weight is the number of pseudo-observations: at 200, a cell needs ~200
    real fights before its own rate dominates the prior.
-#}
    (
        ({{ successes }} + {{ prior_weight }} * ({{ prior_rate }}))
        / nullif({{ trials }} + {{ prior_weight }}, 0)
    )
{% endmacro %}
