"""
main.py – FastAPI-Anwendung mit REST-API für die Messwerte.

Start (im Ordner backend/):  venv/bin/uvicorn app.main:app
Unter "/" liefert dieselbe App das Dashboard aus app/static/ aus.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel

from app.config import Settings
from app.db import Database
from app.mqtt_subscriber import MqttSubscriber

STATIC_DIR = Path(__file__).resolve().parent / "static"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class ReadingOut(BaseModel):
    device: str
    timestamp: datetime          # immer UTC
    temperature_c: float
    pressure_hpa: float
    humidity_pct: float | None


class AggregatedReading(BaseModel):
    device: str
    bucket_start: datetime       # Beginn des Zeitfensters, UTC
    count: int                   # Anzahl Messungen im Fenster
    temperature_c: float
    pressure_hpa: float
    humidity_pct: float | None


class Health(BaseModel):
    status: str
    database: bool
    mqtt_connected: bool


def create_app(settings=None, start_mqtt=True):
    """App-Fabrik: Tests übergeben eigene Settings und schalten MQTT ab."""
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    subscriber = MqttSubscriber(settings, db) if start_mqtt else None

    @asynccontextmanager
    async def lifespan(app):
        db.init_schema()
        if subscriber:
            subscriber.start()
        yield
        if subscriber:
            subscriber.stop()

    app = FastAPI(title="Sensor Station API", lifespan=lifespan)

    @app.get("/api/health", response_model=Health)
    def health():
        database_ok = db.ping()
        return Health(
            # "degraded" statt Fehler: Die API liefert gespeicherte Werte
            # auch dann noch, wenn gerade kein Broker erreichbar ist.
            status="ok" if database_ok and subscriber and subscriber.is_connected()
                   else "degraded",
            database=database_ok,
            mqtt_connected=bool(subscriber and subscriber.is_connected()),
        )

    @app.get("/api/readings/latest", response_model=ReadingOut)
    def latest_reading():
        reading = db.latest()
        if reading is None:
            raise HTTPException(status_code=404, detail="Noch keine Messwerte vorhanden")
        return reading

    @app.get("/api/readings", response_model=list[ReadingOut])
    def list_readings(
        limit: Annotated[int, Query(ge=1, le=1000, description="Maximale Anzahl")] = 100,
        since: Annotated[AwareDatetime | None, Query(
            description="Nur Messungen ab diesem Zeitpunkt (ISO 8601 mit Zeitzone)")] = None,
    ):
        """Messungen, neueste zuerst."""
        return db.list_readings(limit=limit, since=since)

    @app.get("/api/readings/aggregated", response_model=list[AggregatedReading])
    def aggregated_readings(
        since: Annotated[AwareDatetime, Query(
            description="Nur Messungen ab diesem Zeitpunkt (ISO 8601 mit Zeitzone)")],
        bucket_seconds: Annotated[int, Query(
            ge=10, le=86400, description="Breite eines Zeitfensters")] = 60,
        device: Annotated[str | None, Query(description="Nur dieses Gerät")] = None,
    ):
        """Mittelwerte pro Zeitfenster, älteste zuerst (für Diagramme)."""
        return db.aggregate(since=since, bucket_seconds=bucket_seconds, device=device)

    # Dashboard (HTML/CSS/JS) unter "/". Muss NACH den API-Routen stehen:
    # Ein Mount auf "/" fängt sonst alle Pfade ab, auch /api/... .
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")

    return app


app = create_app()
