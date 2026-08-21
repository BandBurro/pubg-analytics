{{ config(tags=['ratings']) }}

-- Grain: one row per human player per analytical match. The feature store.
--
-- Every column here must be computable from information available *before the
-- match started*. That is the whole contract, and it is enforced two ways:
--
--   * ratings come from `ordinal_pre`, never `ordinal_post`
--   * career form uses `rows between unbounded preceding and 1 preceding`, so a
--     player's own result never enters their own feature
--
-- The estimator study measured what breaking this buys: +0.038 AUC that does not
-- exist in production. Which is exactly why the label lives in this model too,
-- clearly named, instead of being joined in later by whoever trains on it.

with base as (

    select
        pm.match_id,
        pm.account_id,
        pm.team_id,
        pm.match_start_ts,
        pm.perspective,
        pm.team_mode,
        pm.map_name,
        pm.human_count,
        pm.team_size,
        pm.human_placement_pct,
        r.ordinal_pre,
        r.sigma_pre,
        r.games_played_before
    from {{ ref('fact_player_match') }} pm
    left join {{ ref('fact_rating_update') }} r
        on pm.match_id = r.match_id and pm.account_id = r.account_id
    where pm.is_analytical
      and not pm.is_bot
      and pm.human_placement_pct is not null

),

with_team as (

    select
        *,
        -- Teammates' strength, excluding self. In a squad game the people beside
        -- you are a bigger determinant of placement than your own rating.
        (
            sum(ordinal_pre) over (partition by match_id, team_id)
            - coalesce(ordinal_pre, 0)
        ) / nullif(count(ordinal_pre) over (partition by match_id, team_id) - 1, 0)
            as teammate_mean_ordinal_pre,
        count(*) over (partition by match_id, team_id) as observed_team_size
    from base

),

with_history as (

    select
        *,
        -- Expanding window over strictly earlier matches. The `1 preceding`
        -- bound is what makes this point-in-time correct; without it a player's
        -- own outcome leaks into their own feature.
        avg(human_placement_pct) over w      as prior_mean_finish_pct,
        count(*) over w                      as prior_matches,
        min(human_placement_pct) over w      as prior_best_finish_pct
    from with_team
    window w as (
        partition by account_id
        order by match_start_ts, match_id
        rows between unbounded preceding and 1 preceding
    )

)

select
    {{ dbt_utils.generate_surrogate_key(['match_id', 'account_id']) }} as feature_sk,
    match_id,
    account_id,
    match_start_ts,

    -- ---- features (all knowable before the match) ----
    ordinal_pre,
    sigma_pre,
    games_played_before,
    teammate_mean_ordinal_pre,
    prior_mean_finish_pct,
    prior_matches,
    prior_best_finish_pct,
    human_count,
    coalesce(team_size, observed_team_size)  as team_size,
    perspective,
    team_mode,
    map_name,

    -- ---- label (knowable only after) ----
    human_placement_pct,
    human_placement_pct <= 0.5               as finished_top_half,

    -- Rows with no rating history at all carry no skill signal; a model should be
    -- able to exclude or flag them rather than silently treat the prior as fact.
    coalesce(games_played_before, 0) > 0     as has_rating_history,
    prior_matches > 0                        as has_form_history

from with_history
