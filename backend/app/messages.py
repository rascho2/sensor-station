"""
messages.py – Prüft eingehende MQTT-Nachrichten und speichert sie.

Bewusst getrennt vom MQTT-Client: parse_message() und handle_message()
bekommen nur Topic und Payload als Bytes. So lassen sie sich ohne Broker
testen, und der Client in mqtt_subscriber.py bleibt eine dünne Hülle.
"""

import logging
import re

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

# sensorstation/<gerät>/messwerte – der Gerätename ist der mittlere Teil
TOPIC_MUSTER = re.compile(r"^sensorstation/([^/]+)/messwerte$")


class InvalidMessage(ValueError):
    """Nachricht kann nicht gespeichert werden (Topic oder Inhalt ungültig)."""


class Payload(BaseModel):
    """JSON-Inhalt einer Nachricht, wie ihn device/sensor.py verschickt.

    strict=True: Strings wie "23.4" werden NICHT stillschweigend in Zahlen
    umgewandelt. Die Grenzen entsprechen den Messbereichen des BME280 laut
    Datenblatt; Werte außerhalb deuten auf einen Fehler hin.
    """
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    timestamp: AwareDatetime                     # ohne Zeitzone -> ungültig
    temperature_c: float = Field(ge=-40, le=85)
    pressure_hpa: float = Field(ge=300, le=1100)
    humidity_pct: float | None = Field(ge=0, le=100)  # None beim BMP280


class Reading(Payload):
    device: str


def parse_message(topic, payload):
    """Topic + Payload (Bytes) -> Reading. Wirft InvalidMessage."""
    treffer = TOPIC_MUSTER.match(topic)
    if not treffer:
        raise InvalidMessage(f"unerwartetes Topic '{topic}'")
    try:
        daten = Payload.model_validate_json(payload)
    except ValidationError as e:
        # Kurzfassung statt des mehrzeiligen pydantic-Texts fürs Log
        fehler = "; ".join(f"{'.'.join(map(str, f['loc'])) or 'payload'}: {f['msg']}"
                           for f in e.errors())
        raise InvalidMessage(fehler) from None
    return Reading(device=treffer.group(1), **daten.model_dump())


def handle_message(db, topic, payload):
    """Prüft und speichert eine Nachricht. Ungültige werden geloggt und
    übersprungen. Gibt zurück, ob gespeichert wurde."""
    try:
        reading = parse_message(topic, payload)
    except InvalidMessage as e:
        log.warning("Ungültige Nachricht auf '%s' übersprungen: %s – Payload: %.200r",
                    topic, e, payload)
        return False
    db.insert_reading(reading)
    return True
