"""
mqtt_subscriber.py – Empfängt die Messwerte vom Broker im Hintergrund.

paho-mqtt läuft mit loop_start() in einem eigenen Thread, unabhängig von
den Anfragen an die API. Verbindungsaufbau und Reconnect übernimmt paho
selbst (connect_async + reconnect_delay_set), ein fehlender Broker
blockiert also weder den Start noch die API.
"""

import logging

import paho.mqtt.client as mqtt

from app.messages import handle_message

log = logging.getLogger(__name__)


class MqttSubscriber:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="sensorstation-backend")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.beendet = False   # gewolltes Trennen nicht als Fehler melden

    def start(self):
        s = self.settings
        log.info("MQTT: verbinde mit %s:%s, Topic '%s'", s.mqtt_host, s.mqtt_port, s.mqtt_topic)
        self.client.connect_async(s.mqtt_host, s.mqtt_port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.beendet = True
        self.client.disconnect()
        self.client.loop_stop()

    def is_connected(self):
        return self.client.is_connected()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("MQTT: Broker lehnt Verbindung ab (%s)", reason_code)
            return
        # Bei JEDEM (Re-)Connect neu abonnieren: Ohne persistente Session
        # vergisst der Broker die Abos, wenn die Verbindung abreißt.
        client.subscribe(self.settings.mqtt_topic, qos=0)
        log.info("MQTT: verbunden, abonniert '%s'", self.settings.mqtt_topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.beendet:
            return
        log.warning("MQTT: Verbindung getrennt (%s), Reconnect läuft", reason_code)

    def _on_message(self, client, userdata, message):
        # Eine Exception hier würde den MQTT-Thread beenden, und danach
        # käme keine einzige Nachricht mehr an. Deshalb alles abfangen,
        # was handle_message() nicht schon selbst behandelt (z. B. eine
        # gesperrte oder volle Datenbank).
        try:
            handle_message(self.db, message.topic, message.payload)
        except Exception:
            log.exception("Fehler beim Verarbeiten der Nachricht auf '%s'", message.topic)
