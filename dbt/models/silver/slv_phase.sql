-- Zone phases: the battle-royale equivalent of rounds.
--
-- Each phase is a discrete, escalating stage with a spatial constraint, which
-- makes it the natural grain for "what happened during this part of the match"
-- in the same way a round is for a tactical shooter.

select
    match_id,
    cast(event_ts as timestamp)                     as event_ts,
    phase,
    players_in_white_circle,

    -- Phase duration, so per-phase rates can be normalised by time.
    cast(
        lead(event_ts) over (partition by match_id order by event_ts)
        as timestamp
    )                                               as next_phase_ts,
    date_diff(
        'second',
        cast(event_ts as timestamp),
        cast(lead(event_ts) over (partition by match_id order by event_ts) as timestamp)
    )                                               as phase_duration_s

from {{ source('bronze', 'phase') }}
