"""Sphinx: документация по коду (autodoc + Google-style docstrings).

Сборка — ``docs/build_docs.sh`` (в образе ML-сервиса: там уже есть CatBoost, pandas и FastAPI).
Результат — ``docs/code/index.html``.
"""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# порядок важен: пакет backend называется `app` и должен перекрыть одноимённый пакет ML-сервиса из PYTHONPATH образа
for p in (ROOT / "services" / "feeder", ROOT / "notebooks", ROOT / "libs", ROOT / "services" / "backend"):
    sys.path.insert(0, str(p))

# пакет ML-сервиса тоже называется `app` — регистрируем его под псевдонимом, относительные импорты работают
_ml = types.ModuleType("ml_service_app")
_ml.__path__ = [str(ROOT / "services" / "ml_service" / "app")]
sys.modules["ml_service_app"] = _ml

# ML-ядро при импорте создаёт каталоги относительно cwd.parent — собираем из notebooks/, чтобы это был корень проекта
os.chdir(ROOT / "notebooks")

project = "Предиктор задержек городского транспорта"
author = "Команда mos-transport"
language = "ru"
release = "1.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",    # Google-style docstrings (Args / Returns / Raises)
    "sphinx.ext.viewcode",    # ссылки на исходный код
]
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False
napoleon_use_ivar = True      # Attributes → :ivar:, без дублей с полями dataclass
suppress_warnings = ["autodoc.import_object"]
# «|факт|», «|вклад|» в docstrings ML-модуля — это модуль, а не RST-подстановка; ML-код не меняем
rst_prolog = ".. |факт| replace:: \\|факт\\|\n.. |вклад| replace:: \\|вклад\\|\n"

html_theme = "furo"
html_title = "mos-transport · документация по коду"
html_static_path = ["_static"]
