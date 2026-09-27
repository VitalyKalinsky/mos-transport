"""Выгрузка OpenAPI всех трёх HTTP-сервисов в docs/api/ (JSON + статическая HTML-страница Redoc).

Спецификация берётся из самих приложений FastAPI (без запуска серверов), поэтому она всегда совпадает
с кодом. Живой Swagger — http://localhost:8000/docs, :8001/docs, :8090/docs.
"""
import json
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "api"

for p in (ROOT / "services" / "feeder", ROOT / "notebooks", ROOT / "libs", ROOT / "services" / "backend"):
    sys.path.insert(0, str(p))
_ml = types.ModuleType("ml_service_app")          # пакет ML-сервиса тоже называется `app`
_ml.__path__ = [str(ROOT / "services" / "ml_service" / "app")]
sys.modules["ml_service_app"] = _ml
os.chdir(ROOT / "notebooks")                       # ML-ядро создаёт каталоги относительно cwd.parent

from app.main import app as backend_app            # noqa: E402
from ml_service_app.main import app as ml_app      # noqa: E402
from feeder import app as feeder_app               # noqa: E402

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>body{{margin:0}}</style></head>
<body><div id="redoc"></div>
<script src="https://cdn.jsdelivr.net/npm/redoc@2.5.0/bundles/redoc.standalone.js"></script>
<script>Redoc.init({spec}, {{hideDownloadButton: false}}, document.getElementById("redoc"));</script>
</body></html>
"""

OUT.mkdir(parents=True, exist_ok=True)
for name, app in (("backend", backend_app), ("ml-service", ml_app), ("feeder", feeder_app)):
    spec = app.openapi()
    (OUT / f"openapi-{name}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    # спецификация встроена в страницу: открывается двойным кликом, без веб-сервера
    (OUT / f"{name}.html").write_text(
        PAGE.format(title=spec["info"]["title"], spec=json.dumps(spec, ensure_ascii=False)), encoding="utf-8")
    print(f"{name}: {len(spec['paths'])} путей → docs/api/{name}.html")
