"""Generate static PyDoc HTML for the backend code."""

import importlib
import os
import pydoc
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = Path(__file__).resolve().parent / "docs" / "code"
OUTPUT.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT)
for name in ("backend.app.models", "backend.app.data", "backend.app.ndtp", "backend.app.service", "backend.app.main"):
    pydoc.writedoc(importlib.import_module(name))
