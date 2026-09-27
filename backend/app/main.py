"""FastAPI transport backend: NDTP ingestion, ML predictions and data API."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .models import (Alert, ArrivalDeviation, ObservedArrival, Prediction,
                     ScheduledArrival, Snapshot, Telemetry, TrackPoint, Vehicle, VehicleRegistration)
from .ndtp import receive_ndtp
from .history import HistoricalDataset
from .service import DispatcherService
from .store import LiveStore

MOSCOW = timezone(timedelta(hours=3))
EMULATOR_UNITS = (2000100, 2000500, 2000900)


def emulator_request(method: str, payload: dict | None = None) -> dict:
    base = os.getenv("EMULATOR_URL", "http://emulator:18080").rstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(f"{base}/api/config", data=data,
                      headers={"Content-Type": "application/json"}, method=method)
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def local_time(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(MOSCOW).replace(tzinfo=None)
    return value


def create_app(ml_url: str | None = None, ndtp_enabled: bool | None = None,
               dataset_dir: str | Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = LiveStore()
        service = DispatcherService(store,
            ml_url if ml_url is not None else os.getenv("ML_SERVICE_URL"),
            ml_history_minutes=int(os.getenv("ML_HISTORY_MINUTES", "15")))
        app.state.service = service
        root = Path(dataset_dir if dataset_dir is not None else os.getenv(
            "DATASET_DIR", str(Path(__file__).resolve().parents[2] / "dataset")))
        split = os.getenv("DATASET_SPLIT", "validate")
        app.state.history = (HistoricalDataset(root, split, service.ml_url,
            service.ml_history_minutes) if (root / split / "traffic.csv").exists() else None)
        if app.state.history:
            for unit_id, tr_id in app.state.history.store.unit_to_tr.items():
                service.register(VehicleRegistration(unit_id=unit_id, tr_id=tr_id))
        server = None
        enabled = ndtp_enabled if ndtp_enabled is not None else os.getenv("NDTP_ENABLED", "1") == "1"
        if enabled:
            server = await asyncio.start_server(
                lambda reader, writer: receive_ndtp(reader, writer, store.unit_to_tr, service.ingest),
                host="0.0.0.0", port=int(os.getenv("NDTP_PORT", "9201")))
        try:
            yield
        finally:
            if app.state.history:
                await app.state.history.stop()
            if server:
                server.close()
                await server.wait_closed()

    app = FastAPI(title="Transport Dispatcher API", version="1.0.0",
        description="NDTP and local historical CSV telemetry, scheduled arrivals, ML forecasts and alerts.",
        lifespan=lifespan)
    app.add_middleware(CORSMiddleware,
        allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",") if x.strip()],
        allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["*"])

    def service() -> DispatcherService:
        return app.state.service

    def history() -> HistoricalDataset:
        if app.state.history is None:
            raise HTTPException(503, "local dataset is unavailable; set DATASET_DIR")
        return app.state.history

    async def selected_snapshot(mode: Literal["live", "historical"], at: datetime | None = None) -> Snapshot:
        if mode == "live":
            return await service().snapshot(at=at)
        source = history()
        try:
            return await source.snapshot(at or source.cursor or source.start)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/health/live", tags=["health"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready():
        s = service()
        return {"status": "ok", "registered_vehicles": len(s.store.unit_to_tr),
                "scheduled_arrivals": len(s.store.arrivals), "live_vehicles": len(s.live),
                "ml_configured": bool(s.ml_url), "ml_available": s.ml_available,
                "historical_available": app.state.history is not None}

    @app.get("/api/v1/metrics", tags=["health"])
    def metrics():
        s = service()
        historical = app.state.history.service if app.state.history else None
        return {"ml_requests": s.predict_requests, "ml_success": s.predict_success,
                "ml_failures": s.predict_failures, "ml_latency_ms": s.percentiles(s.ml_latency_ms),
                "ingest_to_publish_latency_ms": s.percentiles(s.ingest_latency_ms),
                "duplicate_packets": s.duplicate_packets,
                "out_of_order_packets": s.out_of_order_packets,
                "websocket_dropped_snapshots": s.websocket_dropped_snapshots,
                "live_vehicles": len(s.live), "websocket_clients": len(s.subscribers),
                "historical_ml_requests": historical.predict_requests if historical else 0,
                "historical_ml_success": historical.predict_success if historical else 0,
                "historical_ml_failures": historical.predict_failures if historical else 0}

    @app.get("/api/v1/tracks", response_model=list[TrackPoint], tags=["dashboard"])
    def tracks(source: Literal["ndtp", "api"] | None = None):
        """Recent live packets for restoring paths after a browser reload."""
        items = (item for group in service().live_history.values() for item in group)
        return sorted((service().track_point(item) for item in items
                       if source is None or item.source == source), key=lambda item: item.event_time)

    @app.get("/api/v1/stops", response_model=list[ScheduledArrival], tags=["dashboard"])
    def all_stops(mode: Literal["live", "historical"] = "historical"):
        """Full schedule for rendering every stop before the first telemetry frame."""
        store = history().store if mode == "historical" else service().store
        return sorted(store.arrivals.values(), key=lambda item: (item.tr_id, item.planned_at))

    @app.get("/api/v1/emulator/status", tags=["emulator"])
    async def emulator_status():
        try:
            config = await asyncio.to_thread(emulator_request, "GET")
        except (OSError, HTTPError, URLError, ValueError) as exc:
            raise HTTPException(503, f"emulator unavailable: {exc}") from exc
        return {"running": bool(config.get("units")), "units": config.get("units", [])}

    @app.post("/api/v1/emulator/start", tags=["emulator"])
    async def emulator_start():
        try:
            await asyncio.to_thread(emulator_request, "GET")
        except (OSError, HTTPError, URLError, ValueError) as exc:
            raise HTTPException(503, f"emulator unavailable: {exc}") from exc
        now = datetime.now(MOSCOW).replace(tzinfo=None, microsecond=0)
        for index, unit_id in enumerate(EMULATOR_UNITS):
            service().register(VehicleRegistration(tr_id=unit_id, unit_id=unit_id))
            offset = (unit_id % 1000) / 10000
            for stop_index in range(12):
                arrival = ScheduledArrival(arrival_id=unit_id * 100 + stop_index,
                    tr_id=unit_id, planned_at=now + timedelta(minutes=12 + 5 * stop_index),
                    lon=37.50 + offset + 0.005 * (stop_index + 1),
                    lat=55.70 + offset + 0.003 * (stop_index + 1),
                    address=f"Демонстрационная остановка {stop_index + 1}")
                service().store.upsert_arrival(arrival)
                service().prediction_cache.pop((unit_id, arrival.arrival_id), None)
        config = {"targetHost": "backend", "targetPort": 9201,
                  "units": [{"unitId": unit_id, "intervalMs": 1000,
                             "autoGenerate": True, "cells": []} for unit_id in EMULATOR_UNITS]}
        try:
            await asyncio.to_thread(emulator_request, "POST", config)
        except (OSError, HTTPError, URLError, ValueError) as exc:
            raise HTTPException(503, f"emulator could not start: {exc}") from exc
        return {"running": True, "unit_ids": EMULATOR_UNITS}

    @app.post("/api/v1/emulator/stop", tags=["emulator"])
    async def emulator_stop():
        try:
            await asyncio.to_thread(emulator_request, "POST",
                                    {"targetHost": "backend", "targetPort": 9201, "units": []})
        except (OSError, HTTPError, URLError, ValueError) as exc:
            raise HTTPException(503, f"emulator could not stop: {exc}") from exc
        return {"running": False}

    @app.post("/api/v1/registrations", response_model=VehicleRegistration, tags=["setup"])
    def register(item: VehicleRegistration):
        """Map an NDTP unit_id to the transport ID used in the schedule."""
        try:
            return service().register(item)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/registrations", response_model=list[VehicleRegistration], tags=["setup"])
    def registrations():
        return [VehicleRegistration(tr_id=tr_id, unit_id=unit_id)
                for unit_id, tr_id in sorted(service().store.unit_to_tr.items())]

    @app.post("/api/v1/arrivals", response_model=ScheduledArrival, tags=["setup"])
    def scheduled_arrival(item: ScheduledArrival):
        """Add or update one planned arrival; required for selecting a 10–15 minute target."""
        item = item.model_copy(update={"planned_at": local_time(item.planned_at)})
        try:
            result = service().store.upsert_arrival(item)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        service().prediction_cache.pop((item.tr_id, item.arrival_id), None)
        return result

    @app.post("/api/v1/arrivals/{arrival_id}/observed", response_model=ArrivalDeviation, tags=["setup"])
    def observed_arrival(arrival_id: int, item: ObservedArrival):
        """Record a completed stop so the ML input includes current_dev_s."""
        try:
            result = service().store.observe_arrival(arrival_id, local_time(item.actual_at))
        except KeyError as exc:
            raise HTTPException(404, "scheduled arrival not found") from exc
        for key in tuple(service().prediction_cache):
            if key[0] == result.tr_id:
                service().prediction_cache.pop(key, None)
        return result

    @app.get("/api/v1/snapshot", response_model=Snapshot, tags=["dashboard"])
    async def snapshot(mode: Literal["live", "historical"] = "live", at: datetime | None = None):
        return await selected_snapshot(mode, local_time(at))

    @app.get("/api/v1/vehicles", response_model=list[Vehicle], tags=["dashboard"])
    async def vehicles(risk: str | None = Query(None, pattern="^(green|yellow|red|unknown)$"),
                       mode: Literal["live", "historical"] = "live", at: datetime | None = None):
        state = await selected_snapshot(mode, local_time(at))
        return [item for item in state.vehicles if risk is None or item.risk == risk]

    @app.get("/api/v1/vehicles/{tr_id}", response_model=Vehicle, tags=["dashboard"])
    async def vehicle(tr_id: int, mode: Literal["live", "historical"] = "live", at: datetime | None = None):
        state = await selected_snapshot(mode, local_time(at))
        result = next((item for item in state.vehicles if item.tr_id == tr_id), None)
        if result is None:
            raise HTTPException(404, "vehicle not found")
        return result

    @app.get("/api/v1/alerts", response_model=list[Alert], tags=["dashboard"])
    async def alerts(mode: Literal["live", "historical"] = "live", at: datetime | None = None):
        return (await selected_snapshot(mode, local_time(at))).alerts

    @app.get("/api/v1/predictions", response_model=list[Prediction], tags=["dashboard"])
    async def predictions(mode: Literal["live", "historical"] = "live", at: datetime | None = None):
        state = await selected_snapshot(mode, local_time(at))
        return [item.prediction for item in state.vehicles if item.prediction is not None]

    @app.get("/api/v1/stops", response_model=list[ScheduledArrival], tags=["dashboard"])
    def stops(at: datetime | None = None, before_min: int = Query(0, ge=0, le=120),
              after_min: int = Query(60, ge=0, le=120), tr_id: int | None = None,
              mode: Literal["live", "historical"] = "live"):
        at = local_time(at) or datetime.now(MOSCOW).replace(tzinfo=None)
        store = history().store if mode == "historical" else service().store
        return store.stops_between(at - timedelta(minutes=before_min),
                                   at + timedelta(minutes=after_min), tr_id)

    @app.get("/api/v1/timeline", tags=["replay"])
    def timeline():
        return history().timeline()

    @app.get("/api/v1/replay/status", tags=["replay"])
    def replay_status():
        return history().status()

    @app.get("/api/v1/replay/tracks", response_model=list[TrackPoint], tags=["replay"])
    def replay_tracks(until: datetime, offset: int = Query(0, ge=0),
                      limit: int = Query(5000, ge=1, le=10000)):
        """Page through every historical packet to restore map paths after reconnect."""
        return history().tracks(local_time(until), offset, limit)

    @app.post("/api/v1/replay/step", response_model=Snapshot, tags=["replay"])
    async def replay_step(sample_id: str | None = None, at: datetime | None = None,
                          advance_s: int = Query(15, ge=15, le=300)):
        try:
            return await history().step(local_time(at), sample_id, advance_s)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/replay/start", tags=["replay"])
    async def replay_start(interval_s: float = Query(1.0, ge=0.05, le=60),
                           advance_s: int = Query(15, ge=15, le=300)):
        source = history()
        if source.task is None or source.task.done():
            source.task = asyncio.create_task(source.run(interval_s, advance_s))
        return source.status()

    @app.post("/api/v1/replay/stop", tags=["replay"])
    async def replay_stop():
        source = history()
        await source.stop()
        return source.status()

    @app.post("/api/v1/replay/reset", tags=["replay"])
    async def replay_reset():
        source = history()
        await source.reset()
        return source.status()

    @app.post("/api/v1/telemetry", response_model=Snapshot, tags=["telemetry"])
    async def inject(item: Telemetry):
        """Inject decoded telemetry; NDTP TCP on port 9201 is the regular input."""
        if item.tr_id is None and item.unit_id is None:
            raise HTTPException(422, "tr_id or unit_id is required")
        item = item.model_copy(update={
            "event_time": local_time(item.event_time),
            "received_at": local_time(item.received_at),
            "gps_time": local_time(item.gps_time),
        })
        return await service().ingest(item)

    @app.websocket("/api/v1/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1024)
        historical = app.state.history
        mode = ws.query_params.get("mode", "live")
        if mode == "historical" and historical:
            historical.service.subscribers.add(queue)
        else:
            service().subscribers.add(queue)
        try:
            if mode == "historical" and historical:
                initial = historical.service.last_snapshot or await historical.snapshot(
                    historical.cursor or historical.start)
            else:
                initial = service().last_snapshot or await service().snapshot()
            await ws.send_json({"type": "snapshot", "data": initial.model_dump(mode="json")})
            while True:
                next_message = asyncio.create_task(queue.get())
                disconnect = asyncio.create_task(ws.receive())
                done, pending = await asyncio.wait({next_message, disconnect},
                                                   return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if disconnect in done:
                    break
                await ws.send_json(next_message.result())
        except WebSocketDisconnect:
            pass
        finally:
            service().subscribers.discard(queue)
            if app.state.history:
                app.state.history.service.subscribers.discard(queue)

    return app


app = create_app()
