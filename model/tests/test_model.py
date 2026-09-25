"""Independent feature-table regression and model HTTP integration checks."""

import csv
import math
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.data import DataStore
from backend.app.models import MLRequest, MLTelemetry
from model.app import create_app
from model.features import FEATURE_NAMES, build_features

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = Path(__file__).parent / "reference/test_features_353.csv"
CSV_FEATURES = [name for name in FEATURE_NAMES if name not in ("time_sin", "time_cos")]


def _request(data: DataStore, sample_id: str) -> MLRequest:
    point = data.points_by_id[sample_id]
    target = data.stop_by_arrival[point["target_stop_id"]]
    _, history = data.telemetry_at(point["tr_id"], point["T"], 15)
    return MLRequest(sample_id=sample_id, tr_id=point["tr_id"], T=point["T"],
        target_stop_id=target.arrival_id, target_time_begin=target.planned_at,
        target_geom=target.geom, cur_dev_s=point["cur_dev_s"],
        telemetry_history=[MLTelemetry(packet_id=x.packet_id, tr_id=x.tr_id,
            unit_id=x.unit_id, event_time=x.event_time, device_event_id=x.device_event_id,
            location_valid=x.location_valid, gps_time=x.gps_time, lon=x.lon, lat=x.lat,
            alt=x.alt, speed=x.speed_kmh, heading=x.heading_deg,
            receive_time=x.received_at, is_hist_data=x.is_hist_data) for x in history])


def test_features_match_all_353_reference_rows():
    if not REFERENCE.exists():
        pytest.skip("ML team's reference feature table is not distributed with the repository")
    data = DataStore(ROOT / "dataset", split="test")
    with REFERENCE.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 353
    for row in rows:
        features = build_features(_request(data, row["sample_id"]))
        assert tuple(features) == FEATURE_NAMES
        for name in CSV_FEATURES:
            expected = float(row[name]) if row[name] else math.nan
            actual = features[name]
            assert (math.isnan(actual) and math.isnan(expected)) or math.isclose(
                actual, expected, rel_tol=1e-10, abs_tol=1e-10), (row["sample_id"], name, actual, expected)


def test_model_predicts_from_raw_contract():
    data = DataStore(ROOT / "dataset", split="test")
    request = _request(data, "129964_1767666000")
    with TestClient(create_app(ROOT / "model/catboost_all.cbm")) as client:
        response = client.post("/predict", json=request.model_dump(mode="json"))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert math.isfinite(body["prediction_s"])
        assert body["model_version"].startswith("catboost_all:")
        assert body["delay_probability"] is None
        assert client.get("/health/ready").json()["model_version"] == body["model_version"]
        live_like = request.model_copy(update={"cur_dev_s": None})
        assert client.post("/predict", json=live_like.model_dump(mode="json")).json()["status"] == "insufficient_data"
        missing = request.model_copy(update={"telemetry_history": []})
        assert client.post("/predict", json=missing.model_dump(mode="json")).json()["status"] == "insufficient_data"


def test_future_packet_cannot_change_features():
    data = DataStore(ROOT / "dataset", split="test")
    request = _request(data, "129964_1767666000")
    future = MLTelemetry(tr_id=request.tr_id, event_time=request.T + timedelta(seconds=1),
                         location_valid=True, speed=100, lon=37.0, lat=55.0)
    with_future = request.model_copy(update={"telemetry_history": request.telemetry_history + [future]})
    before, after = build_features(request), build_features(with_future)
    for name in FEATURE_NAMES:
        assert before[name] == after[name] or math.isnan(before[name]) and math.isnan(after[name])
