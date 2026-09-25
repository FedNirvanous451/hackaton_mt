"""Point-in-time orchestration for map state, predictions and incident cards."""

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .data import DataStore
from .models import Alert, MLRequest, MLResponse, MLTelemetry, Prediction, Snapshot, Stop, Summary, Telemetry, Vehicle

log = logging.getLogger(__name__)


def risk_for_delay(prediction_s: float | None) -> str:
    """Dashboard category; late class starts strictly above 120 seconds."""
    if prediction_s is None:
        return "unknown"
    if prediction_s > 120:
        return "red"
    if prediction_s > 60:
        return "yellow"
    return "green"


class DispatcherService:
    def __init__(self, data: DataStore, ml_url: str | None,
                 demo_schedule_shift_days: int = 0, ml_history_minutes: int = 15):
        self.data = data
        self.ml_url = ml_url.rstrip("/") if ml_url else None
        self.demo_schedule_shift_days = demo_schedule_shift_days
        self.ml_history_minutes = max(1, min(ml_history_minutes, 120))
        self.live: dict[str, Telemetry] = {}
        self.live_history: dict[str, deque[Telemetry]] = defaultdict(
            lambda: deque(maxlen=max(150, self.ml_history_minutes * 60)))
        self.live_last_valid: dict[str, Telemetry] = {}
        self.prediction_cache: dict[tuple[int, int], tuple[datetime, Prediction | None]] = {}
        self.alert_created: dict[tuple[str, str], datetime] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.last_snapshot: Snapshot | None = None
        self.last_live_snapshot: Snapshot | None = None
        self.ml_available = False
        self.replay_task: asyncio.Task | None = None
        self.replay_at: datetime | None = None
        self.replay_index = 0
        self.predict_requests = 0
        self.predict_success = 0
        self.predict_failures = 0
        self.predict_latency_ms_total = 0.0
        self.ml_latency_ms: deque[float] = deque(maxlen=2000)
        self.ingest_latency_ms: deque[float] = deque(maxlen=2000)
        self.duplicate_packets = 0
        self.out_of_order_packets = 0
        self.websocket_dropped_snapshots = 0

    @staticmethod
    def percentiles(values: deque[float]) -> dict[str, float | None]:
        ordered = sorted(values)
        if not ordered:
            return {"p50": None, "p95": None}
        return {"p50": ordered[(len(ordered) - 1) // 2],
                "p95": ordered[int((len(ordered) - 1) * 0.95)]}

    def _predict_sync(self, request: MLRequest) -> MLResponse | None:
        if not self.ml_url:
            return None
        body = request.model_dump_json().encode("utf-8")
        http_request = Request(f"{self.ml_url}/predict", body,
            {"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(http_request, timeout=1.5) as response:
                return MLResponse.model_validate_json(response.read())
        except (OSError, HTTPError, URLError, ValueError) as exc:
            log.warning("ML unavailable; using current deviation baseline: %s", exc)
            return None

    async def _prediction(self, tr_id: int, at: datetime, target: Stop,
                          cur_dev_s: float | None, history: list[Telemetry],
                          sample_id: str | None, mode: str) -> Prediction | None:
        cache_key = (tr_id, target.arrival_id)
        cached = self.prediction_cache.get(cache_key) if mode == "live" else None
        if cached and timedelta(0) <= at - cached[0] < timedelta(seconds=15):
            return cached[1]
        request = MLRequest(sample_id=sample_id, tr_id=tr_id, T=at,
            target_stop_id=target.stop_id, target_time_begin=target.planned_at,
            target_geom=target.geom, cur_dev_s=cur_dev_s,
            telemetry_history=[MLTelemetry(packet_id=item.packet_id, tr_id=tr_id,
                unit_id=item.unit_id, event_time=item.event_time,
                device_event_id=item.device_event_id,
                location_valid=item.location_valid, gps_time=item.gps_time,
                lon=item.lon, lat=item.lat, alt=item.alt,
                speed=item.speed_kmh, heading=item.heading_deg,
                receive_time=item.received_at, is_hist_data=item.is_hist_data)
                for item in history if item.event_time <= at])
        result = None
        if self.ml_url:
            started = time.perf_counter()
            self.predict_requests += 1
            result = await asyncio.to_thread(self._predict_sync, request)
            latency_ms = (time.perf_counter() - started) * 1000
            self.predict_latency_ms_total += latency_ms
            self.ml_latency_ms.append(latency_ms)
            if result and result.status == "ok" and result.prediction_s is not None:
                self.predict_success += 1
            else:
                self.predict_failures += 1
        if result and result.status == "ok" and result.prediction_s is not None:
            self.ml_available = True
            value, source = result.prediction_s, "ml"
        elif cur_dev_s is not None:
            self.ml_available = False
            value, source = cur_dev_s, "baseline"
        else:
            self.ml_available = False
            if mode == "live":
                self.prediction_cache[cache_key] = (at, None)
            return None
        prediction = Prediction(tr_id=tr_id, at=at, sample_id=sample_id, target_stop=target,
            prediction_s=value, predicted_arrival=target.planned_at + timedelta(seconds=value),
            horizon_s=(target.planned_at - at).total_seconds(),
            delay_probability=result.delay_probability if source == "ml" else None,
            source=source, model_version=result.model_version if source == "ml" else None,
            pattern=result.pattern if source == "ml" else None,
            reason=result.reason if source == "ml" else None)
        if mode == "live":
            self.prediction_cache[cache_key] = (at, prediction)
        return prediction

    async def _vehicle(self, telemetry: Telemetry, history: list[Telemetry],
                       last_valid: Telemetry | None, at: datetime, mode: str) -> Vehicle:
        tr_id = telemetry.tr_id
        vehicle_id = str(tr_id) if tr_id is not None else f"unit:{telemetry.unit_id}"
        point = self.data.point_at(tr_id, at) if tr_id is not None and mode == "historical" else None
        cur_dev_s = point["cur_dev_s"] if point and at - point["T"] <= timedelta(minutes=5) else None
        offset = timedelta(days=self.demo_schedule_shift_days if mode == "live" else 0)
        target = self.data.target_at(tr_id, at - offset) if tr_id is not None else None
        if target and offset:
            target = target.model_copy(update={"planned_at": target.planned_at + offset})
        sample_id = None
        if mode == "historical" and point and point["T"] == at:
            stop = self.data.stop_by_arrival.get(point["target_stop_id"])
            if stop and stop.tr_id == tr_id and stop.planned_at == point["target_time_begin"]:
                if timedelta(minutes=10) < stop.planned_at - at <= timedelta(minutes=15):
                    target = stop
                    sample_id = point["sample_id"]
        prediction = await self._prediction(tr_id, at, target, cur_dev_s, history, sample_id, mode) if target and tr_id is not None else None
        stale = (at - telemetry.event_time) > timedelta(seconds=90)
        location_stale = last_valid is None or at - last_valid.event_time > timedelta(seconds=90)
        risk = risk_for_delay(prediction.prediction_s if prediction else None)
        forecast_status = (prediction.source if prediction else
            "unknown_vehicle" if tr_id is None else "no_target" if target is None else "insufficient_data")
        return Vehicle(vehicle_id=vehicle_id, tr_id=tr_id, unit_id=telemetry.unit_id,
            observed_at=telemetry.event_time, received_at=telemetry.received_at,
            location_observed_at=last_valid.event_time if last_valid else None,
            lon=last_valid.lon if last_valid else None,
            lat=last_valid.lat if last_valid else None,
            speed_kmh=telemetry.speed_kmh, heading_deg=telemetry.heading_deg,
            location_valid=telemetry.location_valid, location_stale=location_stale,
            stale=stale, target_arrival=target,
            forecast_status=forecast_status, prediction=prediction, risk=risk)

    async def snapshot(self, at: datetime | None = None, mode: str = "historical") -> Snapshot:
        if mode == "historical":
            at = at or self.data.timeline[-1]
            rows = []
            for tr_id in self.data.traffic:
                telemetry, history = self.data.telemetry_at(tr_id, at, self.ml_history_minutes)
                if telemetry:
                    rows.append((telemetry, history, self.data.valid_location_at(tr_id, at)))
        else:
            at = at or datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
            rows = [(item, [x for x in self.live_history[key]
                            if at - timedelta(minutes=self.ml_history_minutes) <= x.event_time <= at],
                     self.live_last_valid.get(key))
                    for key, item in self.live.items()]
        vehicles = await asyncio.gather(*(self._vehicle(item, history, valid, at, mode)
                                          for item, history, valid in rows))
        alerts = []
        stops: dict[int, Stop] = {}
        for vehicle in vehicles:
            if vehicle.prediction:
                prediction = vehicle.prediction
                stops[prediction.target_stop.arrival_id] = prediction.target_stop
                if vehicle.risk in ("yellow", "red"):
                    alert_id = f"{vehicle.vehicle_id}:{prediction.target_stop.arrival_id}"
                    created_key = (mode, alert_id)
                    created_at = self.alert_created.setdefault(created_key, at)
                    alerts.append(Alert(alert_id=alert_id,
                        vehicle_id=vehicle.vehicle_id, tr_id=vehicle.tr_id, risk=vehicle.risk,
                        target_stop=prediction.target_stop,
                        predicted_delay_s=prediction.prediction_s,
                        predicted_arrival=prediction.predicted_arrival,
                        delay_probability=prediction.delay_probability,
                        reason=prediction.reason, pattern=prediction.pattern,
                        recommendation="Проверьте движение ТС и возможность регулирования интервала.",
                        source=prediction.source, created_at=created_at, updated_at=at))
        summary = Summary(total_vehicles=len(vehicles),
            located_vehicles=sum(v.lon is not None for v in vehicles),
            stale_vehicles=sum(v.stale for v in vehicles),
            alerts_yellow=sum(a.risk == "yellow" for a in alerts),
            alerts_red=sum(a.risk == "red" for a in alerts))
        result = Snapshot(at=at, mode=mode, dataset_split=self.data.split,
            vehicles=vehicles, alerts=alerts,
            stops=list(stops.values()), summary=summary, ml_available=self.ml_available,
            demo_schedule_shift_days=self.demo_schedule_shift_days if mode == "live" else 0)
        self.last_snapshot = result
        if mode == "live":
            self.last_live_snapshot = result
        return result

    async def publish(self, snapshot: Snapshot) -> None:
        message = {"type": "snapshot", "data": snapshot.model_dump(mode="json")}
        for queue in tuple(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                    self.websocket_dropped_snapshots += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    async def ingest(self, item: Telemetry) -> Snapshot:
        started = time.perf_counter()
        key = str(item.tr_id) if item.tr_id is not None else f"unit:{item.unit_id}"
        if item.received_at is None:
            item.received_at = datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
        previous = self.live.get(key)
        if previous and item.event_time <= previous.event_time:
            if item.event_time == previous.event_time:
                self.duplicate_packets += 1
            else:
                self.out_of_order_packets += 1
            return self.last_live_snapshot or await self.snapshot(mode="live")
        self.live[key] = item
        self.live_history[key].append(item)
        if item.location_valid and item.lon is not None and item.lat is not None:
            self.live_last_valid[key] = item
        at = max(x.event_time for x in self.live.values())
        result = await self.snapshot(at=at, mode="live")
        await self.publish(result)
        self.ingest_latency_ms.append((time.perf_counter() - started) * 1000)
        return result

    async def run_replay(self, interval_s: float) -> None:
        """Stream all provided forecast times into the dashboard WebSocket."""
        try:
            while self.replay_index < len(self.data.timeline):
                at = self.data.timeline[self.replay_index]
                self.replay_at = at
                await self.publish(await self.snapshot(at=at))
                self.replay_index += 1
                await asyncio.sleep(interval_s)
        finally:
            self.replay_task = None

    async def reset_replay(self) -> None:
        await self.stop_replay()
        self.replay_index = 0
        self.replay_at = None
        self.last_snapshot = None
        for key in tuple(self.alert_created):
            if key[0] == "historical":
                self.alert_created.pop(key, None)

    async def stop_replay(self) -> None:
        if self.replay_task:
            task = self.replay_task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.replay_task = None
