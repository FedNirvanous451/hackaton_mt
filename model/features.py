"""Reconstruct the training features from raw telemetry available at T.

The formulas for the CSV-backed fields were checked against every row of the
ML team's test_features_353.csv export. Time sine/cosine are inferred from the
model's feature names and split borders; they are not present in that export.
"""

import math
from datetime import datetime, timedelta, timezone

from backend.app.models import MLRequest, MLTelemetry

EARTH_RADIUS_KM = 6371.0088
MOSCOW = timezone(timedelta(hours=3))

FEATURE_NAMES = (
    "target_stop_lon", "target_stop_lat", "time_to_target_s", "last_message_age_s",
    "last_speed_kmh", "mean_speed_1m_kmh", "mean_speed_3m_kmh", "mean_speed_5m_kmh",
    "speed_std_5m_kmh", "speed_change_5m_kmh", "stopped_share_5m",
    "valid_messages_5m", "distance_to_target_km", "current_dev_s", "time_sin", "time_cos",
)


class InsufficientData(ValueError):
    """The request cannot supply a meaningful model feature vector."""


def _local(value: datetime) -> datetime:
    return value.astimezone(MOSCOW).replace(tzinfo=None) if value.tzinfo else value


def _point(geom: str | None) -> tuple[float, float]:
    if not geom:
        raise InsufficientData("target_geom is missing")
    pieces = geom.strip().removeprefix("POINT").strip()
    if not pieces.startswith("(") or not pieces.endswith(")"):
        raise InsufficientData("target_geom must be a WKT POINT")
    try:
        lon_text, lat_text = pieces[1:-1].split()
        lon, lat = float(lon_text), float(lat_text)
    except ValueError as exc:
        raise InsufficientData("target_geom must contain lon and lat") from exc
    if not math.isfinite(lon) or not math.isfinite(lat) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise InsufficientData("target_geom has invalid coordinates")
    return lon, lat


def _distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else math.nan


def build_features(request: MLRequest) -> dict[str, float]:
    """Use event_time <= T, inclusive trailing windows, and CSV speed units."""
    if request.cur_dev_s is None or not math.isfinite(request.cur_dev_s):
        raise InsufficientData("current_dev_s is required by this trained model")
    at = _local(request.T)
    packets = sorted((item for item in request.telemetry_history
                      if item.tr_id == request.tr_id and _local(item.event_time) <= at),
                     key=lambda item: _local(item.event_time))
    if not packets:
        raise InsufficientData("telemetry_history has no packets at T")
    last: MLTelemetry = packets[-1]
    last_at = _local(last.event_time)
    target_lon, target_lat = _point(request.target_geom)
    recent = [item for item in packets if _local(item.event_time) >= at - timedelta(minutes=5)]
    speeds = [(item, float(item.speed)) for item in recent
              if item.speed is not None and math.isfinite(item.speed)]
    if not speeds:
        raise InsufficientData("no measured speed in the last 5 minutes")
    speeds_5m = [speed for _, speed in speeds]
    mean_5m = _mean(speeds_5m)
    mean_1m = _mean([speed for item, speed in speeds if _local(item.event_time) >= at - timedelta(minutes=1)])
    mean_3m = _mean([speed for item, speed in speeds if _local(item.event_time) >= at - timedelta(minutes=3)])
    std_5m = math.sqrt(sum((speed - mean_5m) ** 2 for speed in speeds_5m) / len(speeds_5m))
    distance = (_distance_km(last.lon, last.lat, target_lon, target_lat)
                if last.location_valid and last.lon is not None and last.lat is not None else math.nan)
    seconds_in_day = at.hour * 3600 + at.minute * 60 + at.second + at.microsecond / 1_000_000
    angle = 2 * math.pi * seconds_in_day / 86400
    values = {
        "target_stop_lon": target_lon,
        "target_stop_lat": target_lat,
        "time_to_target_s": (_local(request.target_time_begin) - at).total_seconds(),
        "last_message_age_s": (at - last_at).total_seconds(),
        "last_speed_kmh": float(last.speed) if last.speed is not None else math.nan,
        "mean_speed_1m_kmh": mean_1m,
        "mean_speed_3m_kmh": mean_3m,
        "mean_speed_5m_kmh": mean_5m,
        "speed_std_5m_kmh": std_5m,
        "speed_change_5m_kmh": speeds_5m[-1] - speeds_5m[0],
        "stopped_share_5m": sum(speed < 2 for speed in speeds_5m) / len(speeds_5m),
        "valid_messages_5m": float(sum(item.location_valid for item in recent)),
        "distance_to_target_km": distance,
        "current_dev_s": float(request.cur_dev_s),
        "time_sin": math.sin(angle),
        "time_cos": math.cos(angle),
    }
    return values
