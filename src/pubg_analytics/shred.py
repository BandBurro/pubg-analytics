"""Shred raw telemetry JSON into typed Parquet.

Raw JSON is faithful but useless to query: 47 heterogeneous event types in one
nested array per match. This step splits it into flat, typed tables that DuckDB
and dbt can read directly, without re-parsing JSON on every query.

Two rules govern what happens here:

1. **Nothing is dropped.** Bots, tutorial matches and training-room sessions all
   land in the output, carrying flags (`is_bot`, `match_type`) that let later
   layers exclude them explicitly. Silently filtering here would hide from the
   analyst that ~38% of collected matches aren't real competitive games.
2. **Nothing is interpreted.** Coordinates stay in the centimetres PUBG reports,
   timestamps stay as ISO strings. Unit conversion and business logic belong in
   Silver, where they're visible and testable.
"""

from pathlib import Path
from typing import Any

import orjson
import polars as pl

# ---------------------------------------------------------------- helpers


def is_bot(account_id: str | None) -> bool:
    """PUBG backfills lobbies with bots, whose ids are `ai.NNNN`.

    Humans are `account.<hex>`. This distinction decides whether a row may
    inform a skill rating, so it's computed once, here, and carried everywhere.
    """
    return bool(account_id) and account_id.startswith("ai.")


def _loc(obj: dict[str, Any] | None, key: str = "location") -> tuple:
    loc = (obj or {}).get(key) or {}
    return loc.get("x"), loc.get("y"), loc.get("z")


def _char(obj: dict[str, Any] | None, prefix: str) -> dict[str, Any]:
    """Flatten one PUBG 'character' object with a column prefix."""
    o = obj or {}
    x, y, z = _loc(o)
    acct = o.get("accountId")
    return {
        f"{prefix}_account_id": acct,
        f"{prefix}_name": o.get("name"),
        f"{prefix}_team_id": o.get("teamId"),
        f"{prefix}_health": o.get("health"),
        f"{prefix}_ranking": o.get("ranking"),
        f"{prefix}_individual_ranking": o.get("individualRanking"),
        f"{prefix}_is_bot": is_bot(acct),
        f"{prefix}_character_type": o.get("type"),
        f"{prefix}_in_blue_zone": o.get("isInBlueZone"),
        f"{prefix}_in_red_zone": o.get("isInRedZone"),
        f"{prefix}_in_vehicle": o.get("isInVehicle"),
        f"{prefix}_is_dbno": o.get("isDBNO"),
        f"{prefix}_x": x,
        f"{prefix}_y": y,
        f"{prefix}_z": z,
    }


def _damage(obj: dict[str, Any] | None, prefix: str) -> dict[str, Any]:
    o = obj or {}
    return {
        f"{prefix}_damage_reason": o.get("damageReason"),
        f"{prefix}_damage_category": o.get("damageTypeCategory"),
        f"{prefix}_damage_causer": o.get("damageCauserName"),
        f"{prefix}_distance": o.get("distance"),
        f"{prefix}_through_wall": o.get("isThroughPenetrableWall"),
    }


def _region_from_telemetry_match_id(mid: str | None) -> str | None:
    """`match.bro.official.pc-2018-42.steam.squad.as` -> `as`."""
    if not mid:
        return None
    parts = mid.split(".")
    return parts[-1] if len(parts) >= 3 else None


TABLES = (
    "match",
    "match_player",
    "roster",
    "phase",
    "game_state",
    "kill",
    "kill_participant",
    "landing",
)


class Shredder:
    """Accumulates rows across matches, then writes one Parquet part per table."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.buf: dict[str, list[dict]] = {t: [] for t in TABLES}

    # ------------------------------------------------------------ manifest

    def add_manifest(self, manifest: dict, telemetry_match_id: str | None, ping: str | None) -> str:
        data = manifest["data"]
        match_id = data["id"]
        attrs = data.get("attributes", {})
        included = manifest.get("included", [])

        participants = [i for i in included if i.get("type") == "participant"]
        rosters = [i for i in included if i.get("type") == "roster"]

        # participant id -> team, so player rows carry their roster's team.
        team_of: dict[str, int | None] = {}
        for r in rosters:
            team_id = r.get("attributes", {}).get("stats", {}).get("teamId")
            for p in r.get("relationships", {}).get("participants", {}).get("data") or []:
                team_of[p["id"]] = team_id

        bots = 0
        for p in participants:
            st = p.get("attributes", {}).get("stats", {})
            acct = st.get("playerId")
            bot = is_bot(acct)
            bots += bot
            self.buf["match_player"].append(
                {
                    "match_id": match_id,
                    "participant_id": p["id"],
                    "account_id": acct,
                    "player_name": st.get("name"),
                    "is_bot": bot,
                    "team_id": team_of.get(p["id"]),
                    "win_place": st.get("winPlace"),
                    "kill_place": st.get("killPlace"),
                    "kills": st.get("kills"),
                    "dbnos": st.get("DBNOs"),
                    "assists": st.get("assists"),
                    "damage_dealt": st.get("damageDealt"),
                    "death_type": st.get("deathType"),
                    "headshot_kills": st.get("headshotKills"),
                    "longest_kill": st.get("longestKill"),
                    "heals": st.get("heals"),
                    "boosts": st.get("boosts"),
                    "revives": st.get("revives"),
                    "road_kills": st.get("roadKills"),
                    "team_kills": st.get("teamKills"),
                    "vehicle_destroys": st.get("vehicleDestroys"),
                    "weapons_acquired": st.get("weaponsAcquired"),
                    "walk_distance": st.get("walkDistance"),
                    "ride_distance": st.get("rideDistance"),
                    "swim_distance": st.get("swimDistance"),
                    "time_survived": st.get("timeSurvived"),
                }
            )

        for r in rosters:
            st = r.get("attributes", {}).get("stats", {})
            self.buf["roster"].append(
                {
                    "match_id": match_id,
                    "roster_id": r["id"],
                    "team_id": st.get("teamId"),
                    "team_rank": st.get("rank"),
                    # PUBG reports this as the string "true"/"false".
                    "won": str(r.get("attributes", {}).get("won")).lower() == "true",
                    "participant_count": len(
                        r.get("relationships", {}).get("participants", {}).get("data") or []
                    ),
                }
            )

        self.buf["match"].append(
            {
                "match_id": match_id,
                "telemetry_match_id": telemetry_match_id,
                "created_at": attrs.get("createdAt"),
                "duration_s": attrs.get("duration"),
                "game_mode": attrs.get("gameMode"),
                "match_type": attrs.get("matchType"),
                "map_name": attrs.get("mapName"),
                "shard_id": attrs.get("shardId"),
                "region": _region_from_telemetry_match_id(telemetry_match_id),
                "season_state": attrs.get("seasonState"),
                "is_custom_match": attrs.get("isCustomMatch"),
                "title_id": attrs.get("titleId"),
                "ping_quality": ping,
                "player_count": len(participants),
                "bot_count": bots,
                "human_count": len(participants) - bots,
                "roster_count": len(rosters),
            }
        )
        return match_id

    # ----------------------------------------------------------- telemetry

    def add_telemetry(self, match_id: str, events: list[dict]) -> None:
        for e in events:
            t = e.get("_T")
            if t == "LogPhaseChange":
                self.buf["phase"].append(
                    {
                        "match_id": match_id,
                        "event_ts": e.get("_D"),
                        "phase": e.get("phase"),
                        "players_in_white_circle": len(e.get("playersInWhiteCircle") or []),
                    }
                )
            elif t == "LogGameStatePeriodic":
                gs = e.get("gameState") or {}
                sx, sy, sz = _loc(gs, "safetyZonePosition")
                px, py, pz = _loc(gs, "poisonGasWarningPosition")
                rx, ry, rz = _loc(gs, "redZonePosition")
                self.buf["game_state"].append(
                    {
                        "match_id": match_id,
                        "event_ts": e.get("_D"),
                        "elapsed_time_s": gs.get("elapsedTime"),
                        "num_start_teams": gs.get("numStartTeams"),
                        "num_alive_teams": gs.get("numAliveTeams"),
                        "num_join_players": gs.get("numJoinPlayers"),
                        "num_start_players": gs.get("numStartPlayers"),
                        "num_alive_players": gs.get("numAlivePlayers"),
                        "safety_zone_x": sx,
                        "safety_zone_y": sy,
                        "safety_zone_z": sz,
                        "safety_zone_radius": gs.get("safetyZoneRadius"),
                        "poison_gas_x": px,
                        "poison_gas_y": py,
                        "poison_gas_z": pz,
                        "poison_gas_radius": gs.get("poisonGasWarningRadius"),
                        "red_zone_x": rx,
                        "red_zone_y": ry,
                        "red_zone_z": rz,
                        "red_zone_radius": gs.get("redZoneRadius"),
                        "black_zone_radius": gs.get("blackZoneRadius"),
                    }
                )
            elif t == "LogPlayerKillV2":
                self._add_kill(match_id, e)
            elif t == "LogParachuteLanding":
                ch = e.get("character") or {}
                x, y, z = _loc(ch)
                acct = ch.get("accountId")
                self.buf["landing"].append(
                    {
                        "match_id": match_id,
                        "event_ts": e.get("_D"),
                        "account_id": acct,
                        "player_name": ch.get("name"),
                        "team_id": ch.get("teamId"),
                        "is_bot": is_bot(acct),
                        "distance": e.get("distance"),
                        "x": x,
                        "y": y,
                        "z": z,
                    }
                )

    def _add_kill(self, match_id: str, e: dict) -> None:
        ts = e.get("_D")
        attack_id = e.get("attackId")
        vgr = e.get("victimGameResult") or {}
        assists = e.get("assists_AccountId") or []
        team_killers = e.get("teamKillers_AccountId") or []

        row: dict[str, Any] = {
            "match_id": match_id,
            "event_ts": ts,
            "attack_id": attack_id,
            "dbno_id": e.get("dBNOId"),
            "is_suicide": e.get("isSuicide"),
            "victim_game_rank": vgr.get("rank"),
            "victim_game_result": vgr.get("gameResult") or None,
            "assist_count": len(assists),
            "team_kill_count": len(team_killers),
        }
        row.update(_char(e.get("victim"), "victim"))
        row.update(_char(e.get("killer"), "killer"))
        row.update(_char(e.get("finisher"), "finisher"))
        row.update(_char(e.get("dBNOMaker"), "dbno_maker"))
        row.update(_damage(e.get("killerDamageInfo"), "killer"))
        row.update(_damage(e.get("finishDamageInfo"), "finish"))
        row.update(_damage(e.get("dBNODamageInfo"), "dbno"))
        row["victim_weapon"] = e.get("victimWeapon") or None
        self.buf["kill"].append(row)

        # Bridge rows. A kill has several actors in distinct roles; keeping them
        # in their own table is what stops damage and credit being double counted
        # when the fact table is aggregated.
        for role, key in (
            ("victim", "victim"),
            ("killer", "killer"),
            ("finisher", "finisher"),
            ("dbno_maker", "dBNOMaker"),
        ):
            obj = e.get(key)
            if not obj or not obj.get("accountId"):
                continue
            x, y, z = _loc(obj)
            self.buf["kill_participant"].append(
                {
                    "match_id": match_id,
                    "event_ts": ts,
                    "attack_id": attack_id,
                    "role": role,
                    "account_id": obj.get("accountId"),
                    "player_name": obj.get("name"),
                    "team_id": obj.get("teamId"),
                    "is_bot": is_bot(obj.get("accountId")),
                    "x": x,
                    "y": y,
                    "z": z,
                }
            )
        for acct in assists:
            self.buf["kill_participant"].append(
                {
                    "match_id": match_id,
                    "event_ts": ts,
                    "attack_id": attack_id,
                    "role": "assist",
                    "account_id": acct,
                    "player_name": None,
                    "team_id": None,
                    "is_bot": is_bot(acct),
                    "x": None,
                    "y": None,
                    "z": None,
                }
            )

    # --------------------------------------------------------------- output

    def row_counts(self) -> dict[str, int]:
        return {t: len(rows) for t, rows in self.buf.items()}

    def flush(self, part: int) -> dict[str, int]:
        written = {}
        for table, rows in self.buf.items():
            if not rows:
                continue
            out = self.out_dir / table
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"part-{part:05d}.parquet"
            tmp = path.with_suffix(".parquet.tmp")
            pl.DataFrame(rows, infer_schema_length=None).write_parquet(tmp, compression="zstd")
            tmp.replace(path)
            written[table] = len(rows)
            rows.clear()
        return written


def read_gz_json(path: Path) -> Any:
    import gzip

    with gzip.open(path, "rb") as fh:
        return orjson.loads(fh.read())


def telemetry_to_manifest_path(telemetry_path: str) -> Path:
    """Manifest and telemetry are mirrored trees, so one path derives the other."""
    return Path(telemetry_path.replace("/telemetry/", "/matches/"))


def match_definition(events: list[dict]) -> tuple[str | None, str | None]:
    for e in events:
        if e.get("_T") == "LogMatchDefinition":
            return e.get("MatchId"), (e.get("PingQuality") or None)
    return None, None
