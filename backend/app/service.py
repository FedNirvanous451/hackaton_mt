"""Live NDTP orchestration and the HTTP adapter for ml_core."""

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .features import build_ml_request, movement_metrics
from .models import (Alert, MLPredictionRequest, MLPredictionResponse, Prediction,
                     ScheduledArrival, Snapshot, Summary, Telemetry, TrackPoint, Vehicle, VehicleRegistration)
from .store import LiveStore

log = logging.getLogger(__name__)
MOSCOW = timezone(timedelta(hours=3))


def risk_for_delay(prediction_s: float | None) -> str:
    if prediction_s is None:
        return "unknown"
    if prediction_s > 120:
        return "red"
    if prediction_s > 60:
        return "yellow"
    return "green"


class DispatcherService:
    def __init__(self, store: LiveStore, ml_url: str | None,
                 ml_history_minutes: int = 15) -> None:
        self.store = store
        self.ml_url = ml_url.rstrip("/") if ml_url else None
        self.ml_history_minutes = max(5, min(ml_history_minutes, 120))
        self.live: dict[str, Telemetry] = {}
        self.live_history: dict[str, deque[Telemetry]] = defaultdict(
            lambda: deque(maxlen=max(300, self.ml_history_minutes * 60)))
        self.live_last_valid: dict[str, Telemetry] = {}
        self.prediction_cache: dict[tuple[int, int], tuple[datetime, Prediction | None]] = {}
        self.last_targets: dict[int, tuple[datetime, ScheduledArrival]] = {}
        self.last_predictions: dict[int, Prediction] = {}
        self.alert_created: dict[str, datetime] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self.last_snapshot: Snapshot | None = None
        self.ml_available = False
        self.predict_requests = 0
        self.predict_success = 0
        self.predict_failures = 0
        self.ml_latency_ms: deque[float] = deque(maxlen=2000)
        self.ingest_latency_ms: deque[float] = deque(maxlen=2000)
        self.duplicate_packets = 0
        self.out_of_order_packets = 0
        self.websocket_dropped_snapshots = 0
        self.forecast_points: dict[tuple[int, datetime], tuple[int, float | None]] = {}
        self.processed_packets = 0

    @staticmethod
    def track_point(item: Telemetry) -> TrackPoint:
        return TrackPoint(vehicle_id=str(item.tr_id) if item.tr_id is not None else f"unit:{item.unit_id}",
            tr_id=item.tr_id, unit_id=item.unit_id, event_time=item.event_time,
            lon=item.lon, lat=item.lat, speed_kmh=item.speed_kmh,
            location_valid=item.location_valid, source=item.source)

    @staticmethod
    def percentiles(values: deque[float]) -> dict[str, float | None]:
        ordered = sorted(values)
        if not ordered:
            return {"p50": None, "p95": None}
        return {"p50": ordered[(len(ordered) - 1) // 2],
                "p95": ordered[int((len(ordered) - 1) * 0.95)]}

    def register(self, item: VehicleRegistration) -> VehicleRegistration:
        self.store.register(item)
        old_key, new_key = f"unit:{item.unit_id}", str(item.tr_id)
        if old_key in self.live:
            old = self.live.pop(old_key).model_copy(update={"tr_id": item.tr_id})
            if new_key not in self.live or old.event_time > self.live[new_key].event_time:
                self.live[new_key] = old
            history = [x.model_copy(update={"tr_id": item.tr_id})
                       for x in self.live_history.pop(old_key, ())]
            merged = sorted([*self.live_history[new_key], *history], key=lambda x: x.event_time)
            self.live_history[new_key] = deque(merged, maxlen=self.live_history[new_key].maxlen)
            valid = self.live_last_valid.pop(old_key, None)
            if valid and (new_key not in self.live_last_valid or
                          valid.event_time > self.live_last_valid[new_key].event_time):
                self.live_last_valid[new_key] = valid.model_copy(update={"tr_id": item.tr_id})
        return item

    def _predict_sync(self, request: MLPredictionRequest) -> MLPredictionResponse | None:
        if not self.ml_url:
            return None
        http_request = Request(f"{self.ml_url}/predict", request.model_dump_json().encode(),
                               {"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(http_request, timeout=2.0) as response:
                return MLPredictionResponse.model_validate_json(response.read())
        except (OSError, HTTPError, URLError, ValueError) as exc:
            log.warning("ML request failed: %s", exc)
            return None

    async def _prediction(self, tr_id: int, at: datetime, target,
                          history: list[Telemetry]) -> Prediction | None:
        cache_key = (tr_id, target.arrival_id)
        cached = self.prediction_cache.get(cache_key)
        if cached and timedelta(0) <= at - cached[0] < timedelta(seconds=15):
            return cached[1]
        point = self.forecast_points.get((tr_id, at))
        current_dev_s = point[1] if point else self.store.deviation_at(tr_id, at)
        request = build_ml_request(at, target, history, current_dev_s)
        result = None
        if self.ml_url:
            started = time.perf_counter()
            self.predict_requests += 1
            result = await asyncio.to_thread(self._predict_sync, request)
            self.ml_latency_ms.append((time.perf_counter() - started) * 1000)
            if result:
                self.predict_success += 1
            else:
                self.predict_failures += 1
        if result:
            self.ml_available = True
            value, source = result.prediction_delay_s, "ml"
        elif current_dev_s is not None:
            self.ml_available = False
            value, source = current_dev_s, "baseline"
        else:
            self.ml_available = False
            self.prediction_cache[cache_key] = (at, None)
            return None
        prediction = Prediction(tr_id=tr_id, at=at, target_stop=target,
            prediction_s=value, predicted_arrival=target.planned_at + timedelta(seconds=value),
            horizon_s=(target.planned_at - at).total_seconds(), source=source,
            model_version=result.model_version if result else None,
            patterns=result.patterns if result else [])
        self.prediction_cache[cache_key] = (at, prediction)
        return prediction

    async def _vehicle(self, key: str, telemetry: Telemetry, at: datetime) -> Vehicle:
        tr_id = telemetry.tr_id
        point = self.forecast_points.get((tr_id, at)) if tr_id is not None else None
        target = (self.store.arrivals.get(point[0]) if point else self.store.target_at(tr_id, at)) if tr_id is not None else None
        if tr_id is not None:
            if target is not None:
                self.last_targets[tr_id] = (at, target)
            else:
                remembered = self.last_targets.get(tr_id)
                if remembered and remembered[0] <= at <= remembered[1].planned_at:
                    target = remembered[1]
        history = [x for x in self.live_history[key]
                   if at - timedelta(minutes=self.ml_history_minutes) <= x.event_time <= at]
        fresh_history = bool(history and at - history[-1].event_time <= timedelta(seconds=90))
        prediction = await self._prediction(tr_id, at, target, history) if target and fresh_history else None
        prediction_is_retained = False
        if prediction is not None:
            self.last_predictions[tr_id] = prediction
        elif tr_id is not None:
            remembered_prediction = self.last_predictions.get(tr_id)
            if remembered_prediction and remembered_prediction.at <= at and (target is None or
                    remembered_prediction.target_stop.arrival_id == target.arrival_id):
                prediction = remembered_prediction
                target = remembered_prediction.target_stop
                prediction_is_retained = True
        display_only_target = False
        if target is None and tr_id is not None:
            target = self.store.next_arrival_at(tr_id, at)
            display_only_target = target is not None
        valid = self.live_last_valid.get(key)
        stale = at - telemetry.event_time > timedelta(seconds=90)
        location_stale = valid is None or at - valid.event_time > timedelta(seconds=90)
        segment_speed, idle_time = movement_metrics(history)
        if prediction:
            status = "last_known" if prediction_is_retained else prediction.source
        elif tr_id is None:
            status = "unknown_vehicle"
        elif target is None:
            status = "no_target"
        else:
            status = "outside_horizon" if display_only_target else "insufficient_data"
        return Vehicle(vehicle_id=key, tr_id=tr_id, unit_id=telemetry.unit_id,
            source=telemetry.source,
            observed_at=telemetry.event_time, received_at=telemetry.received_at,
            location_observed_at=valid.event_time if valid else None,
            lon=valid.lon if valid else None, lat=valid.lat if valid else None,
            speed_kmh=telemetry.speed_kmh, heading_deg=telemetry.heading_deg,
            location_valid=telemetry.location_valid, location_stale=location_stale,
            stale=stale, doors_closed=telemetry.doors_closed,
            schedule_deviation_s=(point[1] if point else self.store.deviation_at(tr_id, at)) if tr_id is not None else None,
            segment_speed_kmh=segment_speed, idle_time_s=idle_time,
            target_arrival=target, prediction=prediction,
            forecast_status=status, risk=risk_for_delay(prediction.prediction_s if prediction else None))

    async def snapshot(self, at: datetime | None = None, mode: str = "live",
                       dataset_split: str | None = None) -> Snapshot:
        at = at or datetime.now(MOSCOW).replace(tzinfo=None)
        vehicles = await asyncio.gather(*(self._vehicle(key, item, at)
                                          for key, item in self.live.items() if item.event_time <= at))
        alerts: list[Alert] = []
        stops = {}
        stop_start, stop_end = at - timedelta(minutes=5), at + timedelta(minutes=30)
        for vehicle in vehicles:
            if vehicle.tr_id is not None:
                for stop in self.store.stops_between(stop_start, stop_end, vehicle.tr_id):
                    stops[stop.arrival_id] = stop
            prediction = vehicle.prediction
            if prediction is None:
                continue
            stops[prediction.target_stop.arrival_id] = prediction.target_stop
            if vehicle.risk in ("yellow", "red"):
                alert_id = f"{vehicle.vehicle_id}:{prediction.target_stop.arrival_id}"
                created_at = self.alert_created.setdefault(alert_id, at)
                alerts.append(Alert(alert_id=alert_id, vehicle_id=vehicle.vehicle_id,
                    tr_id=prediction.tr_id, risk=vehicle.risk, target_stop=prediction.target_stop,
                    predicted_delay_s=prediction.prediction_s,
                    predicted_arrival=prediction.predicted_arrival,
                    patterns=prediction.patterns,
                    recommendation="Проверьте движение ТС и возможность регулирования интервала.",
                    source=prediction.source, created_at=created_at, updated_at=at))
        summary = Summary(total_vehicles=len(vehicles),
            located_vehicles=sum(v.lon is not None for v in vehicles),
            stale_vehicles=sum(v.stale for v in vehicles),
            alerts_yellow=sum(a.risk == "yellow" for a in alerts),
            alerts_red=sum(a.risk == "red" for a in alerts))
        result = Snapshot(at=at, mode=mode, dataset_split=dataset_split,
                          vehicles=vehicles, alerts=alerts,
                          stops=list(stops.values()), summary=summary,
                          ml_available=self.ml_available,
                          processed_packets=self.processed_packets)
        self.last_snapshot = result
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
        if item.tr_id is None and item.unit_id is not None:
            item = item.model_copy(update={"tr_id": self.store.unit_to_tr.get(item.unit_id)})
        key = str(item.tr_id) if item.tr_id is not None else f"unit:{item.unit_id}"
        if item.received_at is None:
            item.received_at = datetime.now(MOSCOW).replace(tzinfo=None)
        previous = self.live.get(key)
        if previous and item.event_time <= previous.event_time:
            if item.event_time == previous.event_time:
                self.duplicate_packets += 1
            else:
                self.out_of_order_packets += 1
            return self.last_snapshot or await self.snapshot(at=item.event_time)
        self.live[key] = item
        self.processed_packets += 1
        self.live_history[key].append(item)
        cutoff = item.event_time - timedelta(minutes=self.ml_history_minutes)
        while self.live_history[key] and self.live_history[key][0].event_time < cutoff:
            self.live_history[key].popleft()
        if item.location_valid and item.lon is not None and item.lat is not None:
            self.live_last_valid[key] = item
        at = max(x.event_time for x in self.live.values())
        snapshot = await self.snapshot(at=at)
        snapshot.track_points = [self.track_point(item)]
        await self.publish(snapshot)
        self.ingest_latency_ms.append((time.perf_counter() - started) * 1000)
        return snapshot
