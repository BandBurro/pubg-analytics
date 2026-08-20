-- Grain: map × landing grid cell.
--
-- Drop choice is the one major decision every player makes before the game has
-- any state, which makes it unusually clean to measure: there is no prior
-- performance confounding it the way there is for mid-match decisions.
--
-- `contest_level` is the count of *other* players who dropped in the same cell in
-- the same match — the mechanism by which a popular cell becomes a dangerous one.

with drops as (

    select
        pm.match_id,
        pm.account_id,
        pm.map_name,
        pm.landing_grid_cell            as grid_cell,
        pm.landing_x_m,
        pm.landing_y_m,
        pm.human_placement_pct,
        pm.human_kills,
        pm.time_survived,
        pm.survived_to_end,
        count(*) over (
            partition by pm.match_id, pm.landing_grid_cell
        ) - 1                           as contest_level
    from {{ ref('fact_player_match') }} pm
    where pm.is_analytical
      and not pm.is_bot
      and pm.landing_grid_cell is not null

)

select
    d.map_name,
    m.map_display_name,
    d.grid_cell,
    count(*)                                        as drops,
    count(distinct d.match_id)                      as matches,

    round(avg(d.landing_x_m))                       as centroid_x_m,
    round(avg(d.landing_y_m))                       as centroid_y_m,

    -- Outcome. Lower finish percentile is better: 0.0 is a win.
    round(avg(d.human_placement_pct), 4)            as avg_finish_pct,
    round(median(d.human_placement_pct), 4)         as median_finish_pct,
    round(avg(d.time_survived))                     as avg_survival_s,
    round(avg(d.human_kills), 3)                    as avg_human_kills,
    round(avg(case when d.survived_to_end then 1.0 else 0.0 end), 4) as win_rate,

    -- How busy the cell is, which is the usual explanation for a bad one.
    round(avg(d.contest_level), 2)                  as avg_contest_level,

    -- Kills earned per unit of finishing position given up. A cell that trades
    -- position for kills is a different proposition from one that just kills you.
    round(
        avg(d.human_kills) / nullif(avg(d.human_placement_pct), 0), 2
    )                                               as kills_per_finish_cost,

    count(*) >= 50                                  as has_enough_drops

from drops d
left join {{ ref('dim_map') }} m on d.map_name = m.map_name
group by 1, 2, 3
