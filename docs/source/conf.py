"""Sphinx configuration for the transport dispatcher code reference."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ml_core"))

project = "Прогноз задержек городского транспорта"
author = "Команда проекта"
language = "ru"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "member-order": "bysource",
}
autodoc_typehints = "description"

html_theme = "alabaster"
html_title = "Документация по коду — транспортный диспетчер"
html_sidebars = {"**": ["about.html", "navigation.html", "searchbox.html"]}
