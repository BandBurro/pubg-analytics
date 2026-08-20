-- Conformed dimension over everything that can cause a death: guns, throwables,
-- vehicles, fists, and the blue zone.
--
-- `damage_source` is derived structurally from naming patterns, which is reliable.
-- `weapon_class` comes from a curated seed, because gun classes cannot be inferred
-- from an asset name. Anything not in the seed is 'unclassified' rather than
-- guessed — a wrong class silently corrupts every balance query built on it.

with observed as (

    select
        killer_weapon           as causer_name,
        count(*)                as kill_count,
        sum(case when is_human_vs_human then 1 else 0 end) as hvh_kill_count,
        round(median(killer_distance_m), 1) as median_distance_m
    from {{ ref('slv_kill') }}
    where killer_weapon is not null
    group by 1

),

classified as (

    select
        o.*,
        case
            when o.causer_name = 'None'                              then 'unknown'
            when o.causer_name ilike 'Bluezonebomb%'                 then 'blue_zone'
            when o.causer_name ilike '%Molotov%'
              or o.causer_name ilike '%FireEffect%'
              or o.causer_name ilike '%JerrycanFire%'
              or o.causer_name ilike '%GasPump%'                     then 'fire'
            when o.causer_name ilike 'PanzerFaust%'
              or o.causer_name ilike 'Mortar%'                       then 'launcher'
            when o.causer_name ilike 'Proj%'                         then 'throwable'
            when o.causer_name ilike 'Player%Pawn%'
              or o.causer_name ilike 'PlayerMale%'
              or o.causer_name ilike 'PlayerFemale%'
              or o.causer_name ilike 'UltAIPawn%'                    then 'fists'
            when o.causer_name ilike 'Weap%'                         then 'weapon'
            -- Everything left is a vehicle asset: Dacia_*, Uaz_*, Buggy_*, BP_*.
            else 'vehicle'
        end as damage_source
    from observed o

)

select
    {{ dbt_utils.generate_surrogate_key(['c.causer_name']) }} as weapon_sk,
    c.causer_name,
    coalesce(w.weapon_name, c.causer_name)      as weapon_name,
    c.damage_source,
    case
        when c.damage_source = 'weapon' then coalesce(w.weapon_class, 'unclassified')
        else c.damage_source
    end                                         as weapon_class,
    c.damage_source = 'weapon'                  as is_firearm,
    c.kill_count,
    c.hvh_kill_count,
    c.median_distance_m

from classified c
left join {{ ref('weapon_class') }} w
    on c.causer_name = w.causer_name
