"""Tests für die MQTT-Callbacks – ohne Broker: Die Callbacks werden direkt
mit nachgebauten Client-/Nachrichtenobjekten aufgerufen."""

from types import SimpleNamespace
from unittest.mock import Mock

from paho.mqtt.reasoncodes import ReasonCode
from paho.mqtt.packettypes import PacketTypes

from app.config import Settings
from app.mqtt_subscriber import MqttSubscriber

GUELTIG = (b'{"timestamp": "2026-10-22T15:36:20+02:00", "temperature_c": 26.04, '
           b'"pressure_hpa": 1002.47, "humidity_pct": 63.6}')


def nachricht(payload, topic="sensorstation/sensorpi/messwerte"):
    return SimpleNamespace(topic=topic, payload=payload)


def test_subscribes_on_every_connect(db):
    sub = MqttSubscriber(Settings(), db)
    client = Mock()
    erfolg = ReasonCode(PacketTypes.CONNACK, "Success")
    sub._on_connect(client, None, None, erfolg, None)
    sub._on_connect(client, None, None, erfolg, None)   # nach Reconnect
    assert client.subscribe.call_count == 2
    client.subscribe.assert_called_with("sensorstation/+/messwerte", qos=0)


def test_no_subscribe_when_broker_refuses(db):
    sub = MqttSubscriber(Settings(), db)
    client = Mock()
    sub._on_connect(client, None, None, ReasonCode(PacketTypes.CONNACK, "Not authorized"), None)
    client.subscribe.assert_not_called()


def test_on_message_stores_reading(db):
    MqttSubscriber(Settings(), db)._on_message(None, None, nachricht(GUELTIG))
    assert db.latest()["temperature_c"] == 26.04


def test_invalid_message_does_not_stop_processing(db):
    sub = MqttSubscriber(Settings(), db)
    sub._on_message(None, None, nachricht(b"kaputt"))
    sub._on_message(None, None, nachricht(GUELTIG))
    assert len(db.list_readings(limit=10)) == 1


def test_database_error_is_logged_not_raised(db, caplog):
    db.insert_reading = Mock(side_effect=OSError("Platte voll"))
    MqttSubscriber(Settings(), db)._on_message(None, None, nachricht(GUELTIG))  # kein raise
    assert "Fehler beim Verarbeiten" in caplog.text
