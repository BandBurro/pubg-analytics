{{ config(tags=['ratings']) }}

-- Current rating per player: the most recent update's post-state.
--
-- A convenience view over history, never the source of truth. Any question about
-- what a player's rating *was* at some past moment must go to
-- fact_rating_update, or it will silently answer with today's rating.

with latest as (

    select *
    from {{ ref('fact_rating_update') }}
    qualify row_number() over (
        partition by account_id, rating_system
        order by match_start_ts desc, match_id desc
    ) = 1

)

select
    l.player_sk,
    l.account_id,
    l.rating_system,
    l.mu_post                       as mu,
    l.sigma_post                    as sigma,
    l.ordinal_post                  as ordinal,
    l.games_played_before + 1       as games_rated,
    l.match_start_ts                as last_rated_ts,

    -- Sigma stays near its prior until a player has been seen repeatedly, so a
    -- rating without games behind it is a placeholder, not a measurement. The
    -- engine's recovery curve puts a usable rating at roughly 30 matches.
    l.games_played_before + 1 >= 30 as is_converged,

    p.matches_played,
    p.analytical_matches,
    p.career_kills

from latest l
left join {{ ref('dim_player') }} p using (account_id)
