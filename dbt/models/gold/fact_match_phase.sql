-- Grain: one row per match per zone phase.
--
-- Phases are the battle-royale analogue of rounds: discrete, escalating stages
-- with a shrinking spatial constraint. This is the table that makes "when in a
-- match did this happen" a groupable dimension instead of a raw timestamp.

with phases as (

    select
        match_id,
        max(phase)                  as phase,
        min(event_ts)               as phase_start_ts,
        max(next_phase_ts)          as phase_end_ts,
        max(players_in_white_circle) as players_in_white_circle
    from {{ ref('slv_phase') }}
    group by match_id, phase

),

lobby_at_start as (

    select
        p.match_id,
        p.phase,
        l.num_alive_players,
        l.num_alive_teams,
        l.safety_zone_radius_m
    from phases p
    asof left join {{ ref('slv_game_state') }} l
        on p.match_id = l.match_id and p.phase_start_ts >= l.event_ts

),

deaths as (

    select
        match_id,
        zone_phase                                          as phase,
        count(*)                                            as deaths,
        count(*) filter (where is_human_vs_human)           as human_deaths,
        count(*) filter (where death_cause = 'environment')  as environment_deaths,
        round(median(killer_distance_m), 1)                 as median_engagement_m
    from {{ ref('fact_kill') }}
    where zone_phase is not null
    group by 1, 2

)

select
    {{ dbt_utils.generate_surrogate_key(['p.match_id', 'p.phase']) }} as match_phase_sk,
    p.match_id,
    {{ dbt_utils.generate_surrogate_key(['m.map_name']) }}            as map_sk,
    p.phase,
    p.phase_start_ts,
    p.phase_end_ts,
    date_diff('second', p.phase_start_ts, p.phase_end_ts)             as phase_duration_s,

    m.match_type,
    m.game_mode,
    m.map_name,
    m.is_analytical,

    p.players_in_white_circle,
    ls.num_alive_players,
    ls.num_alive_teams,
    ls.safety_zone_radius_m,

    coalesce(d.deaths, 0)               as deaths,
    coalesce(d.human_deaths, 0)         as human_deaths,
    coalesce(d.environment_deaths, 0)   as environment_deaths,
    d.median_engagement_m,

    -- Deaths per minute: the headline "how violent was this phase" measure.
    case
        when date_diff('second', p.phase_start_ts, p.phase_end_ts) > 0
        then round(
            coalesce(d.deaths, 0) * 60.0
            / date_diff('second', p.phase_start_ts, p.phase_end_ts), 2)
    end                                 as deaths_per_minute

from phases p
inner join {{ ref('slv_match') }} m using (match_id)
left join lobby_at_start ls
    on p.match_id = ls.match_id and p.phase = ls.phase
left join deaths d
    on p.match_id = d.match_id and p.phase = d.phase
