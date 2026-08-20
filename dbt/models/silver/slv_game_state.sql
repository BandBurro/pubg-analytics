-- Match state sampled roughly every 10 seconds: who is alive, and where the
-- zones are. This is the substrate for the zone-luck question — the safe zone
-- centre is randomised, so its distance from where players actually are is a
-- measurable source of unearned advantage.

select
    match_id,
    cast(event_ts as timestamp)                     as event_ts,
    elapsed_time_s,

    num_start_teams,
    num_alive_teams,
    num_join_players,
    num_start_players,
    num_alive_players,
    num_start_players - num_alive_players           as players_eliminated,

    safety_zone_x / {{ var('cm_per_m') }}           as safety_zone_x_m,
    safety_zone_y / {{ var('cm_per_m') }}           as safety_zone_y_m,
    safety_zone_radius / {{ var('cm_per_m') }}      as safety_zone_radius_m,

    poison_gas_x / {{ var('cm_per_m') }}            as poison_gas_x_m,
    poison_gas_y / {{ var('cm_per_m') }}            as poison_gas_y_m,
    nullif(poison_gas_radius, 0) / {{ var('cm_per_m') }} as poison_gas_radius_m,

    nullif(red_zone_radius, 0) / {{ var('cm_per_m') }}   as red_zone_radius_m,
    nullif(black_zone_radius, 0) / {{ var('cm_per_m') }} as black_zone_radius_m,

    -- The circle is shrinking whenever the next safe zone is smaller than the
    -- current one; useful for splitting "moving" from "holding" periods.
    poison_gas_radius > 0                           as is_zone_closing

from {{ source('bronze', 'game_state') }}
