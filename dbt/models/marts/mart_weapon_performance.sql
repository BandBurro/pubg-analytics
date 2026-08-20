-- Grain: weapon × perspective × range bucket.
--
-- Restricted to analytical matches and human-vs-human kills throughout. The naive
-- equivalent of this table ranks the MP5K first; that ranking is an artefact of
-- tutorial lobbies and bot farming, so the filters are not optional here.
--
-- `kill_share` is share of kills, which measures *usage as much as lethality* —
-- a popular mediocre gun outranks a rare excellent one. Read it alongside
-- headshot and knock rates rather than as a power ranking.

with base as (

    select
        k.*,
        w.weapon_name,
        w.weapon_class,
        w.damage_source
    from {{ ref('fact_kill') }} k
    inner join {{ ref('dim_weapon') }} w using (weapon_sk)
    where k.is_analytical
      and k.is_human_vs_human
      and w.is_firearm

),

totals as (

    select
        perspective,
        count(*) as perspective_kills
    from base
    group by 1

),

agg as (

    select
        b.weapon_name,
        b.weapon_class,
        b.perspective,
        coalesce(b.range_bucket, 'unknown')             as range_bucket,
        count(*)                                        as kills,
        count(distinct b.match_id)                      as matches,
        count(distinct b.killer_account_id)             as distinct_killers,
        round(median(b.killer_distance_m), 1)           as median_distance_m,
        round(avg(b.killer_distance_m), 1)              as mean_distance_m,
        round(quantile_cont(b.killer_distance_m, 0.9), 1) as p90_distance_m,
        avg(case when b.killer_through_wall then 1.0 else 0.0 end) as through_wall_rate,
        avg(case when b.victim_in_vehicle then 1.0 else 0.0 end)   as victim_in_vehicle_rate,
        round(avg(b.zone_phase), 2)                     as mean_zone_phase
    from base b
    group by 1, 2, 3, 4

)

select
    a.*,
    t.perspective_kills,
    a.kills / cast(t.perspective_kills as double)       as kill_share,

    -- Sample-size gate. Anything below this should not be ranked or compared.
    a.kills >= 100                                      as has_enough_kills

from agg a
inner join totals t using (perspective)
