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

    win_place                                       as raw_placement,

    -- Placement among humans only. Null for bots: they are not rated entities,
    -- and giving them a rank would imply they are.
    case
        when not is_bot
        then dense_rank() over (
            partition by match_id, is_bot order by win_place
        )
    end                                             as human_placement,

    -- 0.0 = won, 1.0 = finished last among humans. Comparable across lobby sizes.
    case
        when not is_bot and human_count > 1
        then (
            dense_rank() over (partition by match_id, is_bot order by win_place) - 1
        ) / cast(human_count - 1 as double)
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

from joined
