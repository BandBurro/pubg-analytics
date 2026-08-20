-- The point-in-time guarantee, asserted in SQL.
--
-- A player's rating going into match N must be exactly the rating they carried
-- out of match N-1. If this ever drifts, every feature built on `ordinal_pre` is
-- quietly leaking the future into the past — the single most common way a model
-- looks excellent offline and fails in production.

with chained as (

    select
        account_id,
        match_id,
        rating_system,
        mu_pre,
        sigma_pre,
        lag(mu_post) over w    as prev_mu_post,
        lag(sigma_post) over w as prev_sigma_post
    from {{ ref('fact_rating_update') }}
    window w as (
        partition by account_id, rating_system
        order by match_start_ts, match_id
    )

)

select *
from chained
where prev_mu_post is not null
  and (
        abs(mu_pre - prev_mu_post) > 1e-9
     or abs(sigma_pre - prev_sigma_post) > 1e-9
  )
