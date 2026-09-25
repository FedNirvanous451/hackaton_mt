"""Read-only indexes for the selected dataset split; target labels are not loaded."""

import csv
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .models import Stop, Telemetry


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _float(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


class DataStore:
    """Indexes schedule and historical telemetry for efficient point-in-time lookup."""

    def __init__(self, dataset: Path, split: str = "validate"):
        if split not in ("validate", "train", "test"):
            raise ValueError("DATASET_SPLIT must be validate, train or test")
        self.dataset = dataset
        self.split = split
        self.stops: dict[int, list[Stop]] = defaultdict(list)
        self.stop_times: dict[int, list[datetime]] = {}
        self.traffic: dict[int, list[Telemetry]] = defaultdict(list)
        self.traffic_times: dict[int, list[datetime]] = {}
        self.valid_locations: dict[int, list[Telemetry]] = {}
        self.valid_location_times: dict[int, list[datetime]] = {}
        self.points: dict[int, list[dict]] = defaultdict(list)
        self.point_times: dict[int, list[datetime]] = {}
        self.points_by_id: dict[str, dict] = {}
        self.unit_to_tr: dict[int, int] = {}
        self.ambiguous_units: set[int] = set()
        self.stop_by_arrival: dict[int, Stop] = {}
        self.timeline: list[datetime] = []
        self._load()

    def _load(self) -> None:
        schedule_name = "schedule_plan.csv" if self.split == "validate" else "schedule.csv"
        with (self.dataset / self.split / schedule_name).open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                geom = row.get("geom", "")
                lon = lat = None
                if geom.startswith("POINT (") and geom.endswith(")"):
                    try:
                        lon, lat = map(float, geom[7:-1].split())
                    except ValueError:
                        pass
                tr_id = int(row["tr_id"])
                arrival_id = int(row["tt_action_item_id"])
                if arrival_id in self.stop_by_arrival:
                    raise ValueError(f"duplicate scheduled arrival ID: {arrival_id}")
                stop = Stop(stop_id=arrival_id, arrival_id=arrival_id, tr_id=tr_id,
                    planned_at=_dt(row["time_begin"]), lon=lon, lat=lat,
                    address=row.get("building_address") or None, geom=geom or None)
                self.stops[tr_id].append(stop)
                self.stop_by_arrival[arrival_id] = stop
        for tr_id, stops in self.stops.items():
            stops.sort(key=lambda x: x.planned_at)
            self.stop_times[tr_id] = [x.planned_at for x in stops]
        points_path = (self.dataset / "validate" / "points.csv" if self.split == "validate"
                       else self.dataset / "labels" / f"labels_{self.split}.csv")
        with points_path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                point = {"sample_id": row["sample_id"], "tr_id": int(row["tr_id"]),
                    "T": _dt(row["T"]), "target_stop_id": int(row["target_stop_id"]),
                    "target_time_begin": _dt(row["target_time_begin"]),
                    "cur_dev_s": _float(row["cur_dev_s"])}
                self.points[point["tr_id"]].append(point)
                self.points_by_id[point["sample_id"]] = point
                self.timeline.append(point["T"])
        for tr_id, points in self.points.items():
            points.sort(key=lambda x: x["T"])
            self.point_times[tr_id] = [x["T"] for x in points]
        self.timeline = sorted(set(self.timeline))

        unit_candidates: dict[int, set[int]] = defaultdict(set)
        with (self.dataset / self.split / "traffic.csv").open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                tr_id = int(row["tr_id"])
                unit_id = int(row["unit_id"]) if row["unit_id"] else None
                if unit_id is not None:
                    unit_candidates[unit_id].add(tr_id)
                self.traffic[tr_id].append(Telemetry(tr_id=tr_id, unit_id=unit_id,
                    event_time=_dt(row["event_time"]),
                    received_at=_dt(row["receive_time"]) if row.get("receive_time") else None,
                    packet_id=row.get("packet_id") or None,
                    device_event_id=int(row["device_event_id"]) if row.get("device_event_id") else None,
                    gps_time=_dt(row["gps_time"]) if row.get("gps_time") else None,
                    is_hist_data=row["is_hist_data"].lower() == "true" if row.get("is_hist_data") else None,
                    source="csv", lon=_float(row["lon"]), lat=_float(row["lat"]),
                    alt=_float(row["alt"]), speed_kmh=_float(row["speed"]),
                    heading_deg=_float(row["heading"]),
                    location_valid=row["location_valid"].lower() == "true"))
        for tr_id, records in self.traffic.items():
            records.sort(key=lambda x: x.event_time)
            self.traffic_times[tr_id] = [x.event_time for x in records]
            valid = [x for x in records if x.location_valid and x.lon is not None and x.lat is not None]
            self.valid_locations[tr_id] = valid
            self.valid_location_times[tr_id] = [x.event_time for x in valid]
        for unit_id, tr_ids in unit_candidates.items():
            if len(tr_ids) == 1:
                self.unit_to_tr[unit_id] = next(iter(tr_ids))
            else:
                self.ambiguous_units.add(unit_id)

    def telemetry_at(self, tr_id: int, at: datetime,
                     history_minutes: int = 15) -> tuple[Telemetry | None, list[Telemetry]]:
        times = self.traffic_times.get(tr_id, [])
        i = bisect_right(times, at)
        if not i:
            return None, []
        start = bisect_right(times, at - timedelta(minutes=history_minutes))
        return self.traffic[tr_id][i - 1], self.traffic[tr_id][start:i]

    def point_at(self, tr_id: int, at: datetime) -> dict | None:
        times = self.point_times.get(tr_id, [])
        i = bisect_right(times, at)
        return self.points[tr_id][i - 1] if i else None

    def valid_location_at(self, tr_id: int, at: datetime) -> Telemetry | None:
        times = self.valid_location_times.get(tr_id, [])
        i = bisect_right(times, at)
        return self.valid_locations[tr_id][i - 1] if i else None

    def target_at(self, tr_id: int, at: datetime) -> Stop | None:
        """First planned stop in the strict (T+10m, T+15m] window."""
        times = self.stop_times.get(tr_id, [])
        i = bisect_right(times, at + timedelta(minutes=10))
        if i < len(times) and times[i] <= at + timedelta(minutes=15):
            return self.stops[tr_id][i]
        return None
