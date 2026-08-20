-- Bridge: one row per (death, player, role).
--
-- A death involves several actors — victim, killer, finisher, the player who
-- knocked them down, and assists. Flattening those onto the kill row would make
-- every aggregate double-count damage and credit. Keeping them here means a
-- "kills by player" query is a simple filter on role.
--
-- 6.3% of raw rows duplicate on (match, attack_id, role, account) because
-- attack_id repeats within a match; including the timestamp resolves it.

with src as (

    select * from {{ source('bronze', 'kill_participant') }}

),

deduped as (

    select *
    from src
    -- Deterministic pick so the model is reproducible run to run.
    qualify row_number() over (
        partition by match_id, event_ts, attack_id, role, account_id
        order by coalesce(x, 0), coalesce(y, 0)
    ) = 1

),

k as (

    -- (match_id, event_ts, attack_id) is unique in slv_kill, so this attaches
    -- exactly one death to each participant row. attack_id is nulled there for
    -- environmental deaths, so it is restored to -1 to join on.
    select
        kill_sk,
        match_id,
        event_ts,
        coalesce(attack_id, -1) as attack_id_raw
    from {{ ref('slv_kill') }}

)

select
    k.kill_sk,
    d.match_id,
    cast(d.event_ts as timestamp)                   as event_ts,
    nullif(d.attack_id, -1)                         as attack_id,
    d.role,
    d.account_id,
    d.player_name,
    d.team_id,
    d.is_bot,
    d.x / {{ var('cm_per_m') }}                     as x_m,
    d.y / {{ var('cm_per_m') }}                     as y_m,
    d.z / {{ var('cm_per_m') }}                     as z_m

from deduped d
inner join k
    on  d.match_id = k.match_id
    and cast(d.event_ts as timestamp) = k.event_ts
    and d.attack_id = k.attack_id_raw
