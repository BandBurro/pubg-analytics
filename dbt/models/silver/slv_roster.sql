-- One row per team per match. `won` arrives from the API as the string
-- "true"/"false"; it was coerced to a boolean during shredding.

select
    match_id,
    roster_id,
    team_id,
    team_rank,
    won,
    participant_count

from {{ source('bronze', 'roster') }}
