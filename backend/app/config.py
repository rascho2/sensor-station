"""
config.py – Einstellungen des Backends, gelesen aus Umgebungsvariablen.

  MQTT_HOST   Adresse des Brokers            (Standard: localhost)
  MQTT_PORT   Port des Brokers               (Standard: 1883)
  MQTT_TOPIC  Abonniertes Topic              (Standard: sensorstation/+/messwerte)
  DB_PATH     Pfad zur SQLite-Datei          (Standard: backend/data/sensorstation.db)
"""

import os
from dataclasses import dataclass
from pathlib import Path

STANDARD_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sensorstation.db"


@dataclass(frozen=True)
class Settings:
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_topic: str = "sensorstation/+/messwerte"
    db_path: Path = STANDARD_DB_PATH

    @classmethod
    def from_env(cls):
        return cls(
            mqtt_host=os.environ.get("MQTT_HOST", cls.mqtt_host),
            mqtt_port=int(os.environ.get("MQTT_PORT", cls.mqtt_port)),
            mqtt_topic=os.environ.get("MQTT_TOPIC", cls.mqtt_topic),
            db_path=Path(os.environ.get("DB_PATH", cls.db_path)),
        )
