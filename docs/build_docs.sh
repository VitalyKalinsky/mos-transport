#!/bin/sh
# Сборка документации: Sphinx по коду → docs/code/, OpenAPI → docs/api/.
# Запускается в образе ML-сервиса (в нём уже есть CatBoost, pandas, FastAPI), см. docs/API.md.
set -e
cd "$(dirname "$0")/.."
pip install -q -r docs/requirements.txt
rm -rf docs/code
sphinx-build -q -b html docs/sphinx docs/code
rm -rf docs/code/.doctrees docs/code/.buildinfo
python docs/export_openapi.py
echo "docs/code/index.html и docs/api/*.html готовы"
