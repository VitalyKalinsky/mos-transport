FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY services/backend/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Эталонное расписание (план) и справочник терминалов — встроены в образ (предсказуемый холодный старт)
COPY dataset/validate/schedule_plan.csv /data/validate/schedule_plan.csv
COPY dataset/validate/traffic.csv /data/validate/traffic.csv

COPY libs/ndtp /app/libs/ndtp
COPY services/backend/app /app/app
WORKDIR /app
ENV PYTHONPATH=/app/libs:/app
EXPOSE 8000 9201
HEALTHCHECK --interval=5s --timeout=3s --start-period=15s --retries=12 \
  CMD curl -fs http://localhost:8000/health | grep -q '"status":"ok"' || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "info"]
