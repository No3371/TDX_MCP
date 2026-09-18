"""Response projection.

TDX payloads are large; the relay returns only the fields an assistant needs,
and caps the record count, to keep the token cost of a tool result small.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

RAIL_LIMIT = 3
EVENT_LIMIT = 5


def records(payload: Any, key: Optional[str] = None) -> List[Any]:
    """v2 endpoints return a bare array, v3 wraps it in a named field."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        if key and isinstance(payload.get(key), list):
            return payload[key]
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


def name(value: Any) -> Optional[str]:
    """TDX names are either a plain string or a {Zh_tw, En} object."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return value.get("Zh_tw") or value.get("Zh_TW") or value.get("En")
    return None


def first(source: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in source and source[key] not in (None, ""):
            return source[key]
    return None


def compact(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def tra_trains(payload: Any, limit: int = RAIL_LIMIT) -> List[Dict[str, Any]]:
    out = []
    for item in records(payload, "TrainTimetables")[:limit]:
        info = item.get("TrainInfo", {}) if isinstance(item, Mapping) else {}
        stops = item.get("StopTimes", []) if isinstance(item, Mapping) else []
        origin = stops[0] if stops else {}
        destination = stops[-1] if stops else {}
        out.append(
            compact(
                {
                    "train_no": info.get("TrainNo"),
                    "train_type": name(info.get("TrainTypeName")),
                    "from": name(origin.get("StationName")),
                    "depart": origin.get("DepartureTime"),
                    "to": name(destination.get("StationName")),
                    "arrive": destination.get("ArrivalTime"),
                }
            )
        )
    return out


def thsr_trains(payload: Any, limit: int = RAIL_LIMIT) -> List[Dict[str, Any]]:
    out = []
    for item in records(payload, "DailyTimetables")[:limit]:
        if not isinstance(item, Mapping):
            continue
        info = item.get("DailyTrainInfo", {})
        origin = item.get("OriginStopTime", {})
        destination = item.get("DestinationStopTime", {})
        out.append(
            compact(
                {
                    "train_no": info.get("TrainNo"),
                    "from": name(origin.get("StationName")),
                    "depart": origin.get("DepartureTime"),
                    "to": name(destination.get("StationName")),
                    "arrive": destination.get("ArrivalTime"),
                }
            )
        )
    return out


def fares(payload: Any, key: str, limit: int = RAIL_LIMIT) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in records(payload, key):
        if not isinstance(item, Mapping):
            continue
        for fare in item.get("Fares", []) or []:
            out.append(
                compact(
                    {
                        "ticket_type": fare.get("TicketType"),
                        "fare_class": fare.get("FareClass"),
                        "cabin_class": fare.get("CabinClass"),
                        "price": fare.get("Price"),
                    }
                )
            )
            if len(out) >= limit * 3:
                return out
    return out


def stations(payload: Any, key: str, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
    keyword = (keyword or "").strip()
    out = []
    for item in records(payload, key):
        if not isinstance(item, Mapping):
            continue
        station_name = name(item.get("StationName")) or ""
        if keyword and keyword not in station_name:
            continue
        out.append(compact({"station_id": item.get("StationID"), "station_name": station_name}))
        if len(out) >= limit:
            break
    return out


_EVENT_TITLE = ("Title", "Description", "Comment", "IncidentName")
_EVENT_ROAD = ("RoadName", "SectionName", "Road", "LocationDescription", "Section")
_EVENT_START = ("StartTime", "UpdateTime", "PublishTime", "OccurTime")
_EVENT_END = ("EndTime", "ExpectedEndTime")


def events(payload: Any, limit: int = EVENT_LIMIT) -> List[Dict[str, Any]]:
    out = []
    for item in records(payload):
        if not isinstance(item, Mapping):
            continue
        out.append(
            compact(
                {
                    "title": name(first(item, _EVENT_TITLE)),
                    "road": name(first(item, _EVENT_ROAD)),
                    "direction": item.get("Direction"),
                    "start": first(item, _EVENT_START),
                    "end": first(item, _EVENT_END),
                }
            )
        )
        if len(out) >= limit:
            break
    return out
