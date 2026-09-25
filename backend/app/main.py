"""FastAPI application and NDTP receiver lifecycle."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .data import DataStore
from .models import Alert, Prediction, Snapshot, Stop, Telemetry, Vehicle
from .ndtp import receive_ndtp
from .service import DispatcherService

DATASET = Path(os.getenv("DATASET_DIR", Path(__file__).resolve().parents[2] / "dataset"))
MOSCOW = timezone(timedelta(hours=3))


def local_time(value: datetime | None) -> datetime | None:
    """Normalize optional offset-aware API timestamps to dataset local time."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(MOSCOW).replace(tzinfo=None)
    return value


def create_app(dataset_dir: Path | None = None, ml_url: str | None = None,
               ndtp_enabled: bool | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        data = DataStore(dataset_dir or DATASET, os.getenv("DATASET_SPLIT", "validate"))
        service = DispatcherService(data, ml_url if ml_url is not None else os.getenv("ML_SERVICE_URL"),
            demo_schedule_shift_days=int(os.getenv("DEMO_SCHEDULE_SHIFT_DAYS", "0")),
            ml_history_minutes=int(os.getenv("ML_HISTORY_MINUTES", "15")))
        app.state.service = service
        server = None
        enabled = ndtp_enabled if ndtp_enabled is not None else os.getenv("NDTP_ENABLED", "1") == "1"
        if enabled:
            port = int(os.getenv("NDTP_PORT", "9201"))
            server = await asyncio.start_server(
                lambda reader, writer: receive_ndtp(reader, writer, data.unit_to_tr, service.ingest),
                host="0.0.0.0", port=port)
        try:
            yield
        finally:
            await service.stop_replay()
            if server:
                server.close()
                await server.wait_closed()

    app = FastAPI(title="Transport Dispatcher API", version="0.1.0",
        description="NDTP telemetry, point-in-time forecasts, map state and alerts. Dataset timestamps are local Moscow time.",
        lifespan=lifespan)
    app.add_middleware(CORSMiddleware,
        allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",") if x.strip()],
        allow_credentials=True, allow_methods=["GET", "POST"], allow_headers=["*"])

    def service() -> DispatcherService:
        return app.state.service

    @app.get("/health/live", tags=["health"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready():
        data = service().data
        return {"status": "ok", "historical_vehicles": len(data.traffic),
            "forecast_points": len(data.points_by_id), "live_vehicles": len(service().live),
            "dataset_split": data.split,
            "ambiguous_unit_ids": len(data.ambiguous_units),
            "ml_available": service().ml_available}

    @app.get("/api/v1/metrics", tags=["health"])
    def metrics():
        s = service()
        return {"ml_requests": s.predict_requests, "ml_success": s.predict_success,
            "ml_failures": s.predict_failures,
            "ml_average_latency_ms": s.predict_latency_ms_total / s.predict_requests if s.predict_requests else None,
            "ml_latency_ms": s.percentiles(s.ml_latency_ms),
            "ingest_to_publish_latency_ms": s.percentiles(s.ingest_latency_ms),
            "duplicate_packets": s.duplicate_packets,
            "out_of_order_packets": s.out_of_order_packets,
            "websocket_dropped_snapshots": s.websocket_dropped_snapshots,
            "live_vehicles": len(s.live), "websocket_clients": len(s.subscribers)}

    @app.get("/api/v1/timeline", tags=["replay"])
    def timeline():
        times = service().data.timeline
        return {"first": times[0], "last": times[-1], "forecast_times": times}

    @app.get("/api/v1/snapshot", response_model=Snapshot, tags=["dashboard"])
    async def snapshot(at: datetime | None = None, mode: str = Query("historical", pattern="^(historical|live)$")):
        return await service().snapshot(at=local_time(at), mode=mode)

    @app.get("/api/v1/vehicles", response_model=list[Vehicle], tags=["dashboard"])
    async def vehicles(at: datetime | None = None, mode: str = Query("historical", pattern="^(historical|live)$"),
                       min_lon: float | None = None, min_lat: float | None = None,
                       max_lon: float | None = None, max_lat: float | None = None,
                       risk: str | None = Query(None, pattern="^(green|yellow|red|unknown)$")):
        result = await service().snapshot(at=local_time(at), mode=mode)
        return [v for v in result.vehicles if
            (risk is None or v.risk == risk) and
            (min_lon is None or v.lon is not None and v.lon >= min_lon) and
            (max_lon is None or v.lon is not None and v.lon <= max_lon) and
            (min_lat is None or v.lat is not None and v.lat >= min_lat) and
            (max_lat is None or v.lat is not None and v.lat <= max_lat)]

    @app.get("/api/v1/vehicles/{tr_id}", response_model=Vehicle, tags=["dashboard"])
    async def vehicle(tr_id: int, at: datetime | None = None,
                      mode: str = Query("historical", pattern="^(historical|live)$")):
        result = await service().snapshot(at=local_time(at), mode=mode)
        found = next((x for x in result.vehicles if x.tr_id == tr_id), None)
        if found is None:
            raise HTTPException(404, "vehicle not found at requested time")
        return found

    @app.get("/api/v1/alerts", response_model=list[Alert], tags=["dashboard"])
    async def alerts(at: datetime | None = None, mode: str = Query("historical", pattern="^(historical|live)$")):
        return (await service().snapshot(at=local_time(at), mode=mode)).alerts

    @app.get("/api/v1/predictions", response_model=list[Prediction], tags=["dashboard"])
    async def predictions(at: datetime | None = None,
                          mode: str = Query("historical", pattern="^(historical|live)$")):
        state = await service().snapshot(at=local_time(at), mode=mode)
        return [v.prediction for v in state.vehicles if v.prediction is not None]

    @app.get("/api/v1/stops", response_model=list[Stop], tags=["dashboard"])
    def stops(at: datetime | None = None, before_min: int = Query(0, ge=0, le=120),
              after_min: int = Query(60, ge=0, le=120), tr_id: int | None = None):
        """Planned stop markers near the selected time; no route geometry is implied."""
        at = local_time(at) or service().data.timeline[-1]
        start, end = at - timedelta(minutes=before_min), at + timedelta(minutes=after_min)
        groups = [service().data.stops.get(tr_id, [])] if tr_id is not None else service().data.stops.values()
        return [stop for group in groups for stop in group if start <= stop.planned_at <= end]

    @app.post("/api/v1/replay/step", response_model=Snapshot, tags=["replay"])
    async def replay_step(at: datetime | None = None, sample_id: str | None = None):
        if sample_id:
            point = service().data.points_by_id.get(sample_id)
            if not point:
                raise HTTPException(404, "forecast point not found")
            at = point["T"]
        if at is None:
            raise HTTPException(422, "at or sample_id is required")
        result = await service().snapshot(at=local_time(at))
        await service().publish(result)
        return result

    @app.post("/api/v1/replay/start", tags=["replay"])
    async def replay_start(interval_ms: int = Query(1000, ge=100, le=60000)):
        s = service()
        if s.replay_task and not s.replay_task.done():
            raise HTTPException(409, "replay already running")
        if s.replay_index >= len(s.data.timeline):
            raise HTTPException(409, "replay finished; call /api/v1/replay/reset")
        s.replay_task = asyncio.create_task(s.run_replay(interval_ms / 1000))
        return {"status": "started", "interval_ms": interval_ms, "forecast_times": len(s.data.timeline)}

    @app.post("/api/v1/replay/stop", tags=["replay"])
    async def replay_stop():
        await service().stop_replay()
        return {"status": "stopped"}

    @app.post("/api/v1/replay/reset", tags=["replay"])
    async def replay_reset():
        await service().reset_replay()
        return {"status": "reset", "next_index": 0}

    @app.get("/api/v1/replay/status", tags=["replay"])
    def replay_status():
        s = service()
        return {"running": bool(s.replay_task and not s.replay_task.done()),
            "at": s.replay_at, "next_index": s.replay_index, "total": len(s.data.timeline)}

    @app.post("/api/v1/telemetry", response_model=Snapshot, tags=["telemetry"],
              description="Inject decoded telemetry for local integration and testing. NDTP TCP is the production input.")
    async def inject(item: Telemetry):
        if item.tr_id is None and item.unit_id is None:
            raise HTTPException(422, "tr_id or unit_id is required")
        if item.tr_id is None and item.unit_id is not None:
            item.tr_id = service().data.unit_to_tr.get(item.unit_id)
        item.event_time = local_time(item.event_time)
        item.received_at = local_time(item.received_at)
        return await service().ingest(item)

    @app.websocket("/api/v1/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        service().subscribers.add(queue)
        try:
            initial = service().last_snapshot or await service().snapshot()
            await ws.send_json({"type": "snapshot", "data": initial.model_dump(mode="json")})
            while True:
                next_message = asyncio.create_task(queue.get())
                disconnect = asyncio.create_task(ws.receive())
                done, pending = await asyncio.wait({next_message, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if disconnect in done:
                    break
                await ws.send_json(next_message.result())
        except WebSocketDisconnect:
            pass
        finally:
            service().subscribers.discard(queue)

    return app


app = create_app()
