#!/usr/bin/env python3
"""
sensor.py – Liest einen BME280 bzw. BMP280 über I2C aus.

Hardware:  Raspberry Pi Zero W, Sensor an I2C-Bus 1, Adresse 0x76
Abhängigkeit: nur smbus2 (sudo apt install python3-smbus2)

Funktionsweise in Kurzform:
  1. Chip-ID lesen       -> BME280 (0x60) oder BMP280 (0x58)?
  2. Kalibrierwerte lesen -> jeder Sensor wird im Werk einzeln vermessen,
                             die Korrekturfaktoren stehen in seinem ROM.
  3. Messung anstoßen    -> "Forced Mode": Sensor misst einmal und schläft dann.
  4. Rohwerte lesen      -> 20-Bit-Werte (Temp/Druck) bzw. 16-Bit (Feuchte).
  5. Kompensieren        -> Rohwerte + Kalibrierwerte = echte physikalische Werte
                             (Formeln aus dem Bosch-Datenblatt, Kapitel 4.2.3 / 8.1).
"""

import time

from smbus2 import SMBus

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
I2C_BUS = 1          # Am Pi liegen GPIO2 (SDA) / GPIO3 (SCL) auf Bus 1
I2C_ADRESSE = 0x76   # 0x76 wenn SDO auf GND, 0x77 wenn SDO auf VCC
INTERVALL_S = 2.0    # Abstand zwischen zwei Messungen in Sekunden

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
        # (geringer Stromverbrauch, kaum Eigenerwärmung).
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
# Hauptprogramm
# ---------------------------------------------------------------------------
def main():
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
            time.sleep(INTERVALL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBeendet.")
