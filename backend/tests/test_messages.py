import json
import logging

import pytest

from app.messages import InvalidMessage, handle_message, parse_message

TOPIC = "sensorstation/sensorpi/messwerte"


def payload(**ueberschreiben):
    """Gültige Nachricht wie von device/sensor.py, einzelne Felder änderbar."""
    daten = {"timestamp": "2026-10-22T15:36:20+02:00", "temperature_c": 26.04,
             "pressure_hpa": 1002.47, "humidity_pct": 63.6}
    daten.update(ueberschreiben)
    return json.dumps(daten).encode()


def test_parse_valid_message():
    r = parse_message(TOPIC, payload())
    assert r.device == "sensorpi"
    assert r.temperature_c == 26.04
    assert r.pressure_hpa == 1002.47
    assert r.humidity_pct == 63.6
    assert r.timestamp.utcoffset().total_seconds() == 2 * 3600


def test_bmp280_without_humidity_is_valid():
    assert parse_message(TOPIC, payload(humidity_pct=None)).humidity_pct is None


def test_integer_values_are_accepted_as_float():
    assert parse_message(TOPIC, payload(temperature_c=26)).temperature_c == 26.0


def test_extra_fields_are_ignored():
    assert parse_message(TOPIC, payload(firmware="1.0")).device == "sensorpi"


@pytest.mark.parametrize("topic", [
    "sensorstation/messwerte",
    "sensorstation/a/b/messwerte",
    "andere/sensorpi/messwerte",
    "sensorstation/sensorpi/status",
])
def test_invalid_topic(topic):
    with pytest.raises(InvalidMessage, match="Topic"):
        parse_message(topic, payload())


@pytest.mark.parametrize("roh, grund", [
    (b"kein json", "kein JSON"),
    (b"", "leer"),
    (b"[1, 2]", "kein Objekt"),
    (b"\xff\xfe", "kein UTF-8"),
    (payload(temperature_c="26.0"), "Zahl als String"),
    (payload(temperature_c=True), "Boolean statt Zahl"),
    (payload(timestamp="2026-10-22T15:36:20"), "Zeitstempel ohne Zeitzone"),
    (payload(timestamp="gestern"), "kein Zeitstempel"),
    (payload(temperature_c=150.0), "außerhalb Messbereich"),
    (payload(humidity_pct=-1.0), "Feuchte negativ"),
    (payload(pressure_hpa=0.0), "Druck 0"),
    (b'{"timestamp": "2026-10-22T15:36:20+02:00", "temperature_c": NaN, '
     b'"pressure_hpa": 1002.0, "humidity_pct": 50.0}', "NaN"),
    (json.dumps({"timestamp": "2026-10-22T15:36:20+02:00",
                 "temperature_c": 26.0, "pressure_hpa": 1002.0}).encode(),
     "humidity_pct fehlt"),
])
def test_invalid_payload(roh, grund):
    with pytest.raises(InvalidMessage):
        parse_message(TOPIC, roh)


def test_handle_message_stores_valid_reading(db):
    assert handle_message(db, TOPIC, payload()) is True
    gespeichert = db.latest()
    assert gespeichert["device"] == "sensorpi"
    # in UTC normalisiert: 15:36:20+02:00 -> 13:36:20+00:00
    assert gespeichert["timestamp"] == "2026-10-22T13:36:20+00:00"


def test_handle_message_logs_and_skips_invalid(db, caplog):
    with caplog.at_level(logging.WARNING):
        assert handle_message(db, TOPIC, b"kaputt") is False
    assert db.latest() is None
    assert "Ungültige Nachricht" in caplog.text
