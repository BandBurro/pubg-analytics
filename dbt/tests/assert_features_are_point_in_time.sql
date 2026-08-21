-- The feature store's point-in-time contract, asserted rather than assumed.
--
-- Two ways this can break, both silent:
--
--   1. A player's first match shows form history it cannot have.
--   2. The expanding window includes the current row, so a player's own result
--      helps predict their own result. Offline scores improve, production does not.
--
-- Recomputing the window independently and comparing is the only way to catch (2)
-- — the model would look fine either way.

with recomputed as (

    select
        feature_sk,
        prior_matches,
        prior_mean_finish_pct,
        count(*) over w                 as expected_prior_matches,
        avg(human_placement_pct) over w as expected_prior_mean
    from {{ ref('feat_player_match') }}
    window w as (
        partition by account_id
        order by match_start_ts, match_id
        rows between unbounded preceding and 1 preceding
    )

)

select *
from recomputed
where
    -- A first-ever match cannot have prior form.
    (prior_matches = 0 and prior_mean_finish_pct is not null)

    -- The window must match an independent recomputation.
    or coalesce(prior_matches, -1) != coalesce(expected_prior_matches, -1)
    or abs(coalesce(prior_mean_finish_pct, 0) - coalesce(expected_prior_mean, 0)) > 1e-9
