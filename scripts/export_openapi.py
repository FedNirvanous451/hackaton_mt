"""Export the FastAPI Swagger/OpenAPI schemas kept in the repository."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.main import app as backend_app  # noqa: E402

sys.path.insert(0, str(ROOT / "ml_core"))
from api.main import app as ml_app  # noqa: E402

for app, destination in (
    (backend_app, ROOT / "backend" / "openapi.json"),
    (ml_app, ROOT / "ml_core" / "openapi.json"),
):
    destination.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(destination)
