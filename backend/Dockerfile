# Backend-Image: FastAPI + MQTT-Subscriber + Dashboard in einem Prozess.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/sensorstation.db

WORKDIR /app

# Erst nur die Abhängigkeiten: Diese Schicht bleibt im Cache, solange sich
# requirements.txt nicht ändert, auch wenn sich der Code ändert.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Eigener Benutzer statt root. /data gehört ihm schon im Image, damit ein
# neues Volume beim ersten Einhängen diese Rechte übernimmt.
RUN useradd --system --uid 10001 --no-create-home sensorstation \
    && mkdir /data && chown sensorstation /data

COPY app/ app/

USER sensorstation
EXPOSE 8000

# Exec-Form: uvicorn ist PID 1 und bekommt SIGTERM von "docker stop" direkt,
# die Lifespan trennt dann sauber vom Broker.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
