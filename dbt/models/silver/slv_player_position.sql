{{ config(materialized='view') }}

-- Positions, typed and converted to metres. 207M rows.
--
-- **A view, not a table, and that is the point.** Every other Silver model is
-- materialised, because the cost of a copy is trivial and the query speedup is
-- real. Here a copy would be 3 GB of Parquet duplicated for no gain: nothing
-- queries this at row grain — the marts downstream aggregate it to match × player
-- (~2M rows) or map × grid cell (a few thousand). Materialising it would double
-- storage to speed up queries nobody runs.
--
-- No dedup, deliberately. Unlike LogParachuteLanding, which is 22% duplicate
-- emissions, positions have **12 duplicate rows in 207,508,882** on
-- (match_id, account_id, event_ts). The duplicate-emission problem in this
-- telemetry is event-type-specific, not a property of the feed.
--
-- Note that `elapsed_time_s` is *not* a usable key: it is coarser than the
-- timestamp and collides on 5.9% of rows.

select
    match_id,
    cast(event_ts as timestamp)             as event_ts,
    elapsed_time_s,
    num_alive_players,
    account_id,
    is_bot,
    team_id,
    health,
    ranking,
    in_vehicle,
    in_blue_zone,
    x / {{ var('cm_per_m') }}               as x_m,
    y / {{ var('cm_per_m') }}               as y_m,
    z / {{ var('cm_per_m') }}               as z_m

from {{ source('bronze', 'player_position') }}
