-- One row per participant per match.
--
-- The important column here is `human_placement`. PUBG backfills lobbies with
-- bots, so raw `win_place` is not comparable across matches: finishing 20th in a
-- lobby of 50 bots is a much weaker result than 20th against 99 humans. Any skill
-- rating must consume the human-relative placement, not the raw one.

with src as (

    select * from {{ source('bronze', 'match_player') }}

),

m as (

    select match_id, human_count, player_count, is_analytical
    from {{ ref('slv_match') }}

),

joined as (

    select
        src.*,
        m.human_count,
        m.player_count as lobby_size,
        m.is_analytical as match_is_analytical
    from src
    inner join m using (match_id)

),

ranked as (

    select
        *,
        -- Placement among humans only. Null for bots: they are not rated
        -- entities, and giving them a rank would imply they are.
        --
        -- Note this is a *team*-level rank — in a squad game teammates share a
        -- win_place, so dense_rank collapses them onto one position.
        case
            when not is_bot
            then dense_rank() over (partition by match_id, is_bot order by win_place)
        end as human_placement
    from joined

),

scaled as (

    select
        *,
        -- The count of distinct human finishing positions, i.e. the number of
        -- human teams. This is the correct denominator for a team-level rank.
        max(human_placement) over (partition by match_id) as human_rank_count
    from ranked

)

select
    match_id,
    participant_id,
    account_id,
    player_name,
    is_bot,
    team_id,
    match_is_analytical,
    human_count,
    lobby_size,
    human_rank_count,

    win_place                                       as raw_placement,
    human_placement,

    -- 0.0 = won, 1.0 = finished last among humans. Comparable across lobby sizes
    -- and, critically, across team modes.
    --
    -- Dividing by human_count instead of human_rank_count was a real bug: a
    -- team-level rank over a player-level denominator compresses the scale by
    -- roughly the team size. Solo came out at 0.49, duo 0.27, squad 0.18 — so
    -- pooling modes averaged three different scales together, and 93% of players
    -- appeared to finish in the "top half".
    case
        when not is_bot and human_rank_count > 1
        then (human_placement - 1) / cast(human_rank_count - 1 as double)
    end                                             as human_placement_pct,

    kill_place,
    kills,
    dbnos,
    assists,
    damage_dealt,
    nullif(death_type, '')                          as death_type,
    death_type = 'alive'                            as survived_to_end,
    headshot_kills,
    longest_kill,
    heals,
    boosts,
    revives,
    road_kills,
    team_kills,
    vehicle_destroys,
    weapons_acquired,
    walk_distance,
    ride_distance,
    swim_distance,
    walk_distance + ride_distance + swim_distance   as total_distance,
    time_survived

from scaled
