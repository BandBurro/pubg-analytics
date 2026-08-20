-- Conformed map dimension.
--
-- Built from the maps actually observed, so a new map appearing in the data shows
-- up here with a null display name rather than silently vanishing from reports.

with observed as (

    select
        map_name,
        count(*)                as match_count,
        round(avg(duration_s))  as avg_duration_s,
        min(match_start_ts)     as first_seen_ts,
        max(match_start_ts)     as last_seen_ts
    from {{ ref('slv_match') }}
    group by 1

)

select
    {{ dbt_utils.generate_surrogate_key(['map_name']) }} as map_sk,
    map_name,
    case map_name
        when 'Baltic_Main'     then 'Erangel'
        when 'Desert_Main'     then 'Miramar'
        when 'Savage_Main'     then 'Sanhok'
        when 'DihorOtok_Main'  then 'Vikendi'
        when 'Tiger_Main'      then 'Taego'
        when 'Neon_Main'       then 'Rondo'
        when 'Chimera_Main'    then 'Paramo'
        when 'Summerland_Main' then 'Karakin'
        when 'Kiki_Main'       then 'Deston'
        when 'Range_Main'      then 'Camp Jackal'
        when 'Heaven_Main'     then 'Haven'
    end                                     as map_display_name,

    -- Camp Jackal is the training range, not a competitive map. Its matches
    -- average ~560s against ~1700s elsewhere, which is why it needs excluding
    -- from anything about real play.
    map_name = 'Range_Main'                 as is_training_map,

    match_count,
    avg_duration_s,
    first_seen_ts,
    last_seen_ts

from observed
