"""TDX API paths used by the relay, in one place.

TDX revises its service catalogue from time to time, so every path here can be
overridden with an environment variable without touching the code.  Check the
current paths against the TDX swagger before deploying.
"""

from __future__ import annotations

import os

PATHS = {
    "tra_station": "/v3/Rail/TRA/Station",
    "tra_od_timetable": "/v3/Rail/TRA/DailyTrainTimetable/OD/{origin}/to/{destination}/{date}",
    "tra_od_fare": "/v3/Rail/TRA/ODFare/{origin}/to/{destination}",
    "thsr_station": "/v2/Rail/THSR/Station",
    "thsr_od_timetable": "/v2/Rail/THSR/DailyTimetable/OD/{origin}/to/{destination}/{date}",
    "thsr_od_fare": "/v2/Rail/THSR/ODFare/{origin}/to/{destination}",
    "event_city": "/v2/Road/Traffic/Live/News/City/{city}",
    "event_provincial": "/v2/Road/Traffic/Live/News/Highway/ProvincialHighway",
    "event_freeway": "/v2/Road/Traffic/Live/News/Highway/Freeway",
}


def path(name: str, **kwargs: str) -> str:
    template = os.environ.get(f"RELAY_PATH_{name.upper()}", PATHS[name])
    return template.format(**kwargs)
