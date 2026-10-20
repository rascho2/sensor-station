#!/usr/bin/env python3
"""
sensor.py – Liest einen BME280 bzw. BMP280 über I2C aus und zeigt die
Werte in der Konsole und (falls vorhanden) auf einem OLED-Display an.
Optional wird jede Messung zusätzlich per MQTT als JSON verschickt.

Hardware:  Raspberry Pi Zero W, Sensor an I2C-Bus 1, Adresse 0x76
           optional: 1,3"-OLED (SH1106, 128x64) an I2C-Bus 1, Adresse 0x3C
Abhängigkeiten: smbus2, optional luma.oled (für das Display),
                optional paho-mqtt >= 2.0 (für MQTT)

Aufruf:  venv/bin/python sensor.py [--mqtt-host 172.20.10.6] [--mqtt-port 1883]

Funktionsweise in Kurzform:
  1. Chip-ID lesen       -> BME280 (0x60) oder BMP280 (0x58)?
  2. Kalibrierwerte lesen -> jeder Sensor wird im Werk einzeln vermessen,
                             die Korrekturfaktoren stehen in seinem ROM.
  3. Messung anstoßen    -> "Forced Mode": Sensor misst einmal und schläft dann.
  4. Rohwerte lesen      -> 20-Bit-Werte (Temp/Druck) bzw. 16-Bit (Feuchte).
  5. Kompensieren        -> Rohwerte + Kalibrierwerte = echte physikalische Werte
                             (Formeln aus dem Bosch-Datenblatt, Kapitel 4.2.3 / 8.1).
"""

import argparse
import json
import socket
import time
from datetime import datetime

from smbus2 import SMBus

# luma.oled ist optional: Fehlt die Bibliothek, läuft das Skript nur mit
# Konsolenausgabe weiter.
try:
    from luma.core.interface.serial import i2c as luma_i2c
    from luma.core.render import canvas
    from luma.oled.device import sh1106
    from PIL import ImageFont
except ImportError:
    sh1106 = None

# paho-mqtt ist ebenfalls optional: Ohne die Bibliothek (oder ohne
# --mqtt-host) wird einfach nichts verschickt.
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
I2C_BUS = 1          # Am Pi liegen GPIO2 (SDA) / GPIO3 (SCL) auf Bus 1
I2C_ADRESSE = 0x76   # 0x76 wenn SDO auf GND, 0x77 wenn SDO auf VCC
INTERVALL_S = 2.0    # Abstand zwischen zwei Messungen in Sekunden

OLED_ADRESSE = 0x3C  # SH1106-Display, gleicher I2C-Bus wie der Sensor
OLED_SCHRIFT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

MQTT_PORT = 1883                  # Standard-Port für unverschlüsseltes MQTT
MQTT_TOPIC = "sensorstation/{hostname}/messwerte"

# ---------------------------------------------------------------------------
# Register-Adressen (siehe Datenblatt, "Memory map")
# ---------------------------------------------------------------------------
REG_CHIP_ID = 0xD0     # Enthält die Chip-ID (nur lesen)
REG_RESET = 0xE0       # 0xB6 hineinschreiben = Soft-Reset
REG_CTRL_HUM = 0xF2    # Oversampling Feuchte (nur BME280)
REG_STATUS = 0xF3      # Bit 3 = "misst gerade", Bit 0 = "kopiert NVM-Daten"
REG_CTRL_MEAS = 0xF4   # Oversampling Temp/Druck + Betriebsmodus
REG_CONFIG = 0xF5      # Standby-Zeit, IIR-Filter
REG_DATEN = 0xF7       # Ab hier liegen die Messwerte (8 Bytes beim BME280)

REG_CALIB_TP = 0x88    # Kalibrierblock 1: 0x88..0xA1 (26 Bytes)
REG_CALIB_H = 0xE1     # Kalibrierblock 2: 0xE1..0xE7 (7 Bytes, nur BME280)

CHIP_ID_BME280 = 0x60
CHIP_IDS_BMP280 = (0x56, 0x57, 0x58)  # 0x56/0x57 = Vorserienmuster


# ---------------------------------------------------------------------------
# Hilfsfunktionen zum Zusammensetzen von Bytes
# ---------------------------------------------------------------------------
def u16_le(daten, i):
    """Zwei Bytes ab Index i als vorzeichenlose 16-Bit-Zahl (Little Endian:
    erst das niederwertige Byte, dann das höherwertige)."""
    return daten[i] | (daten[i + 1] << 8)


def s16_le(daten, i):
    """Wie u16_le, aber vorzeichenbehaftet (Zweierkomplement)."""
    wert = u16_le(daten, i)
    return wert - 0x10000 if wert & 0x8000 else wert


def s8(wert):
    """Ein Byte als vorzeichenbehaftete 8-Bit-Zahl interpretieren."""
    return wert - 0x100 if wert & 0x80 else wert


def s12(wert):
    """12-Bit-Zahl im Zweierkomplement in ein Python-int umwandeln."""
    return wert - 0x1000 if wert & 0x800 else wert


# ---------------------------------------------------------------------------
# Sensor-Klasse
# ---------------------------------------------------------------------------
class BME280:
    def __init__(self, bus, adresse):
        self.bus = bus
        self.adr = adresse

        # 1) Welcher Chip ist das?
        chip_id = self.bus.read_byte_data(self.adr, REG_CHIP_ID)
        if chip_id == CHIP_ID_BME280:
            self.hat_feuchte = True
            self.name = "BME280"
        elif chip_id in CHIP_IDS_BMP280:
            self.hat_feuchte = False
            self.name = "BMP280"
        else:
            raise RuntimeError(f"Unbekannte Chip-ID 0x{chip_id:02X} – "
                               "ist wirklich ein BME/BMP280 angeschlossen?")

        # 2) Soft-Reset: Sensor in einen definierten Zustand bringen.
        self.bus.write_byte_data(self.adr, REG_RESET, 0xB6)
        time.sleep(0.005)
        # Nach dem Reset kopiert der Sensor seine Kalibrierdaten aus dem
        # nichtflüchtigen Speicher (NVM) in die Register. Solange Bit 0 im
        # Status-Register gesetzt ist, ist er damit noch beschäftigt.
        while self.bus.read_byte_data(self.adr, REG_STATUS) & 0x01:
            time.sleep(0.002)

        # 3) Kalibrierwerte einlesen
        self._lese_kalibrierung()

        # 4) Grundkonfiguration
        # config (0xF5): Standby egal (wir nutzen Forced Mode), IIR-Filter aus.
        self.bus.write_byte_data(self.adr, REG_CONFIG, 0x00)
        if self.hat_feuchte:
            # ctrl_hum (0xF2): Feuchte-Oversampling x1 (Bits 2..0 = 001).
            # WICHTIG: Eine Änderung an ctrl_hum wird erst wirksam, nachdem
            # danach ctrl_meas geschrieben wurde – das passiert in messen().
            self.bus.write_byte_data(self.adr, REG_CTRL_HUM, 0b001)

        # t_fine: Zwischenergebnis der Temperaturberechnung. Druck- und
        # Feuchteberechnung brauchen es, weil beide temperaturabhängig sind.
        self.t_fine = 0.0

    def _lese_kalibrierung(self):
        """Liest die werkseitigen Kalibrierkoeffizienten aus dem Sensor.

        Die Namen dig_T1, dig_P1, dig_H1 ... stammen direkt aus dem
        Datenblatt. Wichtig ist, ob ein Wert mit oder ohne Vorzeichen
        gespeichert ist – das steht ebenfalls im Datenblatt (Tabelle 16
        beim BME280).
        """
        # Block 1: 0x88..0xA1 = 26 Bytes (Temperatur, Druck, H1)
        c = self.bus.read_i2c_block_data(self.adr, REG_CALIB_TP, 26)

        # Temperatur
        self.dig_T1 = u16_le(c, 0)    # 0x88/0x89 unsigned
        self.dig_T2 = s16_le(c, 2)    # 0x8A/0x8B signed
        self.dig_T3 = s16_le(c, 4)    # 0x8C/0x8D signed

        # Druck
        self.dig_P1 = u16_le(c, 6)    # 0x8E/0x8F unsigned
        self.dig_P2 = s16_le(c, 8)
        self.dig_P3 = s16_le(c, 10)
        self.dig_P4 = s16_le(c, 12)
        self.dig_P5 = s16_le(c, 14)
        self.dig_P6 = s16_le(c, 16)
        self.dig_P7 = s16_le(c, 18)
        self.dig_P8 = s16_le(c, 20)
        self.dig_P9 = s16_le(c, 22)   # 0x9E/0x9F
        # c[24] = Register 0xA0 ist unbenutzt

        if not self.hat_feuchte:
            return

        # Feuchte – leider etwas verstreut gespeichert:
        self.dig_H1 = c[25]           # 0xA1 unsigned char

        # Block 2: 0xE1..0xE7 = 7 Bytes
        h = self.bus.read_i2c_block_data(self.adr, REG_CALIB_H, 7)
        self.dig_H2 = s16_le(h, 0)    # 0xE1/0xE2 signed
        self.dig_H3 = h[2]            # 0xE3 unsigned char
        # H4 und H5 sind 12-Bit-Werte, die sich das Byte 0xE5 teilen:
        #   H4 = 0xE4 [11:4]  +  untere 4 Bit von 0xE5 [3:0]
        #   H5 = 0xE6 [11:4]  +  obere  4 Bit von 0xE5 [3:0]
        self.dig_H4 = s12((h[3] << 4) | (h[4] & 0x0F))
        self.dig_H5 = s12((h[5] << 4) | (h[4] >> 4))
        self.dig_H6 = s8(h[6])        # 0xE7 signed char

    # -----------------------------------------------------------------------
    # Messung
    # -----------------------------------------------------------------------
    def messen(self):
        """Löst eine Einzelmessung aus und gibt (temp_C, druck_hPa, feuchte_%)
        zurück. Beim BMP280 ist feuchte_% = None."""

        # ctrl_meas (0xF4) Bit-Aufbau:  osrs_t[7:5] osrs_p[4:2] mode[1:0]
        #   osrs_t = 001 -> Temperatur 1-fach abtasten
        #   osrs_p = 001 -> Druck 1-fach abtasten
        #   mode   = 01  -> Forced Mode: eine Messung, danach Sleep
        # Das ist der vom Datenblatt empfohlene Modus für Wetterstationen
        # (geringer Stromverbrauch, kaum Eigenerwärmung). Im Normal Mode
        # (mode = 11) würde der Sensor dagegen ununterbrochen messen und
        # sich dabei messbar selbst aufheizen.
        self.bus.write_byte_data(self.adr, REG_CTRL_MEAS, 0b001_001_01)

        # Warten, bis die Messung fertig ist (Status-Bit 3 = "measuring").
        # Bei 1-fach-Oversampling dauert das max. ca. 10 ms.
        time.sleep(0.010)
        while self.bus.read_byte_data(self.adr, REG_STATUS) & 0x08:
            time.sleep(0.002)

        # Alle Messwerte in EINEM Burst-Read lesen. Das ist wichtig: Der Sensor
        # garantiert nur so, dass alle Werte zur selben Messung gehören.
        anzahl = 8 if self.hat_feuchte else 6
        d = self.bus.read_i2c_block_data(self.adr, REG_DATEN, anzahl)

        # Druck und Temperatur sind 20-Bit-Werte, verteilt auf 3 Bytes:
        #   MSB [19:12], LSB [11:4], XLSB [3:0] (nur obere 4 Bit gültig)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)

        # Reihenfolge ist wichtig: Temperatur zuerst, weil sie t_fine setzt!
        temperatur = self._kompensiere_temperatur(adc_t)
        druck = self._kompensiere_druck(adc_p)

        feuchte = None
        if self.hat_feuchte:
            # Feuchte ist ein 16-Bit-Wert: MSB, LSB
            adc_h = (d[6] << 8) | d[7]
            feuchte = self._kompensiere_feuchte(adc_h)

        return temperatur, druck / 100.0, feuchte   # Pa -> hPa

    # -----------------------------------------------------------------------
    # Kompensationsformeln (Fließkomma-Variante aus dem Bosch-Datenblatt)
    # Die Zahlen wie 16384.0 oder 524288.0 sind Zweierpotenzen, die Bosch
    # für die Festkomma-Skalierung der Kalibrierwerte gewählt hat.
    # -----------------------------------------------------------------------
    def _kompensiere_temperatur(self, adc_t):
        """Rohwert -> Temperatur in °C. Setzt außerdem self.t_fine."""
        var1 = (adc_t / 16384.0 - self.dig_T1 / 1024.0) * self.dig_T2
        var2 = (adc_t / 131072.0 - self.dig_T1 / 8192.0) ** 2 * self.dig_T3
        self.t_fine = var1 + var2
        return self.t_fine / 5120.0

    def _kompensiere_druck(self, adc_p):
        """Rohwert -> Druck in Pascal (benötigt t_fine)."""
        var1 = self.t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * self.dig_P6 / 32768.0
        var2 = var2 + var1 * self.dig_P5 * 2.0
        var2 = var2 / 4.0 + self.dig_P4 * 65536.0
        var1 = (self.dig_P3 * var1 * var1 / 524288.0
                + self.dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self.dig_P1
        if var1 == 0:
            return 0.0  # Division durch null vermeiden
        p = 1048576.0 - adc_p
        p = (p - var2 / 4096.0) * 6250.0 / var1
        var1 = self.dig_P9 * p * p / 2147483648.0
        var2 = p * self.dig_P8 / 32768.0
        return p + (var1 + var2 + self.dig_P7) / 16.0

    def _kompensiere_feuchte(self, adc_h):
        """Rohwert -> relative Luftfeuchtigkeit in % (benötigt t_fine)."""
        h = self.t_fine - 76800.0
        h = ((adc_h - (self.dig_H4 * 64.0 + self.dig_H5 / 16384.0 * h))
             * (self.dig_H2 / 65536.0
                * (1.0 + self.dig_H6 / 67108864.0 * h
                   * (1.0 + self.dig_H3 / 67108864.0 * h))))
        h = h * (1.0 - self.dig_H1 * h / 524288.0)
        # Physikalisch sinnvollen Bereich erzwingen
        return max(0.0, min(100.0, h))


# ---------------------------------------------------------------------------
# OLED-Anzeige
# ---------------------------------------------------------------------------
class Anzeige:
    """Zeigt die Messwerte auf einem SH1106-OLED (128x64) an.

    Ist kein Display angeschlossen (oder luma.oled nicht installiert), ist
    self.device None und anzeigen() tut einfach nichts.
    """

    def __init__(self, bus_nr, adresse):
        self.device = None
        if sh1106 is None:
            print("Hinweis: luma.oled nicht installiert – nur Konsolenausgabe.")
            return
        try:
            serial = luma_i2c(port=bus_nr, address=adresse)
            self.device = sh1106(serial, width=128, height=64)
        except Exception as e:  # z. B. DeviceNotFoundError, OSError
            print(f"Hinweis: Kein Display an 0x{adresse:02X} ({e}) – "
                  "nur Konsolenausgabe.")
            return
        try:
            self.schrift = ImageFont.truetype(OLED_SCHRIFT, 15)
        except OSError:
            self.schrift = ImageFont.load_default()
        print(f"SH1106-Display gefunden an Adresse 0x{adresse:02X}.")

    def anzeigen(self, temp, druck, feuchte):
        if self.device is None:
            return
        zeilen = [f"{temp:5.1f} °C", f"{druck:6.1f} hPa"]
        if feuchte is not None:
            zeilen.append(f"{feuchte:5.1f} %rF")
        try:
            # canvas() zeichnet in ein Bild im Speicher und schickt es am
            # Ende des with-Blocks in einem Rutsch ans Display.
            with canvas(self.device) as zeichnung:
                for i, text in enumerate(zeilen):
                    zeichnung.text((4, 2 + i * 21), text,
                                   font=self.schrift, fill="white")
        except OSError as e:
            # Display im Betrieb abgezogen o. Ä.: ab jetzt nur noch Konsole.
            print(f"Hinweis: Display antwortet nicht mehr ({e}) – "
                  "nur noch Konsolenausgabe.")
            self.device = None


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
class MqttSender:
    """Verschickt die Messwerte als JSON an einen MQTT-Broker.

    Verbindungsaufbau und Reconnect laufen in einem Hintergrund-Thread von
    paho-mqtt (loop_start). Ist der Broker nicht erreichbar, wird die
    Messung einfach nicht verschickt – Messung und Display laufen weiter.
    Ist kein Host angegeben (oder paho-mqtt fehlt), tut senden() nichts.
    """

    def __init__(self, host, port):
        self.client = None
        if host is None:
            print("Hinweis: Kein --mqtt-host angegeben – MQTT ist aus.")
            return
        if mqtt is None:
            print("Hinweis: paho-mqtt nicht installiert – MQTT ist aus.")
            return

        self.host, self.port = host, port
        self.topic = MQTT_TOPIC.format(hostname=socket.gethostname())
        # Merkt sich, ob "nicht verbunden" schon gemeldet wurde, damit die
        # Konsole nicht alle 2 Sekunden mit demselben Hinweis zugemüllt wird.
        self.fehler_gemeldet = False
        self.beendet = False   # gewolltes Trennen nicht als Fehler melden

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._bei_verbindung
        self.client.on_disconnect = self._bei_trennung
        self.client.on_connect_fail = self._bei_verbindungsfehler
        # Wartezeit zwischen Reconnect-Versuchen: 1 s, verdoppelt bis max. 30 s
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        # connect_async() blockiert nicht und wirft keine Exception, wenn der
        # Broker fehlt – der Hintergrund-Thread versucht es immer wieder.
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()
        print(f"MQTT: verbinde mit {host}:{port}, Topic '{self.topic}'.")

    def _bei_verbindung(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._melde(f"Broker lehnt Verbindung ab ({reason_code})")
            return
        # Zeilenumbruch im String statt per print(): Diese Callbacks laufen im
        # MQTT-Thread, so vermischt sich die Zeile nicht mit der Messausgabe.
        print(f"MQTT: verbunden mit {self.host}:{self.port}.\n", end="",
              flush=True)
        self.fehler_gemeldet = False

    def _bei_trennung(self, client, userdata, flags, reason_code, properties):
        if self.beendet:
            return
        self._melde(f"Verbindung getrennt ({reason_code})")

    def _bei_verbindungsfehler(self, client, userdata):
        self._melde(f"Broker {self.host}:{self.port} nicht erreichbar")

    def _melde(self, grund):
        if not self.fehler_gemeldet:
            print(f"Hinweis: MQTT – {grund}. Neuer Versuch läuft im "
                  "Hintergrund, Messung geht weiter.\n", end="", flush=True)
            self.fehler_gemeldet = True

    def senden(self, temp, druck, feuchte):
        if self.client is None or not self.client.is_connected():
            return
        nachricht = {
            # Ortszeit mit Zeitzonen-Offset, z. B. 2026-10-20T15:42:07+02:00
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "temperature_c": round(temp, 2),
            "pressure_hpa": round(druck, 2),
            # Beim BMP280 None -> in JSON null
            "humidity_pct": None if feuchte is None else round(feuchte, 1),
        }
        self.client.publish(self.topic, json.dumps(nachricht), qos=0)

    def beenden(self):
        if self.client is not None:
            self.beendet = True
            self.client.disconnect()
            self.client.loop_stop()


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------
def lies_argumente():
    parser = argparse.ArgumentParser(
        description="BME280/BMP280 auslesen, auf dem OLED anzeigen und "
                    "optional per MQTT verschicken.")
    parser.add_argument("--mqtt-host",
                        help="IP oder Hostname des MQTT-Brokers "
                             "(ohne Angabe ist MQTT aus)")
    parser.add_argument("--mqtt-port", type=int, default=MQTT_PORT,
                        help=f"Port des MQTT-Brokers (Standard: {MQTT_PORT})")
    return parser.parse_args()


def main(args):
    anzeige = Anzeige(I2C_BUS, OLED_ADRESSE)
    sender = MqttSender(args.mqtt_host, args.mqtt_port)
    try:
        messschleife(anzeige, sender)
    finally:
        sender.beenden()


def messschleife(anzeige, sender):
    with SMBus(I2C_BUS) as bus:
        sensor = BME280(bus, I2C_ADRESSE)
        print(f"{sensor.name} gefunden an Bus {I2C_BUS}, "
              f"Adresse 0x{I2C_ADRESSE:02X}. Abbrechen mit Strg+C.\n")

        while True:
            temp, druck, feuchte = sensor.messen()
            zeit = time.strftime("%H:%M:%S")
            zeile = f"{zeit}  Temperatur: {temp:6.2f} °C   Druck: {druck:7.2f} hPa"
            if feuchte is not None:
                zeile += f"   Feuchte: {feuchte:5.1f} %"
            print(zeile, flush=True)
            anzeige.anzeigen(temp, druck, feuchte)
            sender.senden(temp, druck, feuchte)
            time.sleep(INTERVALL_S)


if __name__ == "__main__":
    try:
        main(lies_argumente())
    except KeyboardInterrupt:
        print("\nBeendet.")
