"""Backend contracts; historical fixtures never require the private dataset in CI."""

import json
import math
from pathlib import Path
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from fastapi.testclient import TestClient
import pytest

from backend.app.features import build_ml_request
from backend.app.main import create_app
from backend.app.models import ScheduledArrival, Telemetry
from backend.app.ndtp import NAV, NPH, NPL, crc16_modbus, decode_frame
from backend.app.service import risk_for_delay
from backend.app.store import LiveStore


def _time() -> datetime:
    return datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None, microsecond=0)


def test_online_features_follow_ml_training_formulas():
    at = _time()
    stop = ScheduledArrival(arrival_id=1, tr_id=7, planned_at=at + timedelta(minutes=12),
                            lon=37.7, lat=55.7)
    history = [
        Telemetry(tr_id=7, event_time=at - timedelta(minutes=4), speed_kmh=0,
                  location_valid=False),
        Telemetry(tr_id=7, event_time=at - timedelta(minutes=2), speed_kmh=10,
                  location_valid=True, lon=37.6, lat=55.6),
        Telemetry(tr_id=7, event_time=at, speed_kmh=20,
                  location_valid=True, lon=37.61, lat=55.61),
    ]
    fields = build_ml_request(at, stop, history, None)
    assert fields.mean_speed_1m_kmh == 20
    assert fields.mean_speed_3m_kmh == 15
    assert fields.mean_speed_5m_kmh == 10
    assert math.isclose(fields.speed_std_5m_kmh, math.sqrt(200 / 3))
    assert fields.speed_change_5m_kmh == 20
    assert fields.stopped_share_5m == 1 / 3
    assert fields.valid_messages_5m == 2
    assert fields.distance_to_target_km > 0
    assert fields.current_dev_s is None


def test_ndtp_optional_irma_door_mask():
    nav = bytes((0, 0)) + NAV.pack(1767670500, 376000000, 557000000,
                                  0xE0, 0, 20, 0, 90, 0, 0, 0, 0)
    for mask, expected in ((0x13, False), (0x11, True), (0x00, None)):
        cells = nav + bytes((8, 0)) + bytes(6) + bytes((4, 0)) + bytes(14) + bytes((mask,))
        payload = NPH.pack(1, 101, 1, 1) + cells
        crc = int.from_bytes(crc16_modbus(payload).to_bytes(2, "little"), "big")
        frame = NPL.pack(0x7E7E, len(payload), 0, crc, 2, 77, 0) + payload
        item = decode_frame(frame, {77: 7})
        assert item.tr_id == 7 and item.doors_closed is expected


def test_current_deviation_uses_latest_planned_completed_arrival():
    store = LiveStore()
    at = _time()
    earlier = ScheduledArrival(arrival_id=10, tr_id=7,
        planned_at=at - timedelta(minutes=10), lon=37.6, lat=55.6)
    later = ScheduledArrival(arrival_id=11, tr_id=7,
        planned_at=at - timedelta(minutes=5), lon=37.7, lat=55.7)
    store.upsert_arrival(earlier)
    store.upsert_arrival(later)
    store.observe_arrival(10, at - timedelta(minutes=4))
    store.observe_arrival(11, at - timedelta(minutes=4, seconds=30))
    assert store.deviation_at(7, at) == 30
    assert store.deviation_at(7, at - timedelta(minutes=5)) is None


def test_ml_core_http_contract_and_websocket():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/predict"
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"prediction_delay_s": 125.0, "horizon_minutes": 12.0,
                               "model_version": "extratrees-all-42",
                               "patterns": ["скорость заметно снизилась"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with TestClient(create_app(ml_url=f"http://127.0.0.1:{server.server_port}",
                                   ndtp_enabled=False)) as client:
            at = _time()
            assert client.post("/api/v1/registrations", json={"tr_id": 7, "unit_id": 77}).status_code == 200
            assert client.post("/api/v1/registrations", json={"tr_id": 8, "unit_id": 77}).status_code == 409
            for minutes, speed in [(2, 0), (1, 5)]:
                response = client.post("/api/v1/telemetry", json={
                    "unit_id": 77, "event_time": (at - timedelta(minutes=minutes)).isoformat(),
                    "lon": 37.60, "lat": 55.60, "speed_kmh": speed, "location_valid": True})
                assert response.status_code == 200
            assert client.post("/api/v1/arrivals", json={"arrival_id": 1, "tr_id": 7,
                "planned_at": (at - timedelta(minutes=3)).isoformat(),
                "lon": 37.59, "lat": 55.59}).status_code == 200
            observed = client.post("/api/v1/arrivals/1/observed", json={
                "actual_at": (at - timedelta(minutes=2, seconds=30)).isoformat()})
            assert observed.json()["current_dev_s"] == 30
            assert client.post("/api/v1/arrivals", json={"arrival_id": 2, "tr_id": 7,
                "planned_at": (at + timedelta(minutes=12)).isoformat(),
                "lon": 37.70, "lat": 55.70}).status_code == 200
            with client.websocket_connect("/api/v1/ws") as ws:
                assert ws.receive_json()["type"] == "snapshot"
                response = client.post("/api/v1/telemetry", json={
                    "unit_id": 77, "event_time": at.isoformat(),
                    "lon": 37.61, "lat": 55.61, "speed_kmh": 10, "location_valid": True})
                assert response.status_code == 200
                state = response.json()
                assert ws.receive_json()["data"]["vehicles"][0]["prediction"]["source"] == "ml"
            vehicle = state["vehicles"][0]
            assert vehicle["tr_id"] == 7 and vehicle["risk"] == "red"
            assert state["track_points"][0]["source"] == "api"
            assert state["track_points"][0]["vehicle_id"] == "7"
            assert any(stop["arrival_id"] == 2 for stop in state["stops"])
            assert vehicle["prediction"]["prediction_s"] == 125
            assert vehicle["prediction"]["model_version"] == "extratrees-all-42"
            assert vehicle["prediction"]["patterns"] == ["скорость заметно снизилась"]
            assert state["alerts"][0]["patterns"] == ["скорость заметно снизилась"]
            assert received[-1]["current_dev_s"] == 30
            assert received[-1]["target_stop_lon"] == 37.7
            assert received[-1]["target_time_begin"] == (at + timedelta(minutes=12)).isoformat()
            assert "telemetry_history" not in received[-1]
            assert {stop["arrival_id"] for stop in client.get("/api/v1/stops?mode=live").json()} == {1, 2}
            held = client.post("/api/v1/telemetry", json={
                "unit_id": 77, "event_time": (at + timedelta(minutes=3)).isoformat(),
                "lon": 37.62, "lat": 55.62, "speed_kmh": 19, "location_valid": True}).json()
            held_vehicle = next(item for item in held["vehicles"] if item["tr_id"] == 7)
            assert held_vehicle["target_arrival"]["arrival_id"] == 2
            assert held_vehicle["forecast_status"] == "ml"
            assert received[-1]["last_speed_kmh"] == 19
            assert received[-1]["forecast_time"] == (at + timedelta(minutes=3)).isoformat()
            retained = client.post("/api/v1/telemetry", json={
                "unit_id": 77, "event_time": (at + timedelta(minutes=13)).isoformat(),
                "lon": 37.63, "lat": 55.63, "speed_kmh": 20, "location_valid": True}).json()
            retained_vehicle = next(item for item in retained["vehicles"] if item["tr_id"] == 7)
            assert retained_vehicle["forecast_status"] == "last_known"
            assert retained_vehicle["prediction"]["predicted_arrival"] == held_vehicle["prediction"]["predicted_arrival"]
            assert retained_vehicle["speed_kmh"] == 20
            assert client.get("/api/v1/metrics").json()["ml_success"] >= 1
            assert client.get("/health/ready").json()["scheduled_arrivals"] == 2
            assert client.get("/demo").status_code == 404
            timeline_resp = client.get("/api/v1/timeline").json()
            assert "forecast_times" in timeline_resp, "Timeline endpoint must return forecast_times"
            assert len(timeline_resp["forecast_times"]) > 0, "Forecast times list should not be empty"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_baseline_and_unknown_unit_without_dataset(tmp_path: Path):
    at = _time()
    with TestClient(create_app(ml_url="", ndtp_enabled=False, dataset_dir=tmp_path)) as client:
        assert client.get("/api/v1/timeline").status_code == 503
        unknown = client.post("/api/v1/telemetry", json={
            "unit_id": 99, "event_time": at.isoformat(), "speed_kmh": 8}).json()
        assert unknown["vehicles"][0]["forecast_status"] == "unknown_vehicle"
        assert client.post("/api/v1/registrations", json={"tr_id": 9, "unit_id": 99}).status_code == 200
        client.post("/api/v1/arrivals", json={"arrival_id": 90, "tr_id": 9,
            "planned_at": (at - timedelta(minutes=5)).isoformat(), "lon": 37.5, "lat": 55.5})
        client.post("/api/v1/arrivals/90/observed", json={
            "actual_at": (at - timedelta(minutes=4)).isoformat()})
        client.post("/api/v1/arrivals", json={"arrival_id": 91, "tr_id": 9,
            "planned_at": (at + timedelta(minutes=12)).isoformat(), "lon": 37.6, "lat": 55.6})
        state = client.post("/api/v1/telemetry", json={
            "unit_id": 99, "event_time": (at + timedelta(seconds=1)).isoformat(),
            "speed_kmh": 8, "lon": 37.5, "lat": 55.5, "location_valid": True}).json()
        assert state["vehicles"][0]["prediction"]["source"] == "baseline"
        assert state["vehicles"][0]["prediction"]["prediction_s"] == 60
        assert risk_for_delay(120) == "yellow"
        assert risk_for_delay(120.1) == "red"


def test_next_stop_is_shown_before_ml_window(tmp_path: Path):
    at = _time()
    with TestClient(create_app(ml_url="", ndtp_enabled=False, dataset_dir=tmp_path)) as client:
        client.post("/api/v1/arrivals", json={"arrival_id": 501, "tr_id": 5,
            "planned_at": (at + timedelta(hours=1)).isoformat(),
            "lon": 37.7, "lat": 55.7, "address": "Дальняя остановка"})
        state = client.post("/api/v1/telemetry", json={"tr_id": 5,
            "event_time": at.isoformat(), "speed_kmh": 20,
            "lon": 37.6, "lat": 55.6, "location_valid": True}).json()
        vehicle = state["vehicles"][0]
        assert vehicle["target_arrival"]["address"] == "Дальняя остановка"
        assert vehicle["forecast_status"] == "outside_horizon"
        assert vehicle["prediction"] is None


def test_local_history_replay_uses_only_input_columns(tmp_path: Path):
    folder = tmp_path / "validate"
    folder.mkdir()
    (folder / "schedule_plan.csv").write_text(
        "tt_action_item_id,time_begin,tr_id,geom,building_address\n"
        "10,2026-01-06 00:12:15,7,POINT (37.7 55.7),Stop\n"
        "11,2026-01-06 04:00:00,7,POINT (37.8 55.8),Far stop\n", encoding="utf-8")
    (folder / "points.csv").write_text(
        "sample_id,tr_id,T,target_stop_id,target_time_begin,cur_dev_s\n"
        "7_15,7,2026-01-06 00:00:15,10,2026-01-06 00:12:15,75\n", encoding="utf-8")
    (folder / "traffic.csv").write_text(
        "packet_id,tr_id,unit_id,event_time,location_valid,lon,lat,speed,heading\n"
        "1,7,77,2026-01-06 00:00:00,True,37.6,55.6,0,0\n"
        "2,7,77,2026-01-06 00:00:15,True,37.6001,55.6001,0,0\n"
        "2a,7,77,2026-01-06 00:00:17,True,37.60015,55.60015,2,0\n"
        "2b,7,77,2026-01-06 00:00:22,False,,,2,0\n"
        "3,7,77,2026-01-06 00:00:30,True,37.6002,55.6002,5,0\n", encoding="utf-8")
    with TestClient(create_app(ml_url="", ndtp_enabled=False, dataset_dir=tmp_path)) as client:
        assert client.get("/api/v1/timeline").json()["steps"] == 3
        assert client.get("/api/v1/timeline").json()["packet_count"] == 5
        assert {stop["arrival_id"] for stop in client.get("/api/v1/stops").json()} == {10, 11}
        with client.websocket_connect("/api/v1/ws?mode=historical") as ws:
            ws.receive_json()
            first = client.post("/api/v1/replay/step").json()
            assert first["at"] == "2026-01-06T00:00:00"
            assert len(first["track_points"]) == 1
            assert ws.receive_json()["data"]["mode"] == "historical"
            result = client.post("/api/v1/replay/step?sample_id=7_15").json()
            assert ws.receive_json()["data"]["at"] == "2026-01-06T00:00:15"
            final = client.post("/api/v1/replay/step").json()
            ws.receive_json()
            assert final["processed_packets"] == 5
            assert [point["event_time"] for point in final["track_points"]] == [
                "2026-01-06T00:00:17", "2026-01-06T00:00:22", "2026-01-06T00:00:30"]
            assert final["track_points"][1]["location_valid"] is False
        first_page = client.get("/api/v1/replay/tracks", params={
            "until": final["at"], "offset": 0, "limit": 2}).json()
        second_page = client.get("/api/v1/replay/tracks", params={
            "until": final["at"], "offset": 2, "limit": 10}).json()
        assert len(first_page) == 2 and len(second_page) == 3
        assert second_page[1]["location_valid"] is False
        with client.websocket_connect("/api/v1/ws?mode=live") as ws:
            assert ws.receive_json()["data"]["mode"] == "live"
        with client.websocket_connect("/api/v1/ws?mode=historical") as ws:
            assert ws.receive_json()["data"]["at"] == final["at"]
        vehicle = result["vehicles"][0]
        assert result["dataset_split"] == "validate"
        assert vehicle["prediction"]["source"] == "baseline"
        assert vehicle["prediction"]["prediction_s"] == 75
        assert vehicle["schedule_deviation_s"] == 75
        assert vehicle["idle_time_s"] == 15
        assert vehicle["doors_closed"] is None
        assert client.get("/api/v1/snapshot?mode=historical&at=2026-01-06T00:00:15").status_code == 200
        assert client.post("/api/v1/replay/step?sample_id=bad").status_code == 404
        assert client.post("/api/v1/replay/reset").json()["cursor"] is None


def test_historical_snapshot_keeps_last_telemetry_for_inactive_vehicle(tmp_path: Path):
    folder = tmp_path / "validate"
    folder.mkdir()
    (folder / "schedule_plan.csv").write_text(
        "tt_action_item_id,time_begin,tr_id,geom,building_address\n"
        "10,2026-01-06 00:12:00,7,POINT (37.7 55.7),Stop\n", encoding="utf-8")
    (folder / "points.csv").write_text(
        "sample_id,tr_id,T,target_stop_id,target_time_begin,cur_dev_s\n", encoding="utf-8")
    (folder / "traffic.csv").write_text(
        "packet_id,tr_id,unit_id,event_time,location_valid,lon,lat,speed,heading\n"
        "1,7,77,2026-01-06 00:00:00,True,37.6,55.6,22,0\n"
        "2,8,88,2026-01-06 00:10:00,True,37.8,55.8,18,0\n", encoding="utf-8")
    with TestClient(create_app(ml_url="", ndtp_enabled=False, dataset_dir=tmp_path)) as client:
        state = client.get("/api/v1/snapshot?mode=historical&at=2026-01-06T00:09:00").json()
        vehicle = next(item for item in state["vehicles"] if item["tr_id"] == 7)
        assert vehicle["stale"] is True
        assert vehicle["speed_kmh"] == 22
        assert vehicle["lon"] == 37.6
        assert vehicle["observed_at"] == "2026-01-06T00:00:00"


@pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "dataset/validate/traffic.csv").exists(),
                    reason="private dataset is not present")
def test_real_validate_sample_has_target_and_past_telemetry():
    with TestClient(create_app(ml_url="", ndtp_enabled=False)) as client:
        result = client.post("/api/v1/replay/step?sample_id=131672_1767670500").json()
        vehicle = next(item for item in result["vehicles"] if item["tr_id"] == 131672)
        assert vehicle["prediction"]["target_stop"]["arrival_id"] == 53700172828
        assert vehicle["schedule_deviation_s"] == 274
        assert vehicle["observed_at"] <= result["at"]
