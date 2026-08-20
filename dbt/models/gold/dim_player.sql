-- One row per account ever observed, bots included and flagged.
--
-- Deliberately not a slowly-changing dimension yet: PUBG exposes no per-player
-- rank or region we could version. When Phase 4 produces ratings, the rating
-- history lives in its own fact table keyed by (player, match) — never as mutable
-- columns here, or every historical query silently becomes wrong.

with appearances as (

    select
        mp.account_id,
        mp.is_bot,
        count(*)                                        as matches_played,
        min(m.match_start_ts)                           as first_seen_ts,
        max(m.match_start_ts)                           as last_seen_ts,
        count(*) filter (where m.is_analytical)         as analytical_matches,
        sum(mp.kills)                                   as career_kills,
        sum(mp.damage_dealt)                            as career_damage,
        min(mp.human_placement)                         as best_human_placement,
        count(*) filter (where mp.human_placement = 1)  as wins
    from {{ ref('slv_match_player') }} mp
    inner join {{ ref('slv_match') }} m using (match_id)
    group by 1, 2

)

select
    {{ dbt_utils.generate_surrogate_key(['account_id']) }} as player_sk,
    account_id,
    is_bot,
    matches_played,
    analytical_matches,
    first_seen_ts,
    last_seen_ts,
    career_kills,
    career_damage,
    best_human_placement,
    wins,

    -- Sample-size gate for anything player-level. A single match tells you
    -- essentially nothing, and most accounts here appear exactly once.
    analytical_matches >= 5                             as has_enough_history

from appearances
