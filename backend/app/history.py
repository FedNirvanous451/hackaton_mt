"""Local, read-only CSV fixture and 15-second replay for backend development."""

import asyncio
import csv
import re
from bisect import bisect_right
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from .models import ScheduledArrival, Snapshot, Telemetry, VehicleRegistration
from .service import DispatcherService
from .store import LiveStore

POINT = re.compile(r"POINT\s*\(\s*([\d.\-]+)\s+([\d.\-]+)\s*\)", re.I)


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value[:26])


def _float(value: str | None) -> float | None:
    return float(value) if value and value.strip() else None


def _int(value: str | None) -> int | None:
    return int(value) if value and value.strip() else None


def _rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


class HistoricalDataset:
    def __init__(self, root: Path, split: str, ml_url: str | None,
                 history_minutes: int = 15) -> None:
        if split not in {"train", "test", "validate"}:
            raise ValueError("split must be train, test or validate")
        base = root / split
        schedule_path = base / ("schedule_plan.csv" if split == "validate" else "schedule.csv")
        points_path = base / "points.csv" if split == "validate" else root / "labels" / f"labels_{split}.csv"
        self.store = LiveStore()
        self.service = DispatcherService(self.store, ml_url, history_minutes)
        self.split = split
        self.points_by_id: dict[str, tuple[int, datetime]] = {}
        for row in _rows(schedule_path):
            geom = POINT.fullmatch(row["geom"].strip())
            if geom is None:
                continue
            arrival = ScheduledArrival(arrival_id=int(row["tt_action_item_id"]), tr_id=int(row["tr_id"]),
                planned_at=_datetime(row["time_begin"]), lon=float(geom[1]), lat=float(geom[2]),
                address=row.get("building_address") or None)
            self.store.upsert_arrival(arrival)
            actual = _datetime(row.get("time_fact_begin"))
            if actual is not None:
                self.store.observe_arrival(arrival.arrival_id, actual)
        for row in _rows(points_path):
            tr_id, at = int(row["tr_id"]), _datetime(row["T"])
            self.points_by_id[row["sample_id"]] = (tr_id, at)
            self.service.forecast_points[(tr_id, at)] = (int(row["target_stop_id"]), _float(row.get("cur_dev_s")))
        self.by_vehicle: dict[int, list[Telemetry]] = defaultdict(list)
        for row in _rows(base / "traffic.csv"):
            tr_id = int(row["tr_id"])
            unit_id = _int(row.get("unit_id"))
            if unit_id is not None and unit_id not in self.store.unit_to_tr:
                self.store.register(VehicleRegistration(tr_id=tr_id, unit_id=unit_id))
            valid = row.get("location_valid", "").lower() in {"true", "1"}
            lon, lat = _float(row.get("lon")), _float(row.get("lat"))
            valid = valid and lon is not None and lat is not None and -180 <= lon <= 180 and -90 <= lat <= 90
            self.by_vehicle[tr_id].append(Telemetry(tr_id=tr_id, unit_id=unit_id,
                packet_id=row.get("packet_id"), device_event_id=_int(row.get("device_event_id")),
                event_time=_datetime(row["event_time"]), gps_time=_datetime(row.get("gps_time")),
                received_at=_datetime(row.get("receive_time")),
                is_hist_data=row.get("is_hist_data", "").lower() in {"true", "1"},
                source="csv", lon=lon if valid else None, lat=lat if valid else None,
                alt=_float(row.get("alt")), speed_kmh=_float(row.get("speed")),
                heading_deg=_float(row.get("heading")), location_valid=valid))
        self.times_by_vehicle = {}
        for tr_id, items in self.by_vehicle.items():
            items.sort(key=lambda item: item.event_time)
            self.times_by_vehicle[tr_id] = [item.event_time for item in items]
        all_times = [item.event_time for items in self.by_vehicle.values() for item in (items[0], items[-1]) if items]
        if not all_times:
            raise ValueError("historical traffic is empty")
        start = min(all_times).replace(microsecond=0)
        start -= timedelta(seconds=start.second % 15)
        end = max(all_times)
        self.start, self.end = start, end
        self.step_seconds = 15
        self.cursor: datetime | None = None
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def timeline(self) -> dict:
        return {"split": self.split, "start": self.start, "end": self.end,
                "step_seconds": self.step_seconds,
                "steps": int((self.end - self.start).total_seconds() // self.step_seconds) + 1,
                "sample_count": len(self.points_by_id)}

    def status(self) -> dict:
        return {**self.timeline(), "running": self.task is not None and not self.task.done(),
                "cursor": self.cursor}

    async def snapshot(self, at: datetime) -> Snapshot:
        if at < self.start or at > self.end:
            raise ValueError("at is outside historical traffic range")
        async with self.lock:
            service = self.service
            service.live.clear()
            service.live_last_valid.clear()
            service.live_history.clear()
            cutoff = at - timedelta(minutes=service.ml_history_minutes)
            for tr_id, items in self.by_vehicle.items():
                times = self.times_by_vehicle[tr_id]
                end = bisect_right(times, at)
                if end == 0 or at - times[end - 1] > timedelta(seconds=90):
                    continue
                begin = bisect_right(times, cutoff - timedelta(microseconds=1))
                history = items[begin:end]
                if not history:
                    continue
                key = str(tr_id)
                service.live[key] = history[-1]
                service.live_history[key] = deque(history)
                valid = next((item for item in reversed(history) if item.location_valid), None)
                if valid:
                    service.live_last_valid[key] = valid
            return await service.snapshot(at=at, mode="historical", dataset_split=self.split)

    async def step(self, at: datetime | None = None, sample_id: str | None = None) -> Snapshot:
        if sample_id is not None:
            try:
                _, at = self.points_by_id[sample_id]
            except KeyError as exc:
                raise KeyError("sample_id not found") from exc
        if at is None:
            at = self.start if self.cursor is None else self.cursor + timedelta(seconds=self.step_seconds)
        snapshot = await self.snapshot(at)
        self.cursor = at
        await self.service.publish(snapshot)
        return snapshot

    async def run(self, interval_s: float) -> None:
        try:
            while self.cursor is None or self.cursor + timedelta(seconds=self.step_seconds) <= self.end:
                await self.step()
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

    async def reset(self) -> None:
        await self.stop()
        self.cursor = None
        self.service.prediction_cache.clear()
        self.service.alert_created.clear()
