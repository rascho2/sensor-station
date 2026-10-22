from datetime import datetime, timedelta, timezone

from app.messages import Reading


def reading(zeit, temp=20.0, device="sensorpi"):
    return Reading(device=device, timestamp=zeit, temperature_c=temp,
                   pressure_hpa=1000.0, humidity_pct=50.0)


def test_empty_database(db):
    assert db.latest() is None
    assert db.list_readings(limit=10) == []
    assert db.ping() is True


def test_latest_uses_measurement_time_not_insert_order(db):
    t = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    db.insert_reading(reading(t + timedelta(minutes=5), temp=25.0))
    db.insert_reading(reading(t, temp=20.0))   # später angekommen, aber älter
    assert db.latest()["temperature_c"] == 25.0


def test_mixed_time_zones_sort_correctly(db):
    # 14:30+02:00 ist 12:30 UTC und damit NEUER als 12:10+00:00, obwohl
    # "14:30+02:00" < "12:10+00:00" als Text nicht stimmen würde.
    plus2 = timezone(timedelta(hours=2))
    db.insert_reading(reading(datetime(2026, 10, 22, 12, 10, tzinfo=timezone.utc), temp=1.0))
    db.insert_reading(reading(datetime(2026, 10, 22, 14, 30, tzinfo=plus2), temp=2.0))
    assert [r["temperature_c"] for r in db.list_readings(limit=10)] == [2.0, 1.0]
