-- Drop locations, deduplicated.
--
-- 22% of raw landing rows are duplicate emissions of the same event: identical
-- timestamp and distance, coordinates differing by ~18 cm. Dedup on
-- (match_id, account_id, event_ts) removes them while preserving genuine repeat
-- landings from redeploys, which have different timestamps.

with src as (

    select * from {{ source('bronze', 'landing') }}

),

deduped as (

    select *
    from src
    -- Deterministic pick so the model is reproducible run to run.
    qualify row_number() over (
        partition by match_id, account_id, event_ts
        order by x, y, z
    ) = 1

)

select
    match_id,
    cast(event_ts as timestamp)                     as event_ts,
    account_id,
    player_name,
    team_id,
    is_bot,
    distance                                        as parachute_distance,
    x / {{ var('cm_per_m') }}                       as x_m,
    y / {{ var('cm_per_m') }}                       as y_m,
    z / {{ var('cm_per_m') }}                       as z_m,

    -- Landing sequence per player. 1 is the initial drop; higher values are
    -- redeploys, which are a different tactical event and should not be pooled.
    row_number() over (
        partition by match_id, account_id order by event_ts
    )                                               as landing_seq

from deduped
