"""
db.py – Speichert die Messwerte in einer SQLite-Datenbank.

Zeitstempel werden einheitlich in UTC als ISO-8601-Text gespeichert
(z. B. "2026-10-22T13:36:20+00:00"). Weil alle Werte dasselbe Format und
dieselbe Zeitzone haben, sortieren und vergleichen sie sich als Text
korrekt – "timestamp >= ?" und "ORDER BY timestamp" funktionieren also
ohne Datums-Funktionen von SQLite.

Der MQTT-Thread schreibt, die API-Threads lesen. Deshalb öffnet jede
Operation ihre eigene, kurzlebige Verbindung (sqlite3-Verbindungen sollte
man nicht zwischen Threads teilen), und die Datenbank läuft im WAL-Modus,
damit Lesen und Schreiben sich nicht gegenseitig blockieren.
"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device        TEXT    NOT NULL,
    timestamp     TEXT    NOT NULL,   -- Messzeitpunkt laut Gerät, UTC
    temperature_c REAL    NOT NULL,
    pressure_hpa  REAL    NOT NULL,
    humidity_pct  REAL,               -- NULL beim BMP280
    received_at   TEXT    NOT NULL    -- Empfangszeitpunkt im Backend, UTC
);
CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings (timestamp);
"""

SPALTEN = "device, timestamp, temperature_c, pressure_hpa, humidity_pct"


def als_utc_text(zeitpunkt):
    """Zeitzonenbehafteten datetime -> UTC-ISO-Text, wie er gespeichert wird."""
    return zeitpunkt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, pfad):
        self.pfad = Path(pfad)

    def _verbinden(self):
        verbindung = sqlite3.connect(self.pfad, timeout=10)
        verbindung.row_factory = sqlite3.Row
        return verbindung

    def init_schema(self):
        self.pfad.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._verbinden()) as v:
            v.execute("PRAGMA journal_mode=WAL")
            v.executescript(SCHEMA)

    def insert_reading(self, reading):
        """Speichert eine geprüfte Messung (app.messages.Reading)."""
        with closing(self._verbinden()) as v, v:   # zweites "v": Transaktion
            v.execute(
                f"INSERT INTO readings ({SPALTEN}, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (reading.device, als_utc_text(reading.timestamp),
                 reading.temperature_c, reading.pressure_hpa,
                 reading.humidity_pct, als_utc_text(datetime.now(timezone.utc))),
            )

    def latest(self):
        """Neueste Messung als dict oder None, wenn die Tabelle leer ist."""
        with closing(self._verbinden()) as v:
            zeile = v.execute(
                f"SELECT {SPALTEN} FROM readings "
                "ORDER BY timestamp DESC, id DESC LIMIT 1").fetchone()
        return dict(zeile) if zeile else None

    def list_readings(self, limit, since=None):
        """Bis zu `limit` Messungen, neueste zuerst; optional nur ab `since`."""
        sql = f"SELECT {SPALTEN} FROM readings"
        parameter = []
        if since is not None:
            sql += " WHERE timestamp >= ?"
            parameter.append(als_utc_text(since))
        sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        parameter.append(limit)
        with closing(self._verbinden()) as v:
            return [dict(z) for z in v.execute(sql, parameter)]

    def ping(self):
        """True, wenn die Datenbank erreichbar ist (für /api/health)."""
        try:
            with closing(self._verbinden()) as v:
                v.execute("SELECT 1 FROM readings LIMIT 1")
            return True
        except sqlite3.Error:
            return False
