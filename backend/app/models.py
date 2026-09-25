"""Public API contracts shared with the dashboard and ML service."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Telemetry(BaseModel):
    tr_id: int | None = None
    unit_id: int | None = None
    event_time: datetime
    received_at: datetime | None = None
    packet_id: str | None = None
    device_event_id: int | None = None
    gps_time: datetime | None = None
    is_hist_data: bool | None = None
    source: Literal["csv", "ndtp", "api"] = "api"
    lon: float | None = Field(default=None, ge=-180, le=180)
    lat: float | None = Field(default=None, ge=-90, le=90)
    alt: float | None = None
    speed_kmh: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    location_valid: bool = False


class Stop(BaseModel):
    stop_id: int = Field(description="Compatibility alias for arrival_id; not a physical stop ID")
    arrival_id: int = Field(description="Unique tt_action_item_id of a scheduled arrival")
    tr_id: int
    planned_at: datetime
    lon: float | None = None
    lat: float | None = None
    address: str | None = None
    geom: str | None = None


class Prediction(BaseModel):
    tr_id: int
    at: datetime
    sample_id: str | None = None
    target_stop: Stop
    prediction_s: float
    predicted_arrival: datetime
    horizon_s: float
    delay_probability: float | None = Field(default=None, ge=0, le=1)
    source: Literal["ml", "baseline"]
    model_version: str | None = None
    pattern: str | None = None
    reason: str | None = None


class Vehicle(BaseModel):
    vehicle_id: str
    tr_id: int | None = None
    unit_id: int | None = None
    observed_at: datetime
    received_at: datetime | None = None
    location_observed_at: datetime | None = None
    lon: float | None = None
    lat: float | None = None
    speed_kmh: float | None = None
    heading_deg: float | None = None
    location_valid: bool
    location_stale: bool = False
    stale: bool
    prediction: Prediction | None = None
    target_arrival: Stop | None = None
    forecast_status: Literal["ml", "baseline", "no_target", "insufficient_data", "unknown_vehicle"] = "no_target"
    risk: Literal["green", "yellow", "red", "unknown"] = "unknown"


class Alert(BaseModel):
    alert_id: str
    vehicle_id: str
    tr_id: int | None = None
    risk: Literal["yellow", "red"]
    target_stop: Stop
    predicted_delay_s: float
    predicted_arrival: datetime
    delay_probability: float | None = None
    reason: str | None = None
    pattern: str | None = None
    recommendation: str
    source: Literal["ml", "baseline"]
    created_at: datetime
    updated_at: datetime


class Summary(BaseModel):
    total_vehicles: int
    located_vehicles: int
    stale_vehicles: int
    alerts_yellow: int
    alerts_red: int


class Snapshot(BaseModel):
    at: datetime
    mode: Literal["historical", "live"]
    dataset_split: Literal["validate", "train", "test"] = "validate"
    vehicles: list[Vehicle]
    alerts: list[Alert]
    stops: list[Stop]
    summary: Summary
    ml_available: bool
    demo_schedule_shift_days: int = 0


class MLTelemetry(BaseModel):
    """Raw fields from traffic.csv; absent NDTP fields remain null."""

    packet_id: str | None = None
    tr_id: int
    unit_id: int | None = None
    event_time: datetime
    device_event_id: int | None = None
    location_valid: bool
    gps_time: datetime | None = None
    lon: float | None = None
    lat: float | None = None
    alt: float | None = None
    speed: float | None = None
    heading: float | None = None
    receive_time: datetime | None = None
    is_hist_data: bool | None = None


class MLRequest(BaseModel):
    sample_id: str | None = None
    tr_id: int
    T: datetime
    target_stop_id: int = Field(description="Dataset name for the scheduled arrival ID")
    target_time_begin: datetime
    target_geom: str | None = None
    cur_dev_s: float | None = None
    telemetry_history: list[MLTelemetry]


class MLResponse(BaseModel):
    prediction_s: float | None = Field(default=None, allow_inf_nan=False)
    status: Literal["ok", "insufficient_data"] = "ok"
    delay_probability: float | None = Field(default=None, ge=0, le=1)
    model_version: str | None = None
    pattern: str | None = None
    reason: str | None = None
