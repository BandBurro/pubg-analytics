"""Tests for the skill-rating engine.

The real corpus cannot validate this: no player has more than 4 matches in it, so
every rating sits near its prior. So the engine is checked against a synthetic
population whose true skill we chose — the only way to ask "does this recover the
right answer" rather than "does this produce plausible-looking numbers".
"""

import random

from pubg_analytics.ratings import MatchGroup, group_matches, run_ratings


def _rows(match_id, ts, players):
    """players: list of (team_id, account_id, human_placement)."""
    return [
        {
            "match_id": match_id,
            "match_start_ts": ts,
            "team_id": t,
            "account_id": a,
            "human_placement": p,
        }
        for t, a, p in players
    ]


def test_teams_ranked_by_best_human_finish():
    rows = _rows(
        "m1",
        1,
        [
            (10, "a", 5), (10, "b", 9),   # team 10's best finish is 5
            (20, "c", 1), (20, "d", 7),   # team 20's best finish is 1 -> winner
            (30, "e", 3),                  # team 30 -> second
        ],
    )
    [g] = group_matches(rows)
    ranks = {team: rank for team, rank, _ in g.teams}
    assert ranks == {20: 1, 30: 2, 10: 3}


def test_tied_teams_share_a_rank():
    rows = _rows("m1", 1, [(10, "a", 1), (20, "b", 1), (30, "c", 4)])
    [g] = group_matches(rows)
    ranks = sorted(rank for _, rank, _ in g.teams)
    # Two teams tie for first; the next team is third, not second.
    assert ranks == [1, 1, 3]


def test_single_human_team_yields_no_updates():
    """One team has nothing to be compared against, so it teaches us nothing."""
    rows = _rows("m1", 1, [(10, "a", 1), (10, "b", 2)])
    assert run_ratings(group_matches(rows)) == []


def test_pre_state_equals_previous_post_state():
    """The point-in-time guarantee.

    A player's rating going into match N must be exactly the rating they had
    coming out of match N-1. If this ever drifts, every downstream feature built
    on `mu_pre` is leaking future information.
    """
    groups = [
        MatchGroup("m1", 1, [(1, 1, ["a"]), (2, 2, ["b"])]),
        MatchGroup("m2", 2, [(1, 2, ["a"]), (2, 1, ["b"])]),
        MatchGroup("m3", 3, [(1, 1, ["a"]), (2, 2, ["b"])]),
    ]
    updates = run_ratings(groups)
    for account in ("a", "b"):
        hist = [u for u in updates if u["account_id"] == account]
        assert [u["games_played_before"] for u in hist] == [0, 1, 2]
        for prev, nxt in zip(hist, hist[1:], strict=False):
            assert nxt["mu_pre"] == prev["mu_post"]
            assert nxt["sigma_pre"] == prev["sigma_post"]


def test_uncertainty_shrinks_with_games_played():
    groups = [
        MatchGroup(f"m{i}", i, [(1, 1, ["a"]), (2, 2, ["b"])]) for i in range(12)
    ]
    hist = [u for u in run_ratings(groups) if u["account_id"] == "a"]
    assert hist[-1]["sigma_post"] < hist[0]["sigma_pre"]


def _spearman(xs, ys):
    """Rank correlation, without pulling in scipy."""

    def ranks(vs):
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        out = [0.0] * len(vs)
        for pos, i in enumerate(order):
            out[i] = float(pos)
        return out

    rx, ry = ranks(xs), ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy)


def test_engine_recovers_known_skill_ordering():
    """Ground-truth recovery.

    Build a population with skill we chose, simulate matches where stronger teams
    place better (with noise), and check the engine reconstructs the ordering it
    was never told.
    """
    rng = random.Random(7)
    n_players, n_matches, team_size, teams_per_match = 60, 400, 4, 5
    players = [f"p{i:02d}" for i in range(n_players)]
    true_skill = {p: i for i, p in enumerate(players)}

    groups = []
    for m in range(n_matches):
        picked = rng.sample(players, team_size * teams_per_match)
        rosters = [
            picked[i * team_size : (i + 1) * team_size] for i in range(teams_per_match)
        ]
        # Team performance is mean true skill plus noise, so results are
        # informative but far from deterministic.
        strength = [
            (sum(true_skill[p] for p in r) / team_size + rng.gauss(0, 8), i)
            for i, r in enumerate(rosters)
        ]
        strength.sort(reverse=True)
        teams = [
            (idx, rank + 1, rosters[idx]) for rank, (_, idx) in enumerate(strength)
        ]
        groups.append(MatchGroup(f"m{m:04d}", m, teams))

    updates = run_ratings(groups)
    assert updates, "engine produced no updates"

    final = {}
    for u in updates:  # updates are chronological, so the last write wins
        final[u["account_id"]] = u["ordinal_post"]

    rated = sorted(final)
    rho = _spearman([true_skill[p] for p in rated], [final[p] for p in rated])
    assert rho > 0.7, f"engine failed to recover skill ordering (rho={rho:.3f})"

    # And the best-rated player should genuinely be near the top of true skill.
    best = max(final, key=lambda p: final[p])
    assert true_skill[best] >= n_players * 0.7, f"top-rated player was {best}"
