"""Contracts exposed by the backend and consumed from the ML team's API."""

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
    source: Literal["ndtp", "api", "csv"] = "api"
    lon: float | None = Field(default=None, ge=-180, le=180)
    lat: float | None = Field(default=None, ge=-90, le=90)
    alt: float | None = None
    speed_kmh: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    location_valid: bool = False
    doors_closed: bool | None = Field(default=None, description="Состояние дверей; null, если источник его не передаёт")


class VehicleRegistration(BaseModel):
    tr_id: int
    unit_id: int


class ScheduledArrival(BaseModel):
    arrival_id: int = Field(description="ID конкретного планового прибытия")
    tr_id: int
    planned_at: datetime
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    address: str | None = None


class ObservedArrival(BaseModel):
    actual_at: datetime


class ArrivalDeviation(BaseModel):
    arrival_id: int
    tr_id: int
    actual_at: datetime
    current_dev_s: float


class Prediction(BaseModel):
    tr_id: int
    at: datetime
    target_stop: ScheduledArrival
    prediction_s: float
    predicted_arrival: datetime
    horizon_s: float
    delay_probability: float | None = None
    source: Literal["ml", "baseline"]
    model_version: str | None = None
    patterns: list[str] = Field(default_factory=list)


class Vehicle(BaseModel):
    vehicle_id: str
    tr_id: int | None = None
    unit_id: int | None = None
    source: Literal["ndtp", "api", "csv"] = "api"
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
    doors_closed: bool | None = None
    schedule_deviation_s: float | None = None
    segment_speed_kmh: float | None = None
    idle_time_s: float | None = None
    prediction: Prediction | None = None
    target_arrival: ScheduledArrival | None = None
    forecast_status: Literal["ml", "baseline", "last_known", "outside_horizon", "no_target", "insufficient_data", "unknown_vehicle"] = "no_target"
    risk: Literal["green", "yellow", "red", "unknown"] = "unknown"


class Alert(BaseModel):
    alert_id: str
    vehicle_id: str
    tr_id: int
    risk: Literal["yellow", "red"]
    target_stop: ScheduledArrival
    predicted_delay_s: float
    predicted_arrival: datetime
    patterns: list[str] = Field(default_factory=list)
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


class TrackPoint(BaseModel):
    vehicle_id: str
    tr_id: int | None = None
    unit_id: int | None = None
    event_time: datetime
    lon: float | None = None
    lat: float | None = None
    speed_kmh: float | None = None
    location_valid: bool
    source: Literal["ndtp", "api", "csv"]


class Snapshot(BaseModel):
    at: datetime
    mode: Literal["live", "historical"] = "live"
    dataset_split: str | None = None
    vehicles: list[Vehicle]
    alerts: list[Alert]
    stops: list[ScheduledArrival]
    track_points: list[TrackPoint] = Field(default_factory=list)
    processed_packets: int = 0
    summary: Summary
    ml_available: bool


class MLPredictionRequest(BaseModel):
    """Exact JSON fields accepted by ml_core/api/main.py:/predict."""

    forecast_time: datetime
    target_time_begin: datetime
    target_stop_lon: float
    target_stop_lat: float
    last_message_age_s: float | None = None
    last_speed_kmh: float | None = None
    mean_speed_1m_kmh: float | None = None
    mean_speed_3m_kmh: float | None = None
    mean_speed_5m_kmh: float | None = None
    speed_std_5m_kmh: float | None = None
    speed_change_5m_kmh: float | None = None
    stopped_share_5m: float | None = None
    valid_messages_5m: float | None = None
    distance_to_target_km: float | None = None
    current_dev_s: float | None = None


class MLPredictionResponse(BaseModel):
    prediction_delay_s: float = Field(allow_inf_nan=False)
    horizon_minutes: float
    model_version: str
    patterns: list[str]
