-- Grain: one row per death.
--
-- Enriched with the zone phase and lobby state at the moment it happened, which
-- is what turns a kill log into something you can ask tactical questions of:
-- how lethal is a weapon late in a match, does the circle drive engagements.
--
-- Both time-based joins use ASOF JOIN rather than a BETWEEN range. A range join
-- would fan out wherever two phase events share a timestamp; ASOF is guaranteed
-- to attach exactly one preceding row.

with phases as (

    select
        match_id,
        event_ts,
        max(phase) as phase
    from {{ ref('slv_phase') }}
    group by 1, 2

),

lobby as (

    select match_id, event_ts, num_alive_players, num_alive_teams,
           safety_zone_radius_m
    from {{ ref('slv_game_state') }}

)

select
    k.kill_sk,
    k.match_id,
    k.event_ts,

    -- Foreign keys
    {{ dbt_utils.generate_surrogate_key(['m.map_name']) }}           as map_sk,
    -- Null, not a hash of null: 10,632 deaths (blue zone, falls, drowning) have
    -- no weapon at all, and inventing a key for them would create a dimension
    -- member that means "no weapon" while looking like a real one.
    case
        when k.killer_weapon is not null
        then {{ dbt_utils.generate_surrogate_key(['k.killer_weapon']) }}
    end                                                             as weapon_sk,
    {{ dbt_utils.generate_surrogate_key(['k.victim_account_id']) }}  as victim_player_sk,
    case
        when k.killer_account_id is not null
        then {{ dbt_utils.generate_surrogate_key(['k.killer_account_id']) }}
    end                                                             as killer_player_sk,

    -- Match context
    m.match_type,
    m.game_mode,
    m.perspective,
    m.map_name,
    m.is_analytical,

    -- When in the match, and under what pressure
    date_diff('second', m.match_start_ts, k.event_ts)   as seconds_into_match,
    p.phase                                             as zone_phase,
    l.num_alive_players,
    l.num_alive_teams,
    l.safety_zone_radius_m,

    -- Participants
    k.victim_account_id,
    k.victim_is_bot,
    k.killer_account_id,
    k.killer_is_bot,
    k.has_killer,
    k.is_human_vs_human,
    k.death_cause,
    k.assist_count,

    -- Weapon and geometry
    k.killer_weapon,
    k.killer_damage_reason,
    k.killer_distance_m,
    k.killer_through_wall,
    k.victim_in_blue_zone,
    k.victim_in_vehicle,
    k.victim_x_m,
    k.victim_y_m,
    {{ grid_cell('k.victim_x_m', 'k.victim_y_m') }}      as victim_grid_cell,

    -- Distance bucket, so engagement-range comparisons don't need re-deriving
    case
        when k.killer_distance_m is null then null
        when k.killer_distance_m <   10 then '0-10m'
        when k.killer_distance_m <   50 then '10-50m'
        when k.killer_distance_m <  100 then '50-100m'
        when k.killer_distance_m <  200 then '100-200m'
        when k.killer_distance_m <  400 then '200-400m'
        else '400m+'
    end                                                 as range_bucket

from {{ ref('slv_kill') }} k
inner join {{ ref('slv_match') }} m using (match_id)
asof left join phases p
    on k.match_id = p.match_id and k.event_ts >= p.event_ts
asof left join lobby l
    on k.match_id = l.match_id and k.event_ts >= l.event_ts
