from datetime import datetime, timedelta, timezone

from app.messages import Reading

START = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
URL = "/api/readings/aggregated"


def speichere(db, sekunde, temp, feuchte=50.0, device="sensorpi"):
    """Eine Messung `sekunde` Sekunden nach START."""
    db.insert_reading(Reading(device=device, timestamp=START + timedelta(seconds=sekunde),
                              temperature_c=temp, pressure_hpa=1000.0 + temp,
                              humidity_pct=feuchte))


def hole(client, **params):
    params.setdefault("since", "2026-10-22T00:00:00Z")
    r = client.get(URL, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_database(client):
    assert hole(client) == []


def test_averages_per_bucket(client, db):
    # Fenster 12:00:00–12:00:59: 20, 22, 24 -> 22; Fenster 12:01: 30
    for sekunde, temp in [(0, 20.0), (20, 22.0), (59, 24.0), (60, 30.0)]:
        speichere(db, sekunde, temp)
    daten = hole(client, bucket_seconds=60)
    assert daten == [
        {"device": "sensorpi", "bucket_start": "2026-10-22T12:00:00Z", "count": 3,
         "temperature_c": 22.0, "pressure_hpa": 1022.0, "humidity_pct": 50.0},
        {"device": "sensorpi", "bucket_start": "2026-10-22T12:01:00Z", "count": 1,
         "temperature_c": 30.0, "pressure_hpa": 1030.0, "humidity_pct": 50.0},
    ]


def test_bucket_size_and_alignment(client, db):
    # Fenster beginnen an vollen Vielfachen der Fensterbreite (Unix-Zeit, UTC)
    for sekunde in range(0, 900, 30):          # 12:00:00 bis 12:14:30
        speichere(db, sekunde, 20.0)
    daten = hole(client, bucket_seconds=300)
    assert [d["bucket_start"] for d in daten] == [
        "2026-10-22T12:00:00Z", "2026-10-22T12:05:00Z", "2026-10-22T12:10:00Z"]
    assert [d["count"] for d in daten] == [10, 10, 10]


def test_values_are_rounded(client, db):
    for sekunde, temp in [(0, 20.0), (1, 20.0), (2, 21.0)]:   # Mittel 20.333…
        speichere(db, sekunde, temp)
    assert hole(client)[0]["temperature_c"] == 20.33


def test_since_filters_readings(client, db):
    speichere(db, 0, 10.0)
    speichere(db, 120, 20.0)
    daten = hole(client, since="2026-10-22T14:01:00+02:00")   # = 12:01 UTC
    assert [d["temperature_c"] for d in daten] == [20.0]


def test_humidity_average_ignores_missing_values(client, db):
    speichere(db, 0, 20.0, feuchte=40.0)
    speichere(db, 1, 20.0, feuchte=None)
    speichere(db, 2, 20.0, feuchte=60.0)
    speichere(db, 120, 20.0, feuchte=None)       # nur BMP280-Werte im Fenster
    daten = hole(client)
    assert [d["humidity_pct"] for d in daten] == [50.0, None]


def test_devices_are_aggregated_separately(client, db):
    speichere(db, 0, 10.0, device="sensorpi")
    speichere(db, 1, 30.0, device="balkon")
    daten = hole(client)
    assert {d["device"]: d["temperature_c"] for d in daten} == {"sensorpi": 10.0, "balkon": 30.0}
    assert [d["device"] for d in hole(client, device="balkon")] == ["balkon"]


def test_invalid_parameters(client):
    for params in [{},                                           # since fehlt
                   {"since": "2026-10-22T00:00:00"},             # ohne Zeitzone
                   {"since": "2026-10-22T00:00:00Z", "bucket_seconds": 0},
                   {"since": "2026-10-22T00:00:00Z", "bucket_seconds": 9},
                   {"since": "2026-10-22T00:00:00Z", "bucket_seconds": 86401}]:
        assert client.get(URL, params=params).status_code == 422, params
