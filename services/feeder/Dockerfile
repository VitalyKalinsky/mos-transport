FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY services/feeder/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY dataset/validate/traffic.csv /data/validate/traffic.csv
COPY libs/ndtp /app/libs/ndtp
COPY services/feeder/feeder.py /app/feeder.py
WORKDIR /app
ENV PYTHONPATH=/app/libs:/app
EXPOSE 8090
HEALTHCHECK --interval=5s --timeout=3s --start-period=20s --retries=12 CMD curl -fs http://localhost:8090/status || exit 1
CMD ["uvicorn", "feeder:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", "warning"]
