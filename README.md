# IoT Sensor Station 🌡️

A Raspberry Pi Zero W that measures **temperature, humidity and air pressure** with a Bosch BME280 and shows the live readings on a small OLED display.
The BME280 driver is written from scratch on top of raw I2C register access, including the sensor's factory calibration.

This is the first stage of a full-stack IoT project: the Pi publishes its readings via MQTT, and a FastAPI backend on the laptop stores them in SQLite and serves them over a REST API. Next comes a web dashboard (see [Roadmap](#-roadmap)).

<p align="center">
  <img src="docs/images/demo.gif" alt="Live readings on the OLED display" width="45%">
  &nbsp;
  <img src="docs/images/station.jpg" alt="Raspberry Pi Zero W with BME280 sensor and OLED on a breadboard" width="45%">
</p>

---

## 📁 Repository layout

```
device/    runs on the Raspberry Pi: BME280 driver, OLED, MQTT publisher
backend/   runs on the laptop: MQTT subscriber, SQLite storage, REST API (FastAPI)
docs/      images for this README
```

---

## ✨ Features

- Reads temperature (°C), pressure (hPa) and relative humidity (%) every 2 seconds
- **Own BME280 driver** using only `smbus2`:
  - detects BME280 vs. BMP280 via the chip ID register
  - reads the per-sensor factory calibration coefficients from the sensor's NVM
  - applies Bosch's compensation formulas to turn raw ADC values into physical units
  - burst-reads all measurement registers at once so values belong to the same sample
- **Forced mode** instead of continuous measuring, to minimise self-heating of the sensor
- Live output on a **1.3" SH1106 OLED** (128×64) via `luma.oled`
- Keeps running with console output only if the display or `luma.oled` is missing
- Publishes every reading as **JSON via MQTT** (`paho-mqtt`), with automatic reconnect if the broker is unreachable

---

## 🔧 Hardware

| Component | Details |
|-----------|---------|
| Raspberry Pi Zero W | Raspberry Pi OS Lite (32-bit), headless via SSH over Wi-Fi |
| BME280 breakout | I2C address `0x76`, chip ID `0x60` |
| 1.3" OLED | SH1106 driver, 128×64, I2C address `0x3C` |
| Breadboard + jumper wires | to share the I2C bus between both devices |

Both devices share **I2C bus 1** (SDA/SCL are joined on the breadboard).

### Wiring

| Signal | Raspberry Pi pin | BME280 | OLED |
|--------|------------------|--------|------|
| 3.3 V  | Pin 1            | VIN    | –    |
| 3.3 V  | Pin 17           | –      | VCC  |
| GND    | Pin 6            | GND    | –    |
| GND    | Pin 9            | –      | GND  |
| SDA    | Pin 3 (GPIO2)    | SDA    | SDA  |
| SCL    | Pin 5 (GPIO3)    | SCL    | SCL  |

Check the I2C bus with `i2cdetect -y 1`. It should show `3c` (OLED) and `76` (BME280).

---

## 🚀 Setup & run (device)

On the Raspberry Pi:

```bash
# Enable I2C (once)
sudo raspi-config nonint do_i2c 0

# Get the code and install dependencies in a virtual environment
git clone https://github.com/rascho2/sensor-station.git
cd sensor-station/device
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Run (display + console only)
venv/bin/python sensor.py

# Run and also publish every reading via MQTT
venv/bin/python sensor.py --mqtt-host 172.20.10.6            # port defaults to 1883
venv/bin/python sensor.py --mqtt-host 172.20.10.6 --mqtt-port 1884
```

### MQTT

With `--mqtt-host`, every reading is published (QoS 0) to the topic `sensorstation/<hostname>/messwerte`, e.g. `sensorstation/sensorpi/messwerte`:

```json
{"timestamp": "2026-10-20T15:42:07+02:00", "temperature_c": 23.41, "pressure_hpa": 1012.37, "humidity_pct": 48.2}
```

`humidity_pct` is `null` on a BMP280. If the broker is unreachable or the connection drops, measuring and the display keep running; the script prints a note once and reconnects automatically in the background. The broker address is a command-line argument because the IP can change (e.g. on a phone hotspot).

To watch the messages on the broker machine:

```bash
mosquitto_sub -h localhost -t 'sensorstation/#' -v
```

Example output:

```
BME280 gefunden an Bus 1, Adresse 0x76. Abbrechen mit Strg+C.

13:45:11  Temperatur:  26.91 °C   Druck: 1010.74 hPa   Feuchte:  54.3 %
13:45:13  Temperatur:  26.90 °C   Druck: 1010.73 hPa   Feuchte:  54.3 %
```

> Run the script with the venv's Python (`venv/bin/python`). With the system `python3`, `luma.oled` is not available and the script falls back to console output only.

---

## 🖥️ Backend

The backend runs on the laptop next to the Mosquitto broker. It subscribes to `sensorstation/+/messwerte`, validates every message, stores it in SQLite and serves the data over a REST API.

### Start

```bash
cd backend
python3 -m venv venv
venv/bin/pip install -r requirements.txt        # requirements-dev.txt for the tests
venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive API docs (Swagger UI): <http://localhost:8000/docs>

Configuration via environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `MQTT_HOST` | `localhost` | MQTT broker address |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `sensorstation/+/messwerte` | subscribed topic (`+` = any device) |
| `DB_PATH` | `backend/data/sensorstation.db` | SQLite file (created automatically) |

```bash
MQTT_HOST=192.168.1.20 DB_PATH=/var/lib/sensorstation.db venv/bin/uvicorn app.main:app
```

### Endpoints

| Method & path | Description |
|---------------|-------------|
| `GET /api/health` | `{"status": "ok" \| "degraded", "database": true, "mqtt_connected": true}` |
| `GET /api/readings/latest` | newest reading, `404` if there is none yet |
| `GET /api/readings?limit=100&since=<ISO 8601>` | readings, newest first; `limit` 1–1000 (default 100), `since` is inclusive and must include a time zone |

```bash
curl localhost:8000/api/readings/latest
# {"device":"sensorpi","timestamp":"2026-10-22T13:47:00Z","temperature_c":25.92,"pressure_hpa":1002.31,"humidity_pct":63.5}

curl -G localhost:8000/api/readings --data-urlencode "since=2026-10-22T15:00:00+02:00"
```

Timestamps are returned in UTC. In a URL, a `+` in the time zone offset must be encoded as `%2B` (`--data-urlencode` does that), or use `Z` for UTC.

### Architecture

```
Raspberry Pi                    Laptop
─────────────                   ──────────────────────────────────────────────────────
device/sensor.py ──MQTT/JSON──▶ Mosquitto ──▶ backend (one Python process)
                                                ├─ MQTT thread:  mqtt_subscriber.py
                                                │                  → messages.py (validate)
                                                │                  → db.py ──write──▶ SQLite
                                                └─ uvicorn:      main.py (FastAPI)
                                                                   → db.py ◀──read── SQLite
                                                                        ▲
                                              curl / dashboard ──HTTP───┘
```

- `app/mqtt_subscriber.py`: a paho-mqtt client running in a background thread. It connects asynchronously, so the API also starts when no broker is running, and it reconnects automatically. It re-subscribes after every reconnect. Every exception in the message callback is caught, because an uncaught one would stop the MQTT thread for good.
- `app/messages.py`: turns topic + raw bytes into a validated `Reading`. The device name comes from the topic. The payload is checked with a strict pydantic model: no strings as numbers, time zone required, values within the BME280's measuring range. Invalid messages are logged and skipped. This module knows nothing about MQTT, which makes it testable without a broker.
- `app/db.py`: plain `sqlite3`. All timestamps are normalised to UTC before storing, so they sort and compare correctly as text. One short-lived connection per operation and WAL mode let the MQTT thread write while API requests read.
- `app/main.py`: FastAPI app factory. The *lifespan* creates the schema and starts/stops the subscriber with the server. Query parameters and responses are validated by FastAPI (`422` on bad input).

### Tests

```bash
cd backend
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest
```

The tests use a temporary SQLite file per test and never need a broker: message parsing is tested directly, the MQTT callbacks are called with fake client/message objects, and the API runs with `start_mqtt=False` through FastAPI's `TestClient`.

---

## 🧠 How the driver works

1. **Identify the chip:** register `0xD0` returns `0x60` for a BME280 and `0x58` for a BMP280 (no humidity).
2. **Soft reset** and wait until the sensor has copied its calibration data from NVM.
3. **Read calibration data** (`0x88–0xA1`, `0xE1–0xE7`): every sensor is calibrated individually at the factory. Some coefficients are signed, some unsigned, and the humidity values H4/H5 are 12-bit numbers that share one byte.
4. **Trigger a measurement** in forced mode (`ctrl_meas = 0x25`) and poll the status register until it is done.
5. **Burst-read** the raw 20-bit temperature/pressure and 16-bit humidity values.
6. **Compensate:** temperature first (it produces `t_fine`), then pressure and humidity, which both depend on temperature.

---

## 🧩 Challenges & lessons learned

- **Cold solder joints:** After soldering the 40-pin header, the sensor was not detected on the I2C bus. Systematic continuity tests with a multimeter showed that even the two 5 V pins, which are connected on the board, had no connection: the solder sat on the pins but had not flowed onto the pads. Reflowing the joints at a higher temperature fixed it.
- **Headless setup:** The Raspberry Pi Imager version from the Ubuntu repositories silently ignored the Wi-Fi/SSH settings for the new cloud-init based OS image. I wrote `user-data`, `network-config` and `meta-data` on the boot partition by hand.
- **Wiring by pin number:** Counting header pins from the wrong end or row is easy. Pin 1 is identified by the square pad on the bottom side.
- **"Display stays black":** The hardware was fine (verified with a minimal test script). The real cause was starting the script with the system Python instead of the venv, which triggered the silent console-only fallback.
- **Self-heating:** In continuous mode the BME280 warms itself up. Forced mode measures only on demand.

---

## 🗺️ Roadmap

- [x] Read BME280 over I2C with an own driver
- [x] Show readings on the OLED
- [x] Publish readings via **MQTT** (Mosquitto broker)
- [x] Backend with **FastAPI** + **SQLite**, REST API
- [ ] Web dashboard with history charts
- [ ] Docker Compose, GitHub Actions CI

---

## 🛠️ Development

Developed with AI-assisted coding (Claude Code); all code was reviewed, tested and debugged on the real hardware.

Author: Rashid Brown ([@rascho2](https://github.com/rascho2))
