import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from features import FEATURES
from patterns import detect_patterns

MODEL_PATH = Path(os.getenv("MODEL_PATH", "models/extratrees_all_42.joblib"))
MODEL_VERSION = os.getenv("MODEL_VERSION", "extratrees-all-42")
model = None

class PredictionRequest(BaseModel):
    forecast_time: datetime = Field(description="Момент прогноза T")
    target_time_begin: datetime = Field(description="Плановое прибытие")
    target_stop_lon: float | None = None
    target_stop_lat: float | None = None
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

class PredictionResponse(BaseModel):
    prediction_delay_s: float
    horizon_minutes: float
    model_version: str
    patterns: list[str]

def make_features(request):
    horizon_s = (request.target_time_begin - request.forecast_time).total_seconds()
    if not 600 < horizon_s <= 900:
        raise HTTPException(status_code=422, detail="Плановое прибытие должно находиться в окне T+10…15 минут")
    minutes = request.forecast_time.hour * 60 + request.forecast_time.minute + request.forecast_time.second / 60
    values = request.model_dump()
    values.update(time_to_target_s=horizon_s, time_sin=np.sin(2 * np.pi * minutes / 1440), time_cos=np.cos(2 * np.pi * minutes / 1440))
    row = {feature: np.nan if values.get(feature) is None else values[feature] for feature in FEATURES}
    return pd.DataFrame([row], columns=FEATURES), horizon_s

@asynccontextmanager
async def lifespan(_: FastAPI):
    global model
    if not MODEL_PATH.exists():
        raise RuntimeError(f"Модель не найдена: {MODEL_PATH}")
    model = joblib.load(MODEL_PATH)
    yield
    model = None

app = FastAPI(title="Transport Delay ML API", version="1.0.0", lifespan=lifespan)

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None, "model_version": MODEL_VERSION}

@app.post("/predict", response_model=PredictionResponse)
def predict(request: PredictionRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Модель ещё не загружена")
    features, horizon_s = make_features(request)
    prediction = float(model.predict(features)[0])
    patterns = detect_patterns(request, horizon_s)
    return PredictionResponse(prediction_delay_s=prediction, horizon_minutes=horizon_s / 60, model_version=MODEL_VERSION, patterns=patterns)
