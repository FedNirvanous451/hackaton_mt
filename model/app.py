"""HTTP inference boundary between the backend and the CatBoost model."""

import asyncio
import hashlib
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

from catboost import CatBoostRegressor
from fastapi import FastAPI

from backend.app.models import MLRequest, MLResponse
from .features import FEATURE_NAMES, InsufficientData, build_features

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "catboost_all.cbm"


def create_app(model_path: Path | None = None) -> FastAPI:
    path = model_path or Path(os.getenv("MODEL_PATH", DEFAULT_MODEL_PATH))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        model = CatBoostRegressor()
        model.load_model(str(path))
        if tuple(model.feature_names_) != FEATURE_NAMES:
            raise RuntimeError("Model feature names or order differ from the validated feature contract")
        app.state.model = model
        app.state.model_version = f"{path.stem}:{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}"
        yield

    app = FastAPI(title="Transport Delay Model API", version="0.1.0", lifespan=lifespan)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        return {"status": "ok", "model_version": app.state.model_version}

    @app.post("/predict", response_model=MLResponse)
    async def predict(request: MLRequest) -> MLResponse:
        try:
            features = build_features(request)
        except InsufficientData:
            return MLResponse(status="insufficient_data")
        vector = [features[name] for name in FEATURE_NAMES]
        value = float((await asyncio.to_thread(app.state.model.predict, [vector]))[0])
        if not math.isfinite(value):
            return MLResponse(status="insufficient_data")
        return MLResponse(prediction_s=value, model_version=app.state.model_version)

    return app


app = create_app()
