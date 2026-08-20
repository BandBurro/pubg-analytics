-- Grain: killer weapon class × victim weapon class × range bucket.
--
-- This is the battle-royale answer to a counter-pick matrix. PUBG has no draft,
-- so there are no picks to counter — but every death records what the killer used
-- *and* what the victim was holding, which is a directly observed matchup rather
-- than an inferred one. 95.8% of human-vs-human kills carry both.
--
-- Two methodological points this table exists to enforce:
--
-- * **Symmetry.** A win rate needs both directions. Counting only "X killed Y"
--   measures how often X is used, not whether X beats Y. Each cell pairs
--   wins(X,Y) against wins(Y,X).
-- * **Shrinkage.** With 11 classes and 6 range buckets there are ~660 cells, so
--   the tail is thin. Raw rates are published alongside shrunk ones, but only the
--   shrunk rate should ever be ranked.
--
-- Mirror matchups are kept and flagged. They are 50% by construction and carry no
-- signal, which is exactly why they must be excluded from comparisons rather than
-- quietly averaged in.
--
-- ## The trap this table must not spring
--
-- `victim_weapon` is what the victim was *holding when they died*, not what they
-- were fighting with. Nobody dies holding a grenade, so throwables appear as the
-- killer constantly and as the victim almost never — producing a raw win rate of
-- exactly 1.000 over thousands of fights. That is observability asymmetry, not
-- combat superiority. Long-range classes have the same problem: you die to a
-- sniper while holding your rifle, but the sniper is rarely the one dying at 300 m.
--
-- `has_symmetric_observation` marks the cells where both classes are observed on
-- both sides often enough for the comparison to mean anything. **Only those cells
-- are interpretable as matchup strength.** The rest describe who initiates
-- engagements at what range, which is a different and much weaker claim.

with fights as (

    select
        coalesce(k.range_bucket, 'unknown')     as range_bucket,
        kw.weapon_class                         as killer_class,
        vw.weapon_class                         as victim_class
    from {{ ref('fact_kill') }} k
    inner join {{ ref('weapon_class') }} kw on k.killer_weapon = kw.causer_name
    inner join {{ ref('weapon_class') }} vw on k.victim_weapon = vw.causer_name
    where k.is_analytical
      and k.is_human_vs_human
      -- Team kills and environmental deaths aren't duels between two loadouts.
      and k.death_cause = 'player'

),

directed as (

    select
        killer_class    as class_a,
        victim_class    as class_b,
        range_bucket,
        count(*)        as a_beats_b
    from fights
    group by 1, 2, 3

),

global_rate as (

    -- The prior each cell is shrunk toward. 0.5 is the only defensible one: with
    -- no evidence, neither loadout is favoured.
    select 0.5 as prior

),

paired as (

    select
        d.class_a,
        d.class_b,
        d.range_bucket,
        d.a_beats_b,
        coalesce(r.a_beats_b, 0) as b_beats_a
    from directed d
    left join directed r
        on  d.class_a = r.class_b
        and d.class_b = r.class_a
        and d.range_bucket = r.range_bucket

)

select
    p.class_a,
    p.class_b,
    p.range_bucket,
    p.class_a = p.class_b                       as is_mirror,
    p.a_beats_b,
    p.b_beats_a,
    p.a_beats_b + p.b_beats_a                   as fights,

    -- Raw rate: what a naive query would report. Kept for comparison, not ranking.
    round(
        p.a_beats_b / cast(nullif(p.a_beats_b + p.b_beats_a, 0) as double), 4
    )                                           as raw_win_rate,

    round(
        {{ shrink_rate('p.a_beats_b', 'p.a_beats_b + p.b_beats_a', 'g.prior', 200) }}, 4
    )                                           as shrunk_win_rate,

    -- How far shrinkage had to move the estimate. Large values mark cells whose
    -- raw rate was carried by too little evidence to trust.
    round(
        abs(
            p.a_beats_b / cast(nullif(p.a_beats_b + p.b_beats_a, 0) as double)
            - {{ shrink_rate('p.a_beats_b', 'p.a_beats_b + p.b_beats_a', 'g.prior', 200) }}
        ), 4
    )                                           as shrinkage_applied,

    p.a_beats_b + p.b_beats_a >= 200            as has_enough_fights,

    -- Both directions actually observed. Without this, a 100% win rate means
    -- "this class is never caught holding its weapon", not "this class wins".
    (
        p.class_a != p.class_b
        and p.a_beats_b >= 100
        and p.b_beats_a >= 100
    )                                           as has_symmetric_observation

from paired p
cross join global_rate g
