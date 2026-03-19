from __future__ import annotations

import json
import math
import os
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

DEFAULT_CSA_SCHEDULE_PATH = Path(__file__).resolve().parent / "data" / "csa_schedule_sample.json"
WALKING_SPEED_MPS = 1.39
EARTH_RADIUS_METERS = 6371000.0
INF_TIME = 10**12


@dataclass(frozen=True)
class CsaStop:
    stop_id: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class CsaConnection:
    connection_id: str
    trip_id: str
    route_id: str
    from_stop_id: str
    to_stop_id: str
    departure_s: int
    arrival_s: int


@dataclass(frozen=True)
class CsaFootpath:
    from_stop_id: str
    to_stop_id: str
    duration_s: int
    distance_m: float


@dataclass(frozen=True)
class ExpandedConnection:
    connection: CsaConnection
    departure_abs_s: int
    arrival_abs_s: int


@dataclass(frozen=True)
class WalkOption:
    stop_id: str
    duration_s: int
    distance_m: float


@dataclass(frozen=True)
class ParentStep:
    kind: Literal["access", "connection", "footpath"]
    prev_stop_id: Optional[str]
    expanded_connection: Optional[ExpandedConnection]
    duration_s: int
    distance_m: float


@dataclass(frozen=True)
class CsaSchedule:
    version: str
    stops: Dict[str, CsaStop]
    connections: List[CsaConnection]
    footpaths_by_origin: Dict[str, List[CsaFootpath]]


def _time_to_seconds(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid time value: {value}")

    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2]) if len(parts) == 3 else 0

    if not (0 <= minutes < 60 and 0 <= seconds < 60 and 0 <= hours < 48):
        raise ValueError(f"Out-of-range time value: {value}")

    return hours * 3600 + minutes * 60 + seconds


def _to_radians(value: float) -> float:
    return (value * 3.141592653589793) / 180.0


def _haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = _to_radians(lat1)
    lon1_rad = _to_radians(lon1)
    lat2_rad = _to_radians(lat2)
    lon2_rad = _to_radians(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = (math.sin(dlat / 2) ** 2) + math.cos(lat1_rad) * math.cos(lat2_rad) * (
        math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_METERS * c


def _duration_from_distance(distance_m: float) -> int:
    return max(0, int(round(distance_m / WALKING_SPEED_MPS)))


def _stop_distance_meters(stop_from: CsaStop, stop_to: CsaStop) -> float:
    return _haversine_distance_meters(stop_from.lat, stop_from.lon, stop_to.lat, stop_to.lon)


def _build_walk_options(
    stops: Dict[str, CsaStop],
    lat: float,
    lon: float,
    max_walking_distance_m: float,
) -> List[WalkOption]:
    options: List[WalkOption] = []
    for stop in stops.values():
        distance = _haversine_distance_meters(lat, lon, stop.lat, stop.lon)
        if distance <= max_walking_distance_m:
            options.append(
                WalkOption(
                    stop_id=stop.stop_id,
                    duration_s=_duration_from_distance(distance),
                    distance_m=distance,
                )
            )
    return options


def _expand_connections(connections: List[CsaConnection]) -> List[ExpandedConnection]:
    expanded: List[ExpandedConnection] = []
    for connection in connections:
        for day_offset in (0, 86400):
            expanded.append(
                ExpandedConnection(
                    connection=connection,
                    departure_abs_s=connection.departure_s + day_offset,
                    arrival_abs_s=connection.arrival_s + day_offset,
                )
            )
    expanded.sort(key=lambda c: c.departure_abs_s)
    return expanded


def _coerce_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _seconds_to_datetime(day_start: datetime, seconds: int) -> datetime:
    return day_start + timedelta(seconds=seconds)


def _load_csa_schedule_from_path(path: Path) -> CsaSchedule:
    if not path.exists():
        raise FileNotFoundError(f"CSA schedule file not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    version = str(raw.get("version", "unknown"))

    stops: Dict[str, CsaStop] = {}
    for stop_raw in raw.get("stops", []):
        stop = CsaStop(
            stop_id=str(stop_raw["id"]),
            name=str(stop_raw.get("name", stop_raw["id"])),
            lat=float(stop_raw["lat"]),
            lon=float(stop_raw["lon"]),
        )
        stops[stop.stop_id] = stop

    connections: List[CsaConnection] = []
    for conn_raw in raw.get("connections", []):
        departure_s = _time_to_seconds(str(conn_raw["departure"]))
        arrival_s = _time_to_seconds(str(conn_raw["arrival"]))
        if arrival_s < departure_s:
            arrival_s += 86400

        from_stop = str(conn_raw["from_stop_id"])
        to_stop = str(conn_raw["to_stop_id"])
        if from_stop not in stops or to_stop not in stops:
            continue

        connections.append(
            CsaConnection(
                connection_id=str(conn_raw.get("id", f"{from_stop}-{to_stop}-{departure_s}")),
                trip_id=str(conn_raw["trip_id"]),
                route_id=str(conn_raw["route_id"]),
                from_stop_id=from_stop,
                to_stop_id=to_stop,
                departure_s=departure_s,
                arrival_s=arrival_s,
            )
        )
    connections.sort(key=lambda c: c.departure_s)

    footpaths_by_origin: Dict[str, List[CsaFootpath]] = {}
    for footpath_raw in raw.get("footpaths", []):
        from_stop_id = str(footpath_raw["from_stop_id"])
        to_stop_id = str(footpath_raw["to_stop_id"])
        if from_stop_id not in stops or to_stop_id not in stops:
            continue

        duration_s = int(footpath_raw["duration_s"])
        if duration_s < 0:
            continue

        distance_m = float(footpath_raw.get("distance_m", 0.0))
        if distance_m <= 0:
            distance_m = _stop_distance_meters(stops[from_stop_id], stops[to_stop_id])

        footpaths_by_origin.setdefault(from_stop_id, []).append(
            CsaFootpath(
                from_stop_id=from_stop_id,
                to_stop_id=to_stop_id,
                duration_s=duration_s,
                distance_m=distance_m,
            )
        )

    return CsaSchedule(
        version=version,
        stops=stops,
        connections=connections,
        footpaths_by_origin=footpaths_by_origin,
    )


@lru_cache(maxsize=8)
def _load_csa_schedule_cached(path_value: str) -> CsaSchedule:
    return _load_csa_schedule_from_path(Path(path_value))


def load_csa_schedule(schedule_path: Optional[str] = None) -> CsaSchedule:
    env_path = os.getenv("TRANSIT_CSA_SCHEDULE_PATH")
    resolved = Path(schedule_path or env_path or str(DEFAULT_CSA_SCHEDULE_PATH))
    return _load_csa_schedule_cached(str(resolved.resolve()))


def _relax_footpaths(
    start_stop_id: str,
    start_arrival_s: int,
    schedule: CsaSchedule,
    labels: Dict[str, int],
    parents: Dict[str, ParentStep],
) -> None:
    queue: deque[Tuple[str, int]] = deque([(start_stop_id, start_arrival_s)])
    while queue:
        current_stop_id, current_arrival_s = queue.popleft()
        for footpath in schedule.footpaths_by_origin.get(current_stop_id, []):
            candidate_arrival = current_arrival_s + footpath.duration_s
            if candidate_arrival < labels.get(footpath.to_stop_id, INF_TIME):
                labels[footpath.to_stop_id] = candidate_arrival
                parents[footpath.to_stop_id] = ParentStep(
                    kind="footpath",
                    prev_stop_id=current_stop_id,
                    expanded_connection=None,
                    duration_s=footpath.duration_s,
                    distance_m=footpath.distance_m,
                )
                queue.append((footpath.to_stop_id, candidate_arrival))


def _merge_transit_legs(legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for leg in legs:
        if (
            merged
            and merged[-1]["mode"] == "transit"
            and leg["mode"] == "transit"
            and merged[-1].get("trip_id")
            and merged[-1]["trip_id"] == leg.get("trip_id")
            and merged[-1].get("to_stop_id") == leg.get("from_stop_id")
        ):
            merged[-1]["to_stop_id"] = leg.get("to_stop_id")
            merged[-1]["arrival_time"] = leg.get("arrival_time")
            merged[-1]["duration_s"] += int(leg.get("duration_s", 0))
            continue
        merged.append(leg)
    return merged


def plan_journey_with_csa(
    schedule: CsaSchedule,
    origin_lat: float,
    origin_lon: float,
    destination_lat: float,
    destination_lon: float,
    departure_time: datetime,
    max_walking_distance_m: float = 1200.0,
    max_transfers: int = 4,
    search_window_minutes: int = 180,
) -> Optional[Dict[str, Any]]:
    if not schedule.stops or not schedule.connections:
        return None

    departure_dt = _coerce_datetime(departure_time)
    day_start = departure_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    departure_abs_s = int((departure_dt - day_start).total_seconds())

    origin_options = _build_walk_options(
        stops=schedule.stops,
        lat=origin_lat,
        lon=origin_lon,
        max_walking_distance_m=max_walking_distance_m,
    )
    destination_options = _build_walk_options(
        stops=schedule.stops,
        lat=destination_lat,
        lon=destination_lon,
        max_walking_distance_m=max_walking_distance_m,
    )

    if not origin_options or not destination_options:
        return None

    destination_option_by_stop = {opt.stop_id: opt for opt in destination_options}
    labels: Dict[str, int] = {stop_id: INF_TIME for stop_id in schedule.stops}
    parents: Dict[str, ParentStep] = {}

    earliest_seed = INF_TIME
    for option in origin_options:
        arrival_s = departure_abs_s + option.duration_s
        if arrival_s < labels[option.stop_id]:
            labels[option.stop_id] = arrival_s
            parents[option.stop_id] = ParentStep(
                kind="access",
                prev_stop_id=None,
                expanded_connection=None,
                duration_s=option.duration_s,
                distance_m=option.distance_m,
            )
            _relax_footpaths(
                start_stop_id=option.stop_id,
                start_arrival_s=arrival_s,
                schedule=schedule,
                labels=labels,
                parents=parents,
            )
        earliest_seed = min(earliest_seed, labels[option.stop_id])

    if earliest_seed >= INF_TIME:
        return None

    search_end_abs_s = departure_abs_s + max(30, search_window_minutes) * 60
    for expanded in _expand_connections(schedule.connections):
        if expanded.departure_abs_s < earliest_seed:
            continue
        if expanded.departure_abs_s > search_end_abs_s:
            break

        from_arrival = labels.get(expanded.connection.from_stop_id, INF_TIME)
        if from_arrival <= expanded.departure_abs_s and expanded.arrival_abs_s < labels.get(
            expanded.connection.to_stop_id, INF_TIME
        ):
            labels[expanded.connection.to_stop_id] = expanded.arrival_abs_s
            parents[expanded.connection.to_stop_id] = ParentStep(
                kind="connection",
                prev_stop_id=expanded.connection.from_stop_id,
                expanded_connection=expanded,
                duration_s=expanded.arrival_abs_s - expanded.departure_abs_s,
                distance_m=0.0,
            )
            _relax_footpaths(
                start_stop_id=expanded.connection.to_stop_id,
                start_arrival_s=expanded.arrival_abs_s,
                schedule=schedule,
                labels=labels,
                parents=parents,
            )

    best_stop_id: Optional[str] = None
    best_total_arrival = INF_TIME
    best_egress: Optional[WalkOption] = None
    for stop_id, option in destination_option_by_stop.items():
        arrival_s = labels.get(stop_id, INF_TIME)
        if arrival_s >= INF_TIME:
            continue
        total_arrival = arrival_s + option.duration_s
        if total_arrival < best_total_arrival:
            best_total_arrival = total_arrival
            best_stop_id = stop_id
            best_egress = option

    if not best_stop_id or not best_egress:
        return None

    path_steps: List[Tuple[str, ParentStep]] = []
    current_stop_id = best_stop_id
    access_stop_id: Optional[str] = None
    access_step: Optional[ParentStep] = None
    while True:
        parent = parents.get(current_stop_id)
        if parent is None:
            return None
        if parent.kind == "access":
            access_stop_id = current_stop_id
            access_step = parent
            break
        path_steps.append((current_stop_id, parent))
        if not parent.prev_stop_id:
            return None
        current_stop_id = parent.prev_stop_id

    if not access_stop_id or not access_step:
        return None

    path_steps.reverse()
    legs: List[Dict[str, Any]] = []

    if access_step.duration_s > 0:
        legs.append(
            {
                "mode": "walk",
                "from_stop_id": None,
                "to_stop_id": access_stop_id,
                "route_id": None,
                "trip_id": None,
                "departure_time": _seconds_to_datetime(day_start, departure_abs_s),
                "arrival_time": _seconds_to_datetime(day_start, labels[access_stop_id]),
                "duration_s": access_step.duration_s,
                "distance_m": round(access_step.distance_m, 1),
            }
        )

    for arrival_stop_id, parent in path_steps:
        if not parent.prev_stop_id:
            return None

        if parent.kind == "connection":
            expanded = parent.expanded_connection
            if expanded is None:
                return None
            legs.append(
                {
                    "mode": "transit",
                    "from_stop_id": parent.prev_stop_id,
                    "to_stop_id": arrival_stop_id,
                    "route_id": expanded.connection.route_id,
                    "trip_id": expanded.connection.trip_id,
                    "departure_time": _seconds_to_datetime(day_start, expanded.departure_abs_s),
                    "arrival_time": _seconds_to_datetime(day_start, expanded.arrival_abs_s),
                    "duration_s": parent.duration_s,
                    "distance_m": None,
                }
            )
            continue

        if parent.kind == "footpath":
            arrival_abs_s = labels[arrival_stop_id]
            departure_abs = arrival_abs_s - parent.duration_s
            legs.append(
                {
                    "mode": "walk",
                    "from_stop_id": parent.prev_stop_id,
                    "to_stop_id": arrival_stop_id,
                    "route_id": None,
                    "trip_id": None,
                    "departure_time": _seconds_to_datetime(day_start, departure_abs),
                    "arrival_time": _seconds_to_datetime(day_start, arrival_abs_s),
                    "duration_s": parent.duration_s,
                    "distance_m": round(parent.distance_m, 1),
                }
            )

    if best_egress.duration_s > 0:
        final_stop_arrival = labels[best_stop_id]
        legs.append(
            {
                "mode": "walk",
                "from_stop_id": best_stop_id,
                "to_stop_id": None,
                "route_id": None,
                "trip_id": None,
                "departure_time": _seconds_to_datetime(day_start, final_stop_arrival),
                "arrival_time": _seconds_to_datetime(day_start, best_total_arrival),
                "duration_s": best_egress.duration_s,
                "distance_m": round(best_egress.distance_m, 1),
            }
        )

    merged_legs = _merge_transit_legs(legs)
    transit_legs = [leg for leg in merged_legs if leg["mode"] == "transit"]
    transfers = max(0, len(transit_legs) - 1)
    if transfers > max_transfers:
        return None

    walking_duration_s = sum(int(leg["duration_s"]) for leg in merged_legs if leg["mode"] == "walk")
    transit_duration_s = sum(
        int(leg["duration_s"]) for leg in merged_legs if leg["mode"] == "transit"
    )

    return {
        "departure_time": _seconds_to_datetime(day_start, departure_abs_s),
        "arrival_time": _seconds_to_datetime(day_start, best_total_arrival),
        "duration_s": max(0, best_total_arrival - departure_abs_s),
        "transfers": transfers,
        "walking_duration_s": walking_duration_s,
        "transit_duration_s": transit_duration_s,
        "legs": merged_legs,
    }
