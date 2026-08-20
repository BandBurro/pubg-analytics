-- Grain: player × match, bucketed by first-circle luck.
--
-- The fairness question a battle royale has and a tactical shooter doesn't: the
-- safe zone is randomised, so some players land inside it and others must cross
-- the map. If placement varies systematically with that draw, part of every
-- result is luck rather than skill — and it is measurable.
--
-- The measure is distance from landing point to the first circle's centre,
-- normalised by that circle's radius, so it is comparable across maps and
-- circle sizes:
--
--     < 1.0  landed inside the first circle
--     ~ 2.0  roughly one radius outside it
--
-- Landing choice isn't independent of the circle — players see it before they
-- drop — so this is an association, not a clean causal estimate. It bounds how
-- much the draw could be worth, which is the useful part.

-- Phase 1's circle averages ~5,500 m on an 8 km map, so essentially every player
-- starts inside it and the measure has no variance. Phase 2's circle (~1,850 m) is
-- the first one that actually constrains where you can be.
with phase_2 as (

    select match_id, phase_start_ts
    from {{ ref('fact_match_phase') }}
    where phase = 2

),

first_zone as (

    select
        p.match_id,
        g.safety_zone_x_m,
        g.safety_zone_y_m,
        g.safety_zone_radius_m
    from phase_2 p
    asof left join {{ ref('slv_game_state') }} g
        on p.match_id = g.match_id and p.phase_start_ts >= g.event_ts
    where g.safety_zone_radius_m > 0

),

joined as (

    select
        pm.match_id,
        pm.account_id,
        pm.map_name,
        pm.human_placement_pct,
        pm.human_kills,
        pm.time_survived,
        pm.total_distance,
        z.safety_zone_radius_m,
        sqrt(
            pow(pm.landing_x_m - z.safety_zone_x_m, 2)
            + pow(pm.landing_y_m - z.safety_zone_y_m, 2)
        )                                       as distance_to_centre_m
    from {{ ref('fact_player_match') }} pm
    inner join first_zone z using (match_id)
    where pm.is_analytical
      and not pm.is_bot
      and pm.landing_x_m is not null
      and pm.human_placement_pct is not null

),

scored as (

    select
        *,
        distance_to_centre_m / nullif(safety_zone_radius_m, 0) as radii_from_centre
    from joined

)

select
    case
        when radii_from_centre < 0.5 then '1. deep inside'
        when radii_from_centre < 1.0 then '2. inside'
        when radii_from_centre < 1.5 then '3. just outside'
        when radii_from_centre < 2.0 then '4. one radius out'
        else                              '5. far outside'
    end                                             as zone_luck_bucket,

    count(*)                                        as player_matches,
    round(avg(radii_from_centre), 3)                as avg_radii_from_centre,
    round(avg(distance_to_centre_m))                as avg_distance_m,

    round(avg(human_placement_pct), 4)              as avg_finish_pct,
    round(median(human_placement_pct), 4)           as median_finish_pct,
    round(avg(human_kills), 3)                      as avg_human_kills,
    round(avg(time_survived))                       as avg_survival_s,
    round(avg(total_distance))                      as avg_distance_travelled_m

from scored
group by 1
