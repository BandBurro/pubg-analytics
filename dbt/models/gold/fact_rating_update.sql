{{ config(tags=['ratings']) }}

-- Grain: one row per rated player per match per rating system.
--
-- Carrying pre- *and* post-state is the whole point. `ordinal_pre` is the only
-- rating a model may use to predict this match's outcome; `ordinal_post` already
-- knows how it turned out. Storing both, explicitly labelled, is what makes
-- point-in-time correctness enforceable rather than a thing you remember to do.

select
    {{ dbt_utils.generate_surrogate_key(['account_id', 'match_id', 'rating_system']) }}
                                            as rating_update_sk,
    {{ dbt_utils.generate_surrogate_key(['account_id']) }} as player_sk,
    account_id,
    match_id,
    cast(match_start_ts as timestamp)       as match_start_ts,
    rating_system,

    team_id,
    team_rank,
    teams_in_match,
    games_played_before,

    -- Knowable before the match. Safe for features.
    mu_pre,
    sigma_pre,
    ordinal_pre,

    -- Knowable only after. Never a feature for this match.
    mu_post,
    sigma_post,
    ordinal_post,
    mu_delta,
    sigma_delta,

    -- A player's first match teaches the system nothing about them yet, so
    -- anything skill-conditioned should require this.
    games_played_before > 0                 as had_prior_history

from {{ source('ratings', 'fact_rating_update') }}
