// app.js – Dashboard der Sensorstation. Holt alle 5 Sekunden die Daten von
// der REST-API und aktualisiert Kacheln, Status und Diagramme.
"use strict";

const AKTUALISIERUNG_MS = 5000;
const OFFLINE_NACH_S = 30;       // ältere letzte Messung = Sensor offline

// Zeitraum -> Fensterbreite fürs Aggregieren. Ergibt jeweils 120–290 Punkte:
// genug Details fürs Diagramm, wenig Daten über die Leitung. "raster" ist der
// Abstand der Beschriftungen auf der Zeitachse.
const ZEITRAEUME = {
  "1h":  { sekunden: 3600,  bucket: 30,  raster: 10 * 60 },
  "6h":  { sekunden: 21600, bucket: 120, raster: 60 * 60 },
  "24h": { sekunden: 86400, bucket: 300, raster: 3 * 60 * 60 },
};

const MESSGROESSEN = [
  { feld: "temperature_c", canvas: "chart-temperatur", farbe: "--temperatur", stellen: 1 },
  { feld: "humidity_pct",  canvas: "chart-feuchte",    farbe: "--feuchte",    stellen: 1 },
  { feld: "pressure_hpa",  canvas: "chart-druck",      farbe: "--druck",      stellen: 1 },
];

const $ = (id) => document.getElementById(id);
const zahl = (wert, stellen) =>
  wert == null ? "–" : wert.toLocaleString("de-DE", { minimumFractionDigits: stellen, maximumFractionDigits: stellen });

let zeitraum = gespeicherterZeitraum();
let diagramme = [];
let timer = null;
let runde = 0;        // erkennt veraltete Antworten nach einem Umschalten

// ---------------------------------------------------------------------------
// Daten holen
// ---------------------------------------------------------------------------
async function holeJson(url) {
  const antwort = await fetch(url, { cache: "no-store" });
  if (antwort.status === 404) return null;           // z. B. noch keine Messung
  if (!antwort.ok) throw new Error(`${url}: HTTP ${antwort.status}`);
  return antwort.json();
}

async function aktualisiere() {
  clearTimeout(timer);
  const meineRunde = ++runde;
  try {
    const [health, letzte] = await Promise.all([
      holeJson("/api/health"),
      holeJson("/api/readings/latest"),
    ]);
    if (meineRunde !== runde) return;     // inzwischen neuere Anfrage gestartet
    zeigeAktuelleWerte(letzte);
    zeigeStatus(health, letzte);

    const { sekunden, bucket } = ZEITRAEUME[zeitraum];
    const parameter = new URLSearchParams({
      since: new Date(Date.now() - sekunden * 1000).toISOString(),
      bucket_seconds: bucket,
    });
    if (letzte) parameter.set("device", letzte.device);
    const fenster = await holeJson(`/api/readings/aggregated?${parameter}`);
    if (meineRunde !== runde) return;
    zeigeVerlauf(fenster, bucket);
  } catch (fehler) {
    if (meineRunde !== runde) return;
    console.error(fehler);
    setzeStatus("fehler", "Backend nicht erreichbar",
                "Das Backend antwortet nicht. Die angezeigten Werte sind möglicherweise veraltet.");
  } finally {
    // Nächste Runde erst nach dieser Antwort planen (statt setInterval), damit
    // sich bei langsamer Verbindung keine Anfragen stapeln. Nur die neueste
    // Runde plant weiter, sonst liefen nach einem Umschalten zwei Schleifen.
    if (meineRunde === runde) {
      clearTimeout(timer);
      timer = setTimeout(aktualisiere, AKTUALISIERUNG_MS);
    }
  }
}

// ---------------------------------------------------------------------------
// Kacheln und Status
// ---------------------------------------------------------------------------
function zeigeAktuelleWerte(letzte) {
  $("wert-temperatur").textContent = zahl(letzte?.temperature_c, 1);
  $("wert-feuchte").textContent = zahl(letzte?.humidity_pct, 1);
  $("wert-druck").textContent = zahl(letzte?.pressure_hpa, 1);
  $("geraet").textContent = letzte ? `Gerät: ${letzte.device}` : "Kein Gerät";

  const zeit = $("letzte-messung");
  if (letzte) {
    const t = new Date(letzte.timestamp);
    zeit.dateTime = letzte.timestamp;
    zeit.textContent = `${formatiereZeitpunkt(t)} (${formatiereAlter(t)})`;
  } else {
    zeit.removeAttribute("datetime");
    zeit.textContent = "–";
  }
}

function zeigeStatus(health, letzte) {
  if (!letzte) {
    setzeStatus("warnung", "Noch keine Messwerte",
                "Es wurden noch keine Messwerte empfangen. Läuft sensor.py auf dem Pi?");
    return;
  }
  const t = new Date(letzte.timestamp);
  const alterS = (Date.now() - t) / 1000;
  const seit = `${formatiereZeitpunkt(t)}, ${formatiereAlter(t)}`;

  if (!health.mqtt_connected) {
    setzeStatus("fehler", "Broker getrennt",
                `Das Backend hat keine Verbindung zum MQTT-Broker. Letzte Messung: ${seit}.`);
  } else if (alterS > OFFLINE_NACH_S) {
    setzeStatus("fehler", "Sensor offline", `Sensor offline seit ${seit}.`);
  } else {
    setzeStatus("ok", "Live");
  }
}

function setzeStatus(zustand, kurz, hinweis = "") {
  $("status").dataset.zustand = zustand;
  $("status-text").textContent = kurz;
  $("hinweis").textContent = hinweis;
  $("hinweis").hidden = !hinweis;
  document.body.classList.toggle("veraltet", zustand !== "ok");
}

function formatiereZeitpunkt(t) {
  const heute = t.toDateString() === new Date().toDateString();
  const uhrzeit = t.toLocaleTimeString("de-DE");
  return heute ? uhrzeit
               : `${t.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })}, ${uhrzeit}`;
}

function formatiereAlter(t) {
  // max(0, …): Geht die Uhr des Pi etwas vor, nicht "vor -2 s" anzeigen
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return `vor ${s} s`;
  const min = Math.floor(s / 60);
  if (min < 60) return `vor ${min} min`;
  const h = Math.floor(min / 60);
  if (h < 48) return `vor ${h} h ${min % 60} min`;
  return `vor ${Math.floor(h / 24)} Tagen`;
}

// ---------------------------------------------------------------------------
// Diagramme
// ---------------------------------------------------------------------------
function farbe(variable) {
  return getComputedStyle(document.documentElement).getPropertyValue(variable).trim();
}

function erstelleDiagramme() {
  diagramme = MESSGROESSEN.map((m) => new Chart($(m.canvas), {
    type: "line",
    data: { datasets: [{ data: [], borderWidth: 2, pointRadius: 0, pointHitRadius: 8,
                         tension: 0.3, fill: true, spanGaps: false }] },
    options: {
      animation: false,
      maintainAspectRatio: false,
      parsing: false,                       // Daten kommen schon als {x, y}
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (punkte) => new Date(punkte[0].parsed.x).toLocaleString("de-DE"),
            label: (punkt) => zahl(punkt.parsed.y, m.stellen),
          },
        },
      },
      // Lineare x-Achse mit Millisekunden statt type: "time" – das spart den
      // zusätzlichen Datums-Adapter, den Chart.js für Zeitachsen bräuchte.
      scales: {
        x: { type: "linear",
             afterBuildTicks: (achse) => { achse.ticks = rasterTicks(achse.min, achse.max); },
             ticks: { maxRotation: 0,
                      callback: (ms) => new Date(ms).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) } },
        y: { ticks: { maxTicksLimit: 5,
                      callback: (wert) => wert.toLocaleString("de-DE", { maximumFractionDigits: 1 }) } },
      },
    },
  }));
  wendeFarbenAn();
}

function wendeFarbenAn() {
  Chart.defaults.color = farbe("--text-leise");
  diagramme.forEach((diagramm, i) => {
    const linie = farbe(MESSGROESSEN[i].farbe);
    Object.assign(diagramm.data.datasets[0], {
      borderColor: linie,
      backgroundColor: linie + "22",        // gleiche Farbe, ~13 % deckend
    });
    for (const achse of Object.values(diagramm.options.scales)) {
      achse.grid = { color: farbe("--gitter") };
      achse.border = { color: farbe("--rand") };
      achse.ticks.color = farbe("--text-leise");
    }
    diagramm.update("none");
  });
}

function zeigeVerlauf(fenster, bucket) {
  const jetzt = Date.now();
  const { sekunden } = ZEITRAEUME[zeitraum];
  $("leer").hidden = fenster.length > 0;
  diagramme.forEach((diagramm, i) => {
    diagramm.data.datasets[0].data = mitLuecken(fenster, MESSGROESSEN[i].feld, bucket);
    // Feste Achse über den ganzen Zeitraum: So sieht man auch, wenn die
    // letzten Minuten fehlen, statt dass das Diagramm sich streckt.
    diagramm.options.scales.x.min = jetzt - sekunden * 1000;
    diagramm.options.scales.x.max = jetzt;
    diagramm.update("none");
  });
}

// Beschriftungen der Zeitachse auf runden Ortszeiten (16:00, 16:10 … bzw.
// 15:00, 18:00 …). Chart.js würde sie sonst ab "jetzt − Zeitraum" verteilen,
// also bei krummen Uhrzeiten wie 16:47, 19:47 …
function rasterTicks(min, max) {
  const schritt = ZEITRAEUME[zeitraum].raster * 1000;
  const versatz = new Date(min).getTimezoneOffset() * 60000;   // UTC+2 -> −2 h
  const ticks = [];
  for (let t = Math.ceil((min - versatz) / schritt) * schritt + versatz; t <= max; t += schritt) {
    ticks.push({ value: t });
  }
  return ticks;
}

// Fehlen Fenster (Sensor war aus), einen null-Punkt einfügen. Mit
// spanGaps: false unterbricht Chart.js dort die Linie, statt die Lücke mit
// einer geraden Linie zu überbrücken.
function mitLuecken(fenster, feld, bucket) {
  const punkte = [];
  let vorher = null;
  for (const f of fenster) {
    const x = new Date(f.bucket_start).getTime();
    if (vorher !== null && x - vorher > 1.5 * bucket * 1000) {
      punkte.push({ x: vorher + bucket * 1000, y: null });
    }
    punkte.push({ x, y: f[feld] });
    vorher = x;
  }
  return punkte;
}

// ---------------------------------------------------------------------------
// Zeitraum-Umschalter
// ---------------------------------------------------------------------------
function gespeicherterZeitraum() {
  try {
    const wert = localStorage.getItem("zeitraum");
    if (wert in ZEITRAEUME) return wert;
  } catch { /* privater Modus o. Ä.: Standard nehmen */ }
  return "1h";
}

function waehleZeitraum(neu) {
  zeitraum = neu;
  try { localStorage.setItem("zeitraum", neu); } catch { /* egal */ }
  document.querySelectorAll("[data-zeitraum]").forEach((knopf) =>
    knopf.setAttribute("aria-pressed", String(knopf.dataset.zeitraum === neu)));
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
document.querySelectorAll("[data-zeitraum]").forEach((knopf) =>
  knopf.addEventListener("click", () => { waehleZeitraum(knopf.dataset.zeitraum); aktualisiere(); }));

// Dark Mode live umschalten: Diagrammfarben neu aus den CSS-Variablen lesen
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", wendeFarbenAn);

// Zurück im Tab: sofort aktualisieren statt bis zu 5 s alte Werte zu zeigen
document.addEventListener("visibilitychange", () => { if (!document.hidden) aktualisiere(); });

waehleZeitraum(zeitraum);
if (window.Chart) {
  erstelleDiagramme();
} else {
  // CDN nicht erreichbar (offline): Kacheln und Status funktionieren trotzdem
  document.querySelector(".diagramme").textContent =
    "Diagramme nicht verfügbar: Chart.js konnte nicht geladen werden (keine Internetverbindung?).";
}
aktualisiere();
