-- Grain: match × player. Movement behaviour on foot, joined to how it ended.
--
-- This is where 207M position rows become answerable. Two confounds had to be
-- removed first, both of which produced confident nonsense in the first version:
--
-- 1. **Position logging starts on the aircraft.** Mean altitude in the first 60s
--    is 574 m (max 1,506 m), and the plane crosses the whole map. Naive
--    distance/time gave 45 m/s — 162 km/h — for players who died early, because
--    their tracked window was mostly flight. Fixed by counting only positions at
--    or after the player's own parachute landing.
--
-- 2. **The safe-zone radius collapses 5,637 m -> 110 m** across phases, a 51x
--    shrink. So distance-from-centre divided by radius inflates enormously for
--    whoever survives longest — it measured survival, not zone discipline. Fixed
--    by reporting absolute distance plus a scale-free "was outside the zone"
--    share, and dropping the ratio.
--
-- 3. **Redeploys put players back in the air mid-match.** Filtering on the first
--    landing left 6 players at 64-76 m/s, every one of them with 2-4 parachute
--    landings and single steps of 257-787 m/s. Fixed with a physical-plausibility
--    bound: PUBG's fastest vehicles top out near 30-40 m/s, so a step implying
--    over 60 m/s is flight, not travel. Those steps are excluded from distance
--    and *counted* in `implausible_steps`, because a silent filter is how a
--    known artifact becomes an unknown one.
--
-- Positions are sampled every 10 s (134M of 136M observed gaps are exactly 10),
-- so tick counts convert to seconds by multiplying by 10.

with landed as (

    -- The player's own touchdown. Anything before this is flight or descent.
    select match_id, account_id, min(event_ts) as landed_ts
    from {{ ref('slv_landing') }}
    where not is_bot
    group by 1, 2

),

pos as (

    select p.*
    from {{ ref('slv_player_position') }} p
    inner join landed l
        on p.match_id = l.match_id
       and p.account_id = l.account_id
       and p.event_ts >= l.landed_ts
    where not p.is_bot

),

zone as (

    select match_id, event_ts, safety_zone_x_m, safety_zone_y_m, safety_zone_radius_m
    from {{ ref('slv_game_state') }}
    where safety_zone_radius_m > 0

),

-- ASOF, not a range join: exactly one zone state per position — the most recent
-- at or before it. A BETWEEN join would fan out.
located as (

    select
        p.match_id,
        p.account_id,
        p.event_ts,
        p.x_m,
        p.y_m,
        p.in_vehicle,
        p.in_blue_zone,
        z.safety_zone_radius_m,
        sqrt(
            pow(p.x_m - z.safety_zone_x_m, 2) + pow(p.y_m - z.safety_zone_y_m, 2)
        ) as dist_from_centre_m
    from pos p
    asof left join zone z
        on p.match_id = z.match_id and p.event_ts >= z.event_ts

),

stepped as (

    select
        *,
        sqrt(pow(x_m - lag(x_m) over w, 2) + pow(y_m - lag(y_m) over w, 2)) as step_m
    from located
    window w as (partition by match_id, account_id order by event_ts)

)

select
    {{ dbt_utils.generate_surrogate_key(['s.match_id', 's.account_id']) }} as rotation_sk,
    s.match_id,
    s.account_id,

    count(*)                                            as ticks,
    count(*) * 10                                       as grounded_seconds,

    -- Movement, on foot or in a vehicle — never in the air. 600 m in a 10 s tick
    -- is 60 m/s; nothing drivable in PUBG goes that fast.
    round(sum(case when s.step_m <= 600 then s.step_m else 0 end)) as distance_travelled_m,
    round(
        sum(case when s.step_m <= 600 then s.step_m else 0 end)
        / nullif(count(*) * 10, 0), 2
    )                                                   as mean_speed_mps,
    round(max(case when s.step_m <= 600 then s.step_m end) / 10.0, 1) as peak_speed_mps,
    -- Non-zero here means this player redeployed and flew mid-match.
    count(*) filter (where s.step_m > 600)              as implausible_steps,

    -- Zone discipline, scale-free. `in_blue_zone` is the game's own flag, so it
    -- needs no normalising; `share_outside_zone` is geometry, kept as a check.
    round(avg(s.dist_from_centre_m))                    as mean_dist_from_centre_m,
    round(
        avg(case when s.dist_from_centre_m > s.safety_zone_radius_m then 1.0 else 0.0 end), 4
    )                                                   as share_outside_zone,
    round(avg(case when s.in_blue_zone then 1.0 else 0.0 end), 4) as blue_zone_share,
    sum(case when s.in_blue_zone then 1 else 0 end) * 10 as seconds_in_blue_zone,

    round(avg(case when s.in_vehicle then 1.0 else 0.0 end), 4)  as vehicle_share,
    sum(case when s.in_vehicle then 1 else 0 end) * 10            as seconds_in_vehicle,

    -- Outcome, so this table answers questions without further joins.
    max(pm.human_placement_pct)                         as human_placement_pct,
    max(pm.human_kills)                                 as human_kills,
    max(pm.team_mode)                                   as team_mode,
    max(pm.map_name)                                    as map_name,
    max(case when pm.is_analytical then 1 else 0 end) = 1 as is_analytical

from stepped s
inner join {{ ref('fact_player_match') }} pm
    on s.match_id = pm.match_id and s.account_id = pm.account_id
group by 1, 2, 3
