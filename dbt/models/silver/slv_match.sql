-- One row per match, typed, with integrity flags attached rather than applied.
--
-- Nothing is filtered out here. `is_analytical` marks whether a match belongs in
-- balance or skill analysis, and downstream marts opt in explicitly. Filtering
-- at this layer would silently shrink the corpus by ~40% with no trace of why.

with src as (

    select * from {{ source('bronze', 'match') }}

),

typed as (

    select
        match_id,
        telemetry_match_id,
        cast(created_at as timestamp)              as match_start_ts,
        duration_s,
        game_mode,
        -- First- and third-person are different competitive games. Pooling them
        -- would average away real differences in engagement ranges.
        case when game_mode like '%-fpp' then 'fpp' else 'tpp' end as perspective,
        replace(game_mode, '-fpp', '')             as team_mode,
        match_type,
        map_name,
        shard_id,
        region,
        season_state,
        is_custom_match,
        nullif(ping_quality, '')                   as ping_quality,
        player_count,
        bot_count,
        human_count,
        roster_count,
        cast(bot_count as double) / nullif(player_count, 0) as bot_share

    from src

)

select
    *,

    -- Integrity flags, each independently inspectable.
    match_type in {{ "('" ~ var('analytical_match_types') | join("','") ~ "')" }}
        as is_competitive_type,
    duration_s >= {{ var('min_match_duration_s') }}  as is_full_length,
    human_count >= {{ var('min_human_count') }}      as has_human_lobby,

    (
        match_type in {{ "('" ~ var('analytical_match_types') | join("','") ~ "')" }}
        and duration_s >= {{ var('min_match_duration_s') }}
        and human_count >= {{ var('min_human_count') }}
    ) as is_analytical

from typed
