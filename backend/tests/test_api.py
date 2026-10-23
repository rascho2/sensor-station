from datetime import datetime, timedelta, timezone

import pytest

from app.messages import Reading

START = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def speichere(db, anzahl):
    """Legt `anzahl` Messungen im Minutenabstand ab START an (Temp = Index / 10)."""
    for i in range(anzahl):
        db.insert_reading(Reading(device="sensorpi", timestamp=START + timedelta(minutes=i),
                                  temperature_c=i / 10, pressure_hpa=1000.0,
                                  humidity_pct=None if i % 2 else 50.0))


def test_health_without_mqtt(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "degraded", "database": True, "mqtt_connected": False}


def test_latest_returns_404_when_empty(client):
    assert client.get("/api/readings/latest").status_code == 404


def test_latest_returns_newest_reading(client, db):
    speichere(db, 3)
    r = client.get("/api/readings/latest")
    assert r.status_code == 200
    assert r.json() == {"device": "sensorpi", "timestamp": "2026-10-22T12:02:00Z",
                        "temperature_c": 0.2, "pressure_hpa": 1000.0, "humidity_pct": 50.0}


def test_readings_newest_first_with_default_limit(client, db):
    speichere(db, 150)
    daten = client.get("/api/readings").json()
    assert len(daten) == 100
    assert daten[0]["temperature_c"] == 14.9
    assert daten[-1]["temperature_c"] == 5.0


def test_readings_limit(client, db):
    speichere(db, 10)
    assert [d["temperature_c"] for d in client.get("/api/readings?limit=3").json()] == [0.9, 0.8, 0.7]


def test_readings_since_is_inclusive(client, db):
    speichere(db, 10)
    daten = client.get("/api/readings", params={"since": "2026-10-22T12:07:00Z"}).json()
    assert [d["temperature_c"] for d in daten] == [0.9, 0.8, 0.7]


def test_readings_since_with_offset(client, db):
    speichere(db, 10)
    # 14:08+02:00 == 12:08 UTC; params= kodiert das "+" korrekt als %2B
    daten = client.get("/api/readings", params={"since": "2026-10-22T14:08:00+02:00"}).json()
    assert [d["temperature_c"] for d in daten] == [0.9, 0.8]


def test_readings_bmp280_humidity_is_null(client, db):
    speichere(db, 2)
    assert client.get("/api/readings").json()[0]["humidity_pct"] is None


@pytest.mark.parametrize("query", [
    "limit=0", "limit=1001", "limit=abc",
    "since=gestern",
    "since=2026-10-22T12:00:00",        # ohne Zeitzone: mehrdeutig
])
def test_invalid_query_parameters(client, query):
    assert client.get(f"/api/readings?{query}").status_code == 422
