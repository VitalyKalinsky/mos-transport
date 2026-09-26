# ML-сервис: ML-ядро (notebooks/data_functions.py + models/) копируется в образ как есть.
# Раскладка /opt/ml повторяет репозиторий, поэтому PATHS из data_functions
# (PROJECT_ROOT = cwd.parent) указывают на те же файлы, что и при обучении.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY services/ml_service/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# --- ML-ядро (без изменений) ---
COPY notebooks/data_functions.py /opt/ml/notebooks/data_functions.py
COPY models/ /opt/ml/models/
COPY dataset/train/schedule.csv /opt/ml/dataset/train/schedule.csv
# Данные для переобучения (эндпоинт /v1/train, выключен по умолчанию)
COPY dataset/train/traffic.csv /opt/ml/dataset/train/traffic.csv
COPY dataset/test/ /opt/ml/dataset/test/
COPY dataset/labels/ /opt/ml/dataset/labels/

# --- HTTP-обёртка ---
COPY services/ml_service/app /opt/ml/service/app

WORKDIR /opt/ml/notebooks
ENV PYTHONPATH=/opt/ml/notebooks:/opt/ml/service
EXPOSE 8001
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 \
  CMD curl -fs http://localhost:8001/health | grep -q '"ready":true' || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--log-level", "info"]
