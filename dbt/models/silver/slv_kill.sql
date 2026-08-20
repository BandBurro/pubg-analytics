-- One row per death.
--
-- Key design note: `attack_id` is NOT usable as a key — it is -1 on 31% of rows,
-- because deaths with no attacker (blue zone, falls, drowning) have no attack.
-- And a player can die more than once in a match (respawns exist in tutorial and
-- training modes), so (match_id, victim) isn't unique either. The surrogate key
-- therefore includes the timestamp.

with src as (

    select * from {{ source('bronze', 'kill') }}

)

select
    {{ dbt_utils.generate_surrogate_key(['match_id', 'event_ts', 'victim_account_id']) }}
        as kill_sk,
    match_id,
    cast(event_ts as timestamp)                     as event_ts,
    nullif(attack_id, -1)                           as attack_id,
    nullif(dbno_id, -1)                             as dbno_id,
    is_suicide,

    -- Victim
    victim_account_id,
    victim_name,
    victim_team_id,
    victim_is_bot,
    victim_game_rank,
    victim_in_blue_zone,
    victim_in_red_zone,
    victim_in_vehicle,
    victim_x / {{ var('cm_per_m') }}                as victim_x_m,
    victim_y / {{ var('cm_per_m') }}                as victim_y_m,
    victim_z / {{ var('cm_per_m') }}                as victim_z_m,

    -- Killer. Absent on ~15% of deaths, so this is a left-ish column set:
    -- any inner join on killer silently discards environmental deaths.
    killer_account_id,
    killer_name,
    killer_team_id,
    -- Shredding computes is_bot from the account id, and an absent killer has no
    -- id — which yields `false`, not null. Left as-is it reads as "the killer was
    -- human" on every environmental death. Null is the honest value.
    case when killer_account_id is not null then killer_is_bot end as killer_is_bot,
    killer_x / {{ var('cm_per_m') }}                as killer_x_m,
    killer_y / {{ var('cm_per_m') }}                as killer_y_m,
    killer_z / {{ var('cm_per_m') }}                as killer_z_m,
    killer_account_id is not null                   as has_killer,

    finisher_account_id,
    finisher_is_bot,
    dbno_maker_account_id,

    -- Weapon and damage context
    nullif(killer_damage_causer, '')                as killer_weapon,
    nullif(killer_damage_reason, '')                as killer_damage_reason,
    nullif(killer_damage_category, '')              as killer_damage_category,
    nullif(finish_damage_causer, '')                as finish_weapon,
    nullif(finish_damage_category, '')              as finish_damage_category,
    nullif(victim_weapon, '')                       as victim_weapon,
    case
        when killer_distance is null or killer_distance < 0 then null
        else killer_distance / {{ var('cm_per_m') }}
    end                                             as killer_distance_m,
    killer_through_wall,

    assist_count,
    team_kill_count,

    -- Structural classification. Raw damage fields stay above so this can be
    -- refined without re-deriving it.
    case
        when is_suicide                             then 'suicide'
        when killer_account_id is null              then 'environment'
        when killer_account_id = victim_account_id  then 'self'
        -- teamKillers_AccountId is the authoritative signal. Comparing team ids
        -- finds nothing: killer_team_id never equals victim_team_id in this data,
        -- yet 895 kills carry a team-killer, so the id comparison is meaningless.
        when team_kill_count > 0                    then 'team_kill'
        else 'player'
    end                                             as death_cause,

    -- The flag that keeps combat statistics honest. Bots are 64% of all victim
    -- rows, so anything measuring weapon or duel performance must require this.
    -- has_killer is required explicitly: an environmental death is not a duel.
    (
        not victim_is_bot
        and killer_account_id is not null
        and not killer_is_bot
    )                                               as is_human_vs_human

from src
