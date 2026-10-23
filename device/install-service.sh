#!/usr/bin/env bash
# install-service.sh – Installiert sensorstation.service und startet ihn.
#
# Aufruf auf dem Pi im Ordner ~/sensor-station:  ./install-service.sh
# Mehrfach ausführbar: aktualisiert die Unit und startet den Dienst neu.
set -euo pipefail

ORDNER="$(cd "$(dirname "$0")" && pwd)"
UNIT="sensorstation.service"
ENV_DATEI="$ORDNER/sensorstation.env"

# Die Unit erwartet feste Pfade unter /home/rashid/sensor-station
if [[ "$ORDNER" != "/home/rashid/sensor-station" ]]; then
    echo "Fehler: Skript muss in /home/rashid/sensor-station liegen (liegt in $ORDNER)." >&2
    exit 1
fi

if [[ ! -x "$ORDNER/venv/bin/python" ]]; then
    echo "venv fehlt – lege sie an und installiere die Abhängigkeiten ..."
    python3 -m venv "$ORDNER/venv"
fi
"$ORDNER/venv/bin/pip" install -q -r "$ORDNER/requirements.txt"

# Konfiguration nur beim ersten Mal aus der Vorlage anlegen, nie überschreiben
if [[ ! -f "$ENV_DATEI" ]]; then
    cp "$ORDNER/sensorstation.env.example" "$ENV_DATEI"
    echo "$ENV_DATEI aus der Vorlage angelegt – Broker-IP bei Bedarf anpassen."
fi

# Eine manuell gestartete sensor.py würde parallel zum Dienst laufen
if pgrep -u "$USER" -f "python.* sensor\.py" > /dev/null \
   && ! systemctl is-active --quiet "$UNIT"; then
    echo "Fehler: sensor.py läuft bereits manuell (PID $(pgrep -u "$USER" -f "python.* sensor\.py" | tr '\n' ' '))." >&2
    echo "Bitte zuerst beenden (Strg+C bzw. kill -INT <PID>)." >&2
    exit 1
fi

sudo install -m 644 "$ORDNER/$UNIT" "/etc/systemd/system/$UNIT"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT"
sudo systemctl restart "$UNIT"

sleep 3
systemctl --no-pager status "$UNIT" | head -n 12
echo
echo "Logs live ansehen:  journalctl -u sensorstation -f"
