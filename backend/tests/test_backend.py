"""Integration checks for the real validate data and the NDTP wire contract."""

import asyncio
import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from fastapi.testclient import TestClient

from backend.app.data import DataStore
from backend.app.main import create_app
from backend.app.ndtp import NAV, NPH, NPL, crc16_modbus, decode_frame, receive_ndtp
from backend.app.service import risk_for_delay


DATASET = Path(__file__).resolve().parents[2] / "dataset"


def test_risk_boundary_matches_label_convention():
    assert risk_for_delay(None) == "unknown"
    assert risk_for_delay(120) == "yellow"
    assert risk_for_delay(120.1) == "red"


def test_forecast_horizon_and_no_future_telemetry():
    data = DataStore(DATASET)
    point = data.points_by_id["131672_1767670500"]
    at = point["T"]
    target = data.target_at(point["tr_id"], at)
    assert target is not None
    assert timedelta(minutes=10) < target.planned_at - at <= timedelta(minutes=15)
    telemetry, history = data.telemetry_at(point["tr_id"], at)
    assert telemetry and telemetry.event_time <= at
    assert all(x.event_time <= at for x in history)
    assert data.point_at(point["tr_id"], at - timedelta(seconds=1)) is None


def test_backend_does_not_load_target_labels_as_features():
    data = DataStore(DATASET, split="test")
    assert len(data.points_by_id) == 353
    assert all("target_delay_s" not in point for point in data.points_by_id.values())
    assert not hasattr(data, "actual_arrivals")


def test_replay_alert_swagger_and_websocket():
    app = create_app(DATASET, ndtp_enabled=False)
    with TestClient(app) as client:
        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        assert "Features" not in openapi.json()["components"]["schemas"]
        assert "features" not in openapi.json()["components"]["schemas"]["Vehicle"]["properties"]
        with client.websocket_connect("/api/v1/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            response = client.post("/api/v1/replay/step", params={"sample_id": "131672_1767670500"})
            assert response.status_code == 200
            state = response.json()
            assert "features" not in state["vehicles"][0]
            assert any(a["source"] == "baseline" and a["risk"] == "red" for a in state["alerts"])
            first_alert = next(a for a in state["alerts"] if a["tr_id"] == 131672)
            assert first_alert["alert_id"] == "131672:53700172828"
            assert ws.receive_json()["data"]["at"] == "2026-01-06T03:35:00"
            later = client.get("/api/v1/snapshot", params={"at": "2026-01-06T03:36:00"}).json()
            assert any(a["alert_id"] == first_alert["alert_id"] for a in later["alerts"])
        assert client.get("/api/v1/metrics").json()["ml_requests"] == 0
        utc = client.get("/api/v1/snapshot", params={"at": "2026-01-06T00:35:00Z"})
        assert utc.status_code == 200 and utc.json()["at"] == "2026-01-06T03:35:00"
        assert client.post("/api/v1/replay/start", params={"interval_ms": 1000}).status_code == 200
        assert client.get("/api/v1/replay/status").json()["running"]
        assert client.post("/api/v1/replay/stop").status_code == 200
        assert client.post("/api/v1/replay/reset").json()["next_index"] == 0


def test_live_retains_last_valid_location_and_deduplicates():
    with TestClient(create_app(DATASET, ndtp_enabled=False)) as client:
        first = {"tr_id": 131672, "unit_id": 123, "event_time": "2026-01-06T03:35:00",
                 "lon": 37.6, "lat": 55.7, "location_valid": True, "speed_kmh": 10}
        assert client.post("/api/v1/telemetry", json=first).status_code == 200
        second = {"tr_id": 131672, "unit_id": 123, "event_time": "2026-01-06T03:35:05",
                  "location_valid": False, "speed_kmh": None}
        state = client.post("/api/v1/telemetry", json=second).json()
        vehicle = next(v for v in state["vehicles"] if v["tr_id"] == 131672)
        assert not vehicle["location_valid"]
        assert vehicle["lon"] == 37.6 and vehicle["lat"] == 55.7
        assert vehicle["location_observed_at"] == "2026-01-06T03:35:00"
        client.post("/api/v1/telemetry", json=second)
        assert client.get("/api/v1/metrics").json()["duplicate_packets"] == 1
        assert client.get("/api/v1/metrics").json()["ingest_to_publish_latency_ms"]["p50"] is not None


def test_explicit_demo_schedule_shift_without_backend_feature_estimation(monkeypatch):
    monkeypatch.setenv("DEMO_SCHEDULE_SHIFT_DAYS", "262")
    data = DataStore(DATASET)
    target = data.stop_by_arrival[53700172828]
    with TestClient(create_app(DATASET, ndtp_enabled=False)) as client:
        state = client.post("/api/v1/telemetry", json={"tr_id": target.tr_id,
            "event_time": "2026-09-25T03:35:00", "lon": 37.6, "lat": 55.7,
            "location_valid": True, "speed_kmh": 10}).json()
        vehicle = next(v for v in state["vehicles"] if v["tr_id"] == target.tr_id)
        assert state["demo_schedule_shift_days"] == 262
        assert vehicle["target_arrival"]["planned_at"] == "2026-09-25T03:50:00"
        assert vehicle["forecast_status"] == "insufficient_data"


def test_ndtp_frame_crc_coordinates_and_handshake():
    def frame(service_id, message_type, body):
        nph = NPH.pack(service_id, message_type, 1, 1)
        payload = nph + body
        crc = int.from_bytes(crc16_modbus(payload).to_bytes(2, "little"), "big")
        return NPL.pack(0x7E7E, len(payload), 0, crc, 2, 123, 0) + payload

    handshake = frame(0, 100, bytes(18))
    assert decode_frame(handshake, {123: 456}) is None
    nav = NAV.pack(1767670500, 376173210, 557551234, 0xE0, 0, 35, 40, 90, 0, 200, 8, 2)
    decoded = decode_frame(frame(1, 101, bytes((0, 0)) + nav), {123: 456})
    assert decoded.tr_id == 456 and decoded.unit_id == 123
    assert decoded.lon == 37.617321 and decoded.lat == 55.7551234
    assert decoded.speed_kmh == 35 and decoded.location_valid
    corrupt = bytearray(frame(1, 101, bytes((0, 0)) + nav))
    corrupt[-1] ^= 1
    try:
        decode_frame(bytes(corrupt), {123: 456})
    except ValueError as error:
        assert "CRC" in str(error)
    else:
        raise AssertionError("bad CRC accepted")


def test_ndtp_tcp_stream_accepts_multiple_packets():
    received = []

    async def exercise():
        server = await asyncio.start_server(
            lambda reader, writer: receive_ndtp(reader, writer, {123: 456}, received.append_async),
            "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            nph = NPH.pack(1, 101, 1, 1)
            nav = NAV.pack(1767670500, 376173210, 557551234, 0xE0, 0, 35, 40, 90, 0, 200, 8, 2)
            payload = nph + bytes((0, 0)) + nav
            crc = int.from_bytes(crc16_modbus(payload).to_bytes(2, "little"), "big")
            frame = NPL.pack(0x7E7E, len(payload), 0, crc, 2, 123, 0) + payload
            writer.write(frame[:5])
            await writer.drain()
            writer.write(frame[5:] + frame)
            await writer.drain()
            await asyncio.wait_for(received.ready.wait(), 1)
            writer.close()
            await writer.wait_closed()
            assert len(received) == 2
        finally:
            server.close()
            await server.wait_closed()

    class Collector(list):
        def __init__(self):
            super().__init__()
            self.ready = asyncio.Event()

        async def append_async(self, item):
            self.append(item)
            if len(self) == 2:
                self.ready.set()

    received = Collector()
    asyncio.run(exercise())


def test_ml_http_contract_is_used_without_training_model():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"prediction_s": 180, "delay_probability": 0.8,
                               "model_version": "test-model"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        with TestClient(create_app(DATASET, ml_url=url, ndtp_enabled=False)) as client:
            response = client.post("/api/v1/replay/step", params={"sample_id": "131672_1767670500"})
            assert response.status_code == 200
            alert = next(x for x in response.json()["alerts"] if x["tr_id"] == 131672)
            assert alert["source"] == "ml" and alert["predicted_delay_s"] == 180
            assert alert["delay_probability"] == 0.8
            assert any(x["sample_id"] == "131672_1767670500" and x["T"] == "2026-01-06T03:35:00" for x in requests)
            target_request = next(x for x in requests if x["sample_id"] == "131672_1767670500")
            assert "features" not in target_request and "features_version" not in target_request
            assert target_request["target_geom"].startswith("POINT (")
            assert target_request["telemetry_history"]
            assert all(datetime.fromisoformat(row["event_time"]) <= datetime.fromisoformat(target_request["T"])
                       for row in target_request["telemetry_history"])
            assert {"speed", "heading", "event_time", "location_valid"} <= set(target_request["telemetry_history"][0])
            assert client.get("/api/v1/metrics").json()["ml_success"] >= 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
