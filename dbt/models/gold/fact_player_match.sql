-- Grain: one row per player per match.
--
-- This is the table the skill-rating engine consumes. Two columns make that
-- possible and are the reason it exists:
--
--   human_placement     — rank among humans only, because bots occupy placement
--                         slots and raw win_place isn't comparable across lobbies
--   human_kills         — kills against human victims only, because 64% of all
--                         victims are bots and counting them measures farming
--
-- Bots get rows here too (flagged), so lobby composition stays reconstructible.

with human_kills as (

    -- Credited killer only. Counting killer *and* finisher would double-count the
    -- common case where they're the same player.
    select
        kp.match_id,
        kp.account_id,
        count(*)                                as human_kills,
        count(*) filter (where k.killer_distance_m >= 100) as human_kills_long_range
    from {{ ref('slv_kill_participant') }} kp
    inner join {{ ref('slv_kill') }} k using (kill_sk)
    where kp.role = 'killer'
      and k.is_human_vs_human
    group by 1, 2

),

team_size as (

    select match_id, team_id, participant_count
    from {{ ref('slv_roster') }}

),

landing as (

    -- Initial drop only; redeploys are a different tactical event.
    select match_id, account_id, x_m, y_m
    from {{ ref('slv_landing') }}
    where landing_seq = 1

)

select
    {{ dbt_utils.generate_surrogate_key(['mp.match_id', 'mp.account_id']) }}
                                                as player_match_sk,
    -- Degenerate dimensions
    mp.match_id,
    mp.account_id,
    mp.participant_id,

    -- Foreign keys
    {{ dbt_utils.generate_surrogate_key(['mp.account_id']) }}    as player_sk,
    {{ dbt_utils.generate_surrogate_key(['m.map_name']) }}       as map_sk,

    -- Match context, denormalised so the common query needs no joins
    m.match_start_ts,
    m.match_type,
    m.game_mode,
    m.team_mode,
    m.perspective,
    m.map_name,
    m.region,
    m.duration_s                                as match_duration_s,
    m.human_count,
    m.player_count                              as lobby_size,
    m.is_analytical,

    mp.is_bot,
    mp.team_id,
    ts.participant_count                        as team_size,

    -- Outcome
    mp.raw_placement,
    mp.human_placement,
    mp.human_placement_pct,
    mp.human_placement = 1                      as won_match,
    mp.survived_to_end,
    mp.death_type,
    mp.time_survived,

    -- Performance. `kills` is PUBG's own count (bots included); `human_kills` is
    -- the one that means something.
    mp.kills                                    as kills_reported,
    coalesce(hk.human_kills, 0)                 as human_kills,
    coalesce(hk.human_kills_long_range, 0)      as human_kills_long_range,
    mp.kills - coalesce(hk.human_kills, 0)      as bot_kills,
    mp.dbnos,
    mp.assists,
    mp.damage_dealt,
    mp.headshot_kills,
    mp.longest_kill,
    mp.revives,
    mp.heals,
    mp.boosts,
    mp.team_kills,
    mp.weapons_acquired,
    mp.walk_distance,
    mp.ride_distance,
    mp.total_distance,

    -- Drop location, for landing-spot survival analysis
    ld.x_m                                      as landing_x_m,
    ld.y_m                                      as landing_y_m,
    case
        when ld.x_m is not null
        then {{ grid_cell('ld.x_m', 'ld.y_m') }}
    end                                         as landing_grid_cell

from {{ ref('slv_match_player') }} mp
inner join {{ ref('slv_match') }} m using (match_id)
left join team_size ts
    on mp.match_id = ts.match_id and mp.team_id = ts.team_id
left join human_kills hk
    on mp.match_id = hk.match_id and mp.account_id = hk.account_id
left join landing ld
    on mp.match_id = ld.match_id and mp.account_id = ld.account_id
