#!/usr/bin/env python3
"""
Sistema FX con análisis FUNDAMENTAL (sin indicadores técnicos).

Sesgo de cada par = diferencia de fuerza entre sus dos divisas, a partir de:
  1. Datos por venir   (calendario de ForexFactory: pronóstico vs previo; hoy/mañana pesan x2.5)
  2. Datos publicados  (sorpresas dato real vs pronóstico, con decaimiento en el tiempo)
  3. Mercado           (VIX, bonos del Tesoro de EE. UU., dólar DXY y petróleo)
  4. Geopolítica       (titulares de noticias, traducidos al español)

Modos:
  python fx_system.py weekly    -> plan semanal (domingo 7:30 pm Colombia)
  python fx_system.py outlook   -> plan diario para el día siguiente (lun-jue 7:30 pm)
  python fx_system.py alerts    -> revisa cada 5 min (en silencio): aviso 5-30 min ANTES de cada evento y ~5 min DESPUÉS,
                                   dirección actualizada de la divisa y los pares según el dato real
  python fx_system.py dato AUD "Cash Rate" 4.60  -> igual que lo anterior, con el dato real escrito a mano

Variables opcionales: FORCE=1 (probar en fin de semana), PAIRS, FOCUS_PAIRS, MAX_SETUPS.
Solo usa la librería estándar (Python 3.9+).
"""
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("REPORT_TZ", "America/Bogota"))
FEED_BASE = "https://nfs.faireconomy.media/"
FEEDS = {"this": FEED_BASE + "ff_calendar_thisweek.json",
         "next": FEED_BASE + "ff_calendar_nextweek.json"}
OUT_DIR = os.getenv("REPORT_DIR", "reports")
STATE_FILE = os.path.join(OUT_DIR, "state.json")
HISTORY_FILE = os.path.join(OUT_DIR, "history.json")
RESULTS_FILE = os.path.join(OUT_DIR, "results.json")
CSV_FILE = os.path.join(OUT_DIR, "resultados.csv")
ALERTS_FILE = os.path.join(OUT_DIR, "alerts_sent.json")
LIVE_FILE = os.path.join(OUT_DIR, "state_live.json")      # estado actualizado tras cada dato publicado
# Fuente OPCIONAL del dato real (el calendario gratuito no lo trae). Plantilla con {desde}, {hasta}, {clave}
ACTUALS_URL = os.getenv("ACTUALS_URL", "").strip()
ACTUALS_KEY = os.getenv("ACTUALS_KEY", "").strip()
CTRY2CUR = {"US": "USD", "EU": "EUR", "EMU": "EUR", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR",
            "GB": "GBP", "UK": "GBP", "JP": "JPY", "AU": "AUD", "NZ": "NZD", "CA": "CAD", "CH": "CHF"}
UA = {"User-Agent": "Mozilla/5.0"}
FF_BASE = "https://www.forexfactory.com/calendar"
UA_FULL = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36"),
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "identity",
           "Referer": "https://www.forexfactory.com/"}
FF_KEY = "ForexFactory (dato real)"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
FORCE = os.getenv("FORCE") == "1"

PAIRS = [p.strip().upper() for p in os.getenv(
    "PAIRS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD,GBPAUD,GBPJPY,EURJPY,EURGBP,AUDJPY"
).split(",") if p.strip()]
FOCUS = [p.strip().upper() for p in os.getenv("FOCUS_PAIRS", "").split(",") if p.strip()]
MAX_SETUPS = int(os.getenv("MAX_SETUPS", "3"))
TIME_MULT = 2.5        # peso extra de los eventos de hoy / del día siguiente
HALF_LIFE_DAYS = 21    # las sorpresas pasadas pierden la mitad de su peso cada 21 días

W = {"High": 3, "Medium": 2, "Low": 1}
ES = {"High": "🔴 ALTA", "Medium": "🟠 MEDIA", "Low": "🟡 BAJA"}
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
DIR_TXT = {1: "ALCISTA", -1: "BAJISTA", 0: "SIN DIRECCIÓN"}
COMP_NAME = {"up": "datos por venir", "past": "datos publicados",
             "mkt": "mercado (VIX/bonos/petróleo)", "geo": "geopolítica"}
STATUS = {}            # estado de las fuentes, se muestra al final del plan

# ---------- sensibilidad de cada divisa (heurística) ----------
# Aversión al riesgo (VIX alto / titulares de tensión): refugios suben, divisas de riesgo bajan
TILT = {"JPY": 1.0, "CHF": 1.0, "USD": 0.5, "EUR": -0.25, "GBP": -0.5,
        "CAD": -0.5, "AUD": -1.0, "NZD": -1.0}
# Rendimientos del Tesoro de EE. UU. al alza: favorecen al USD y debilitan sobre todo al JPY
YIELD_TILT = {"USD": 1.0, "JPY": -0.75, "CHF": -0.5, "EUR": -0.3, "GBP": -0.1,
              "AUD": -0.25, "NZD": -0.25, "CAD": -0.1}
# Petróleo al alza: favorece al CAD y perjudica a importadores como JPY y EUR
OIL_TILT = {"CAD": 1.0, "USD": 0.25, "AUD": 0.25, "JPY": -0.5, "EUR": -0.25,
            "GBP": -0.1, "CHF": -0.1, "NZD": 0.0}
CURS = list(TILT)

# (palabras clave, nombre, por qué importa, invertido, es_clave, nota)
# "invertido" = un valor MÁS ALTO es malo para la divisa (desempleo, subsidios).
# El orden importa: gana la primera coincidencia (lo específico va primero).
PLAYBOOK = [
    (["trimmed mean"], "Inflación subyacente (media recortada)",
     "Inflación sin precios volátiles. Es la que más pesa en las decisiones del banco central de Australia.",
     False, True, "Dato alto = más alzas de tasas esperadas."),
    (["fomc statement", "federal funds rate", "fomc press"], "Decisión de la Fed",
     "La Fed mueve al dólar y a todo el mercado.", False, True,
     "Manda el tono y las proyecciones, no solo la tasa."),
    (["cash rate", "official bank rate", "bank rate", "main refinancing", "rate statement",
      "policy rate", "monetary policy statement", "monetary policy assessment",
      "official cash rate", "boj policy"], "Decisión de tasas",
     "Define el rendimiento de la divisa.", False, True,
     "Si la decisión ya estaba descontada (>85%), manda el tono de la conferencia."),
    (["speaks", "testifies", "press conference"], "Discurso de banco central",
     "Puede cambiar las expectativas de tasas.", False, False,
     "Busca pistas sobre próximas subidas o bajadas. Tono duro = divisa sube."),
    (["non-farm", "nfp"], "Empleo (NFP)",
     "Termómetro del mercado laboral de EE. UU.; mueve al USD y al oro.", False, True,
     "Mira también salarios y revisiones del mes anterior."),
    (["core pce", "pce price"], "Inflación PCE",
     "Inflación que prefiere la Fed.", False, True, "Alto = Fed más dura = USD sube."),
    (["core cpi"], "Inflación subyacente (IPC core)",
     "Inflación sin alimentos ni energía.", False, True, "Alto = más alzas esperadas."),
    (["cpi"], "Inflación (IPC)",
     "Inflación general. Mueve las expectativas de tasas.", False, True,
     "Si hay dato core/subyacente, ese pesa más que el general."),
    (["ppi"], "Precios al productor",
     "Adelanta la inflación al consumidor.", False, False, ""),
    (["gdp"], "PIB",
     "Crecimiento de la economía.", False, True, "Revisa si es preliminar o final."),
    (["unemployment claims", "jobless", "claimant"], "Subsidios de desempleo",
     "Ritmo de despidos. Más solicitudes = economía débil.", True, False, ""),
    (["unemployment rate"], "Tasa de desempleo",
     "Salud del empleo. Más desempleo = divisa débil.", True, True, ""),
    (["employment change", "employment"], "Cambio en empleo",
     "Empleos creados en el mes.", False, True, "Junto a la tasa de desempleo forma el cuadro completo."),
    (["retail sales"], "Ventas minoristas",
     "Consumo, motor de la economía.", False, True, ""),
    (["pmi", "ism"], "PMI / ISM",
     "Actividad empresarial. Sobre 50 = expansión.", False, False, "El 50 es la línea clave."),
    (["trade balance"], "Balanza comercial",
     "Exportaciones menos importaciones.", False, False, ""),
    (["consumer confidence", "sentiment"], "Confianza / sentimiento",
     "Anticipa el gasto.", False, False, ""),
]

# Categoría y multiplicador de peso: lo que dirige a una divisa pesa más
CATS = {
    "Decisión de tasas": ("Tasas de interés", 3.0),
    "Decisión de la Fed": ("Tasas de interés", 3.0),
    "Discurso de banco central": ("Tasas de interés", 1.0),
    "Inflación subyacente (media recortada)": ("Inflación", 2.5),
    "Inflación subyacente (IPC core)": ("Inflación", 2.5),
    "Inflación (IPC)": ("Inflación", 2.5),
    "Inflación PCE": ("Inflación", 2.5),
    "Precios al productor": ("Inflación", 1.0),
    "Empleo (NFP)": ("Empleo / nóminas", 2.5),
    "Cambio en empleo": ("Empleo", 2.0),
    "Tasa de desempleo": ("Desempleo", 2.0),
    "Subsidios de desempleo": ("Desempleo", 1.0),
    "PIB": ("Crecimiento", 1.5),
    "Ventas minoristas": ("Consumo", 1.5),
    "PMI / ISM": ("Actividad", 1.0),
    "Balanza comercial": ("Comercio", 0.75),
    "Confianza / sentimiento": ("Sentimiento", 0.75),
}


# ---------- utilidades ----------
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def num(s):
    if s in (None, ""):
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(s))
    return float(m.group().replace(",", ".")) if m else None


def match_rule(title):
    t = title.lower()
    for rule in PLAYBOOK:
        if any(k in t for k in rule[0]):
            return rule
    return None


def relevant(ev):
    if ev["impact"] == "High":
        return True
    return ev["impact"] == "Medium" and ev["_rule"] is not None and ev["_rule"][4]


def inv(ev):
    return bool(ev["_rule"] and ev["_rule"][3])


def cat_of(ev):
    return CATS.get(ev["_rule"][1], ("Otros", 1.0)) if ev["_rule"] else ("Otros", 1.0)


def weight(ev):
    base = W[ev["impact"]] + (1 if ev["_rule"] and ev["_rule"][4] else 0)
    return base * cat_of(ev)[1]


def ev_name(ev):
    return ev["_rule"][1] if ev["_rule"] else ev["title"]


def phrase(ev, higher):
    """higher=True si el dato sale MAYOR que el pronóstico."""
    strong = (higher != inv(ev))
    return f"{ev['country']} se FORTALECE" if strong else f"{ev['country']} se DEBILITA"


def expected_bias(ev):
    """Expectativa: pronóstico vs dato anterior (+1 mejora, -1 deterioro)."""
    f, p = num(ev.get("forecast")), num(ev.get("previous"))
    if f is None or p is None or f == p:
        return 0
    sign = 1 if f > p else -1
    return -sign if inv(ev) else sign


def surprise(ev):
    """Sorpresa real: +1 si el dato es favorable a la divisa, -1 si es desfavorable, 0 si coincide."""
    a, f = num(ev.get("actual")), num(ev.get("forecast"))
    if a is None or f is None or a == f:
        return 0
    return 1 if (a > f) != inv(ev) else -1


def release_read(ev):
    a, f = num(ev.get("actual")), num(ev.get("forecast"))
    if a is None or f is None:
        return f"Actual: {ev.get('actual')} (no comparable con el pronóstico)"
    if a == f:
        return f"Actual {ev['actual']} = pronóstico → reacción débil"
    return f"Actual {ev['actual']} vs pronóstico {ev.get('forecast')} → {phrase(ev, a > f)}"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ---------- calendario de ForexFactory ----------
def fetch_json(url, tries=3, wait=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # límite de peticiones o red
            last = e
            if i < tries - 1:
                time.sleep(wait)
    print(f"No se pudo descargar {url}: {last}", file=sys.stderr)
    return None


def load_events(which=("this",), required=True):
    raw = []
    for w in which:
        data = fetch_json(FEEDS[w])
        if data is None and w == "this":
            STATUS["calendario"] = "❌ no se pudo descargar"
            if required:
                sys.exit(1)
        raw += data or []
    out, seen = [], set()
    for e in raw:
        if e.get("impact") not in W:
            continue
        key = (e.get("title"), e.get("country"), e.get("date"))
        if key in seen:
            continue
        seen.add(key)
        try:
            e["_dt"] = datetime.fromisoformat(e["date"]).astimezone(TZ)
        except (KeyError, ValueError):
            continue
        e["_rule"] = match_rule(e["title"])
        if relevant(e):
            out.append(e)
    STATUS["calendario"] = f"✅ {len(out)} eventos relevantes"
    return sorted(out, key=lambda e: e["_dt"])


# ---------- ForexFactory (página web): dato real publicado ----------
def ff_fetch(query):
    """Descarga una página del calendario de ForexFactory. Si Cloudflare la bloquea, reintenta con
    'cloudscraper' (si está instalado). Devuelve el HTML o None."""
    url = f"{FF_BASE}?{query}"
    reason = ""
    try:
        req = urllib.request.Request(url, headers=UA_FULL)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        reason = f"HTTP {e.code}"
    except Exception as e:
        reason = str(e)[:80]
    try:
        import cloudscraper
        r = cloudscraper.create_scraper().get(url, timeout=40)
        if r.status_code == 200:
            return r.text
        reason += f" / cloudscraper HTTP {r.status_code}"
    except ImportError:
        reason += " (cloudscraper no instalado)"
    except Exception as e:
        reason += f" / cloudscraper: {str(e)[:60]}"
    STATUS[FF_KEY] = f"❌ bloqueado o sin acceso ({reason.strip()})"
    return None


def _walk(o, out):
    if isinstance(o, dict):
        if "currency" in o and "actual" in o and (o.get("name") or o.get("prefixedName")):
            out.append(o)
        for v in o.values():
            _walk(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out)


def _json_blocks(html):
    """Objetos JSON que la página incrusta como 'calendarComponentStates[n] = {...}'."""
    blocks = []
    for m in re.finditer(r"calendarComponentStates\[\d+\]\s*=\s*", html):
        i = m.end()
        if i >= len(html) or html[i] != "{":
            continue
        depth, in_str, esc = 0, False, False
        for j in range(i, len(html)):
            ch = html[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        blocks.append(json.loads(html[i:j + 1]))
                    except Exception:
                        pass
                    break
    return blocks


class _FFRows(HTMLParser):
    """Plan B: lee las filas de la tabla HTML (clases calendar__*)."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.date_txt = [], None, None, ""

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        if tag == "tr" and "calendar__row" in cls:
            self.row = {"cells": {}, "impact": None}
        elif self.row is not None and tag == "td":
            for k in ("date", "currency", "event", "actual", "forecast", "previous"):
                if f"calendar__{k}" in cls:
                    self.cell = k
                    self.row["cells"].setdefault(k, "")
                    break
        elif self.row is not None and tag == "span":
            if "impact-red" in cls:
                self.row["impact"] = "High"
            elif "impact-ora" in cls:
                self.row["impact"] = "Medium"
            elif "impact-yel" in cls:
                self.row["impact"] = "Low"

    def handle_data(self, data):
        if self.row is not None and self.cell:
            self.row["cells"][self.cell] += data

    def handle_endtag(self, tag):
        if tag == "td":
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def ff_parse(html, now):
    """Eventos de una página de ForexFactory: [{cur, title, dt|None, day|None, actual, forecast, previous, impact}]."""
    out = []
    raw = []
    for blk in _json_blocks(html):
        _walk(blk, raw)
    for o in raw:
        try:
            dl = o.get("dateline")
            dt = datetime.fromtimestamp(int(dl), TZ) if dl else None
            imp = {"high": "High", "medium": "Medium", "low": "Low"}.get(str(o.get("impactName", "")).lower())
            out.append({"cur": str(o.get("currency", "")).upper(),
                        "title": (o.get("name") or o.get("prefixedName") or "").strip(), "dt": dt,
                        "day": dt.date() if dt else None, "impact": imp,
                        "actual": str(o.get("actual") or "").strip(),
                        "forecast": str(o.get("forecast") or "").strip(),
                        "previous": str(o.get("previous") or "").strip()})
        except Exception:
            continue
    if out:
        return out
    p = _FFRows()
    try:
        p.feed(html)
    except Exception:
        return []
    day = None
    for r in p.rows:
        c = {k: re.sub(r"\s+", " ", v).strip() for k, v in r["cells"].items()}
        m = re.search(r"([A-Za-z]{3})\s*(\d{1,2})\s*$", c.get("date", ""))
        if m and m.group(1).lower() in MONTHS:
            mo, dd = MONTHS.index(m.group(1).lower()) + 1, int(m.group(2))
            yr = now.year - (1 if mo > now.month + 6 else 0)
            try:
                day = datetime(yr, mo, dd).date()
            except ValueError:
                pass
        if c.get("currency") and c.get("event"):
            out.append({"cur": c["currency"].upper(), "title": c["event"], "dt": None, "day": day,
                        "impact": r["impact"], "actual": c.get("actual", ""),
                        "forecast": c.get("forecast", ""), "previous": c.get("previous", "")})
    return out


def ff_items(queries, now):
    """Prueba varias URL equivalentes hasta que una devuelva eventos."""
    for q in queries:
        html = ff_fetch(q)
        if not html:
            continue
        items = ff_parse(html, now)
        if items:
            return items
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "ff_debug.txt"), "w", encoding="utf-8") as f:
            f.write(f"consulta={q} longitud={len(html)}\n{html[:3000]}")
        STATUS[FF_KEY] = "⚠️ página recibida pero no se pudo leer (revisa reports/ff_debug.txt)"
    return []


def ff_week_queries(now):
    mon = now - timedelta(days=(now.weekday() + 1) % 7)   # la semana de ForexFactory empieza el domingo
    return ["week=this", f"week={MONTHS[mon.month - 1]}{mon.day}.{mon.year}"]


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def web_actuals(now):
    """Datos reales de esta semana según la página de ForexFactory."""
    items = ff_items(ff_week_queries(now), now)
    got = sum(1 for i in items if i["actual"])
    if items:
        STATUS[FF_KEY] = f"✅ {got} datos con valor real de {len(items)} eventos"
    return items


def ff_backfill(hist, now):
    """Completa el historial con los datos reales de las últimas semanas (páginas mensuales).
    Así la fuerza de cada divisa incluye lo que ya salió desde el primer día."""
    prev_m = (now.replace(day=1) - timedelta(days=1))
    items = []
    for q in (f"month={MONTHS[prev_m.month - 1]}.{prev_m.year}", f"month={MONTHS[now.month - 1]}.{now.year}"):
        items += ff_items([q], now)
    fmt_d = "%Y-%m-%dT%H:%M"
    added = 0
    for w in items:
        if w["cur"] not in CURS or not w["title"]:
            continue
        rule = match_rule(w["title"])
        imp = w["impact"] or ("Medium" if rule and rule[4] else None)
        ev = {"title": w["title"], "country": w["cur"], "impact": imp or "Low", "_rule": rule}
        if not imp or not relevant(ev):
            continue
        rel = hist.setdefault(f"{w['cur']}|{w['title']}", {})
        if w["dt"]:
            ent = rel.setdefault(w["dt"].strftime(fmt_d), {"f": w["forecast"], "p": w["previous"], "a": "", "i": imp})
        else:   # sin hora exacta: se une a un lanzamiento ya guardado del mismo día
            ent = next((v for d, v in rel.items() if w["day"] and d.startswith(w["day"].isoformat())), None)
            if ent is None:
                continue
        if w["forecast"] and not ent.get("f"):
            ent["f"] = w["forecast"]
        if w["actual"]:
            if ent.get("a") != w["actual"]:
                added += 1
            ent["a"] = w["actual"]
    if items:
        STATUS[FF_KEY] = f"✅ historial: {sum(1 for i in items if i['actual'])} datos reales leídos ({added} nuevos)"
    return hist


# ---------- historial de lanzamientos y sorpresas ----------
def update_history(events):
    """Guarda cada lanzamiento (pronóstico, previo y, si existe, el dato real)."""
    hist = load_json(HISTORY_FILE, {})
    before = json.dumps(hist, sort_keys=True)
    cutoff = (datetime.now(TZ) - timedelta(days=150)).strftime("%Y-%m-%dT%H:%M")
    for e in events:
        rel = hist.setdefault(f"{e['country']}|{e['title']}", {})
        d = e["_dt"].strftime("%Y-%m-%dT%H:%M")
        new = {"f": e.get("forecast") or "", "p": e.get("previous") or "",
               "a": e.get("actual") or "", "i": e["impact"]}
        old = rel.get(d)
        if old and old.get("a") and not new["a"]:
            new["a"] = old["a"]
        rel[d] = new
    for k in list(hist):
        hist[k] = {d: v for d, v in hist[k].items() if d >= cutoff}
        if not hist[k]:
            del hist[k]
    return hist, json.dumps(hist, sort_keys=True) != before


def history_surprises(hist, now):
    """Datos ya publicados (últimos 60 días). Cada uno aporta dos señales:
    - sorpresa: real vs pronóstico; - tendencia: real vs dato previo (mejora o deterioro).
    Si el dato real no está guardado, se infiere del 'previo' del lanzamiento siguiente."""
    out = []
    for key, rel in hist.items():
        country, title = key.split("|", 1)
        rule = match_rule(title)
        dates = sorted(rel)
        for i, d in enumerate(dates):
            r = rel[d]
            dt = datetime.strptime(d, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
            age = (now - dt).total_seconds() / 86400
            if age < 0 or age > 60:
                continue
            actual, how = r.get("a"), "real"
            if not actual and i + 1 < len(dates):
                actual, how = rel[dates[i + 1]].get("p"), "inferido"
            a, f, p = num(actual), num(r.get("f")), num(r.get("p"))
            ev = {"title": title, "country": country, "impact": r.get("i", "High"), "_rule": rule}
            sur = 0 if (a is None or f is None or a == f) else (1 if (a > f) != inv(ev) else -1)
            trend = 0 if (a is None or p is None or a == p) else (1 if (a > p) != inv(ev) else -1)
            if not sur and not trend:
                continue
            out.append({"cur": country, "title": rule[1] if rule else title, "w": weight(ev), "sur": sur,
                        "trend": trend, "age": age, "decay": 0.5 ** (age / HALF_LIFE_DAYS),
                        "actual": actual, "forecast": r.get("f") or "", "previous": r.get("p") or "",
                        "how": how, "dt": dt})
    return out


# ---------- mercado: VIX, bonos EE. UU., dólar y petróleo (Yahoo Finance) ----------
MARKET_SYMS = {"VIX": "^VIX", "US3M": "^IRX", "US5Y": "^FVX", "US10Y": "^TNX", "US30Y": "^TYX",
               "DXY": "DX-Y.NYB", "WTI": "CL=F", "BRENT": "BZ=F"}


def yahoo_quote(sym):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) +
           "?range=1mo&interval=1d")
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            if len(closes) < 7:
                return None
            return {"last": closes[-1], "d1": closes[-1] - closes[-2], "p1": closes[-1] / closes[-2] - 1,
                    "d5": closes[-1] - closes[-6], "p5": closes[-1] / closes[-6] - 1}
        except Exception as e:
            print(f"Sin datos de {sym}: {e}", file=sys.stderr)
            time.sleep(3)
    return None


def to_yield(q):
    """Yahoo a veces entrega el rendimiento x10; se normaliza a % y se calculan puntos básicos."""
    if not q:
        return None
    k = 0.1 if q["last"] > 20 else 1.0
    return {"lvl": q["last"] * k, "d1bp": q["d1"] * k * 100, "d5bp": q["d5"] * k * 100}


def market_snapshot():
    q = {k: yahoo_quote(v) for k, v in MARKET_SYMS.items()}
    ok = sum(1 for v in q.values() if v)
    STATUS["mercado"] = f"{'✅' if ok == len(q) else '⚠️'} {ok}/{len(q)} series (VIX, bonos, DXY, petróleo)"
    m = {"q": q, "riskoff": 0.0, "yscore": 0.0, "oscore": 0.0, "dxy": 0.0, "lines": []}
    L = m["lines"]

    v = q["VIX"]
    if v:
        lvl = v["last"]
        base = (-1.5 if lvl < 13 else -1.0 if lvl < 16 else 0.0 if lvl < 20 else 1.0 if lvl < 25
                else 1.5 if lvl < 30 else 2.0)
        m["riskoff"] = clamp(base + (0.5 if v["p1"] > 0.10 else -0.5 if v["p1"] < -0.10 else 0), -2, 2)
        zona = ("calma" if lvl < 16 else "normal" if lvl < 20 else "nerviosismo" if lvl < 25
                else "miedo alto" if lvl < 30 else "pánico")
        efecto = ("aversión al riesgo: favorece USD, JPY y CHF; presiona AUD, NZD y CAD" if m["riskoff"] > 0.5
                  else "apetito por riesgo: favorece AUD, NZD y CAD frente a JPY y CHF" if m["riskoff"] < -0.5
                  else "sin señal clara de riesgo")
        L.append(f"😨 VIX {lvl:.1f} ({v['p1'] * 100:+.1f}% hoy · {v['p5'] * 100:+.1f}% en 5 d) — {zona}: {efecto}.")
    else:
        L.append("😨 VIX: sin datos.")

    ys = {k: to_yield(q[k]) for k in ("US3M", "US5Y", "US10Y", "US30Y")}
    if any(ys.values()):
        parts = [f"{n} {y['lvl']:.2f}% ({y['d1bp']:+.0f} pb)" for n, y in
                 (("3M", ys["US3M"]), ("5A", ys["US5Y"]), ("10A", ys["US10Y"]), ("30A", ys["US30Y"])) if y]
        L.append("🏦 Bonos EE. UU.: " + " · ".join(parts))
        if ys["US10Y"] and ys["US3M"]:
            curva = ys["US10Y"]["lvl"] - ys["US3M"]["lvl"]
            L.append(f"   Curva 10A–3M: {curva:+.2f} pp "
                     f"({'invertida: señal de desaceleración' if curva < 0 else 'normal'}).")
        if ys["US10Y"]:
            t = ys["US10Y"]
            m["yscore"] = clamp(t["d1bp"] / 5 + t["d5bp"] / 15, -2, 2)
            L.append(f"   10A: {t['d5bp']:+.0f} pb en 5 días → "
                     + ("rendimientos al alza: apoya al USD y presiona al JPY" if m["yscore"] > 0.5
                        else "rendimientos a la baja: resta apoyo al USD" if m["yscore"] < -0.5
                        else "sin cambio relevante"))
    else:
        L.append("🏦 Bonos EE. UU.: sin datos.")

    d = q["DXY"]
    if d:
        m["dxy"] = clamp(d["p5"] * 100 / 0.7, -1, 1)
        L.append(f"💵 Índice dólar DXY {d['last']:.2f} ({d['p1'] * 100:+.2f}% hoy · {d['p5'] * 100:+.2f}% en 5 d).")

    w, b = q["WTI"], q["BRENT"]
    if w:
        m["oscore"] = clamp(w["p1"] * 100 / 2 + w["p5"] * 100 / 3, -2, 2)
        txt = f"🛢 Petróleo WTI {w['last']:.2f} ({w['p1'] * 100:+.1f}% hoy · {w['p5'] * 100:+.1f}% en 5 d)"
        if b:
            txt += f" · Brent {b['last']:.2f} ({b['p1'] * 100:+.1f}%)"
        L.append(txt + " → " + ("sube: favorece al CAD y pesa sobre JPY y EUR" if m["oscore"] > 0.5
                                else "baja: resta apoyo al CAD" if m["oscore"] < -0.5
                                else "sin cambio relevante") + ".")
    else:
        L.append("🛢 Petróleo: sin datos.")
    return m


# ---------- geopolítica (titulares) ----------
RSS = ["http://feeds.bbci.co.uk/news/world/rss.xml",
       "http://feeds.bbci.co.uk/news/business/rss.xml",
       "https://www.fxstreet.com/rss/news"]
RISK_OFF = ["war", "attack", "missile", "strike", "escalat", "sanction", "tariff", "nuclear",
            "invasion", "conflict", "crisis", "shutdown", "default", "tension", "threat",
            "bomb", "drone"]
RISK_ON = ["ceasefire", "truce", "peace", "de-escalat", "trade deal"]
THEMES = ["oil", "opec", "election", "inflation", "central bank", "interest rate",
          "fed ", "ecb", "boe", "boj", "rba"]


def has(words, text):
    return any(re.search(r"\b" + re.escape(w), text) for w in words)


def translate_es(text):
    """Traduce al español con Google Translate (gratuito, no oficial). Si falla, deja el original."""
    try:
        url = ("https://translate.googleapis.com/translate_a/single"
               "?client=gtx&sl=auto&tl=es&dt=t&q=" + urllib.parse.quote(text))
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return "".join(seg[0] for seg in data[0] if seg and seg[0]) or text
    except Exception as e:
        print(f"Traducción fallida: {e}", file=sys.stderr)
        return text


def geopolitics(translate=True):
    titles = []
    for url in RSS:
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                root = ET.fromstring(r.read())
            titles += [(i.findtext("title") or "").strip() for i in root.iter("item")]
        except Exception as e:
            print(f"RSS fallido {url}: {e}", file=sys.stderr)
    off = on = 0
    tagged, seen = [], set()
    for t in titles:
        low = t.lower()
        if not t or low in seen:
            continue
        seen.add(low)
        if has(RISK_OFF, low):
            off += 1
            tagged.append(("🔻", t))
        elif has(RISK_ON, low):
            on += 1
            tagged.append(("🟢", t))
        elif has(THEMES, low):
            tagged.append(("📰", t))
    net = off - on
    intensity = 0 if net <= 1 else 1 if net <= 4 else 2
    STATUS["titulares"] = f"{'✅' if titles else '❌'} {len(titles)} titulares leídos"
    return intensity, [(ic, translate_es(t) if translate else t) for ic, t in tagged[:8]]


def geo_text(intensity, heads):
    msg = {0: "Sin señales fuertes de aversión al riesgo en los titulares.",
           1: "Tensión moderada: favorece a JPY, CHF y USD frente a AUD, NZD, CAD y GBP.",
           2: "Aversión al riesgo ALTA: refugios (JPY, CHF, USD) con ventaja; AUD y NZD vulnerables."}[intensity]
    txt = msg
    if heads:
        txt += "\nTitulares (🔻 riesgo, 🟢 distensión, 📰 tema de mercado):\n" + \
               "\n".join(f"{ic} {t}" for ic, t in heads)
    return txt + "\nLectura por palabras clave: revisa tú mismo las noticias antes de operar."


# ---------- modelo de fuerza por divisa ----------
def market_scores(m):
    """Aporte del mercado a cada divisa y su explicación."""
    sc, why = {}, {}
    for c in CURS:
        items = [("aversión al riesgo (VIX)", m["riskoff"] * TILT[c]),
                 ("rendimientos EE. UU.", m["yscore"] * YIELD_TILT[c]),
                 ("petróleo", m["oscore"] * OIL_TILT[c])]
        if c == "USD":
            items.append(("índice dólar DXY", m["dxy"]))
        sc[c] = sum(v for _, v in items)
        why[c] = [(abs(v), f"{n} {v:+.1f}") for n, v in items if abs(v) >= 0.4]
    return sc, why


def currency_model(scope, cutoff, hs, m, geo_int):
    """Fuerza de cada divisa = datos por venir + datos publicados + mercado + geopolítica."""
    U, P = defaultdict(float), defaultdict(float)
    up_w, past_w = defaultdict(list), defaultdict(list)
    for e in scope:
        c = e["country"]
        if c not in CURS:
            continue
        mult = TIME_MULT if e["_dt"] <= cutoff else 1.0
        b = expected_bias(e)
        v = weight(e) * mult * b
        U[c] += v
        arrow = "↑" if b > 0 else "↓" if b < 0 else "→"
        up_w[c].append((abs(v) / 6, f"{ev_name(e)} {DIAS[e['_dt'].weekday()]} {e['_dt']:%H:%M} {arrow}"
                        + (" ⚡" if mult > 1 else "")))
    for s in hs:
        c = s["cur"]
        if c not in CURS:
            continue
        v = s["w"] * s["decay"] * (1.5 * s["sur"] + 0.75 * s.get("trend", 0))
        P[c] += v
        ref = f"vs pron {s['forecast']}" if s["sur"] else f"vs prev {s.get('previous') or '-'}"
        past_w[c].append((abs(v) / 4, f"{s['title']} {s['actual']} {ref} " + ("✅" if v > 0 else "❌")))
    mk, mk_w = market_scores(m)
    model = {}
    for c in CURS:
        comps = {"up": clamp(U[c] / 6, -4, 4), "past": clamp(P[c] / 4, -3, 3),
                 "mkt": clamp(mk[c], -3, 3), "geo": clamp(geo_int * TILT[c], -2, 2)}
        why = {"up": sorted(up_w[c], reverse=True), "past": sorted(past_w[c], reverse=True),
               "mkt": sorted(mk_w[c], reverse=True),
               "geo": [(abs(comps["geo"]), f"titulares de riesgo {comps['geo']:+.1f}")] if abs(comps["geo"]) >= 0.4 else []}
        allr = [(a, t, k) for k in why for a, t in why[k]]
        model[c] = {"comps": comps, "strength": sum(comps.values()), "why": why,
                    "top": sorted(allr, reverse=True)[:3]}
    return model


def strength_tag(s):
    return "🟢 FUERTE" if s >= 4 else "🔴 DÉBIL" if s <= -4 else "⚪ NEUTRAL"


# ---------- evaluación de pares ----------
LIM = {"up": 4, "past": 3, "mkt": 3, "geo": 2}
MINS = {"up": 1, "past": 0.5, "mkt": 0.75, "geo": 0.5}


def evaluate_pair(pair, model):
    b, q = pair[:3], pair[3:]
    comps = {k: clamp(model[b]["comps"][k] - model[q]["comps"][k], -LIM[k], LIM[k]) for k in LIM}
    total = sum(comps.values())
    d = 1 if total >= 2.5 else -1 if total <= -2.5 else 0
    aligned = sum(1 for k in comps if d and comps[k] * d >= MINS[k])
    opposed = any(d and comps[k] * d <= -2 for k in comps)
    fund_ok = d and (comps["up"] + comps["past"]) * d >= 1.5
    grade = None
    if d and fund_ok and not opposed:
        if aligned >= 3 and abs(total) >= 6:
            grade = "A"
        elif aligned >= 2 and abs(total) >= 3.5:
            grade = "B"
    fuerza = ("muy fuerte" if abs(total) >= 7 else "fuerte" if abs(total) >= 5
              else "moderada" if abs(total) >= 2.5 else "débil")
    why = []
    for cur in (b, q):
        tops = " · ".join(t for _, t, _ in model[cur]["top"][:2]) or "sin factores destacados"
        why.append(f"{cur} ({model[cur]['strength']:+.1f}): {tops}")
    return {"pair": pair, "comps": {k: round(v, 1) for k, v in comps.items()}, "total": round(total, 1),
            "d": d, "aligned": aligned, "grade": grade, "fuerza": fuerza,
            "label": {1: "📈 ALCISTA", -1: "📉 BAJISTA", 0: "↔️ SIN DIRECCIÓN"}[d], "why": why}


GRADE_RANK = {"A": 2, "B": 1, None: 0}


def rank_key(r):
    return (-GRADE_RANK[r["grade"]], -abs(r["total"]))


def choose_pairs(results):
    """Solo los pares con dirección más clara; máximo 2 setups por divisa."""
    chosen, used = [], defaultdict(int)
    for r in results:
        if not r["grade"] or len(chosen) >= MAX_SETUPS:
            continue
        b, q = r["pair"][:3], r["pair"][3:]
        if used[b] >= 2 or used[q] >= 2:
            continue
        chosen.append(r)
        used[b] += 1
        used[q] += 1
    for r in results:
        if r["pair"] in FOCUS and r not in chosen:
            chosen.append(r)
    return chosen


# ---------- presentación simple: semáforo, pares y tarjetas por sesión ----------
UTC = ZoneInfo("UTC")
DIAS_LARGO = ["LUNES", "MARTES", "MIÉRCOLES", "JUEVES", "VIERNES", "SÁBADO", "DOMINGO"]
SESIONES = [("ASIÁTICA", "19:00–01:00 COL"), ("EUROPEA", "01:00–07:30 COL"),
            ("NUEVA YORK", "07:30–16:00 COL"), ("CIERRE ESTADOUNIDENSE Y TRANSICIÓN A ASIA", "16:00–19:00 COL")]
RULES = ("⚠️ Reglas: no operes 15 min antes de un dato ⭐/🔶 · espera la vela de 5 min tras el dato · "
         "stop más allá de la vela del dato y objetivo 1:2 · riesgo máximo 1%. "
         "Es información, no asesoría financiera.")
SHORT_TEXT = ""  # versión corta para WhatsApp


def gmt(dt):
    return dt.astimezone(UTC)


def tiempo(dt):
    return f"{dt:%H:%M} COL ({gmt(dt):%H:%M} GMT)"


def gmt_cutoff(now):
    """Fin del día GMT en curso (si ya son las 20:00 GMT o más, el del día siguiente)."""
    u = gmt(now)
    d = u.date() if u.hour < 20 else u.date() + timedelta(days=1)
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=UTC).astimezone(TZ)


def sesion_idx(dt):
    g = gmt(dt)
    h = g.hour + g.minute / 60
    return 0 if h < 6 else 1 if h < 12.5 else 2 if h < 21 else 3


def luz(s):
    return "🟢" if s >= 3 else "🟡" if s >= 0 else "🟠" if s > -3 else "🔴"


def prio(e, top_ids=()):
    if weight(e) >= 8 or (id(e) in top_ids and e["impact"] == "High"):
        return "⭐", "PRIORIDAD MÁXIMA"
    if e["impact"] == "High":
        return "🔶", "PRIORIDAD ALTA"
    return "🔹", "PRIORIDAD MEDIA"


def top_ids_by_currency(events):
    best = {}
    for e in events:
        if e["impact"] == "High" and (e["country"] not in best or weight(e) > weight(best[e["country"]])):
            best[e["country"]] = e
    return {id(e) for e in best.values()}


def event_card(e, top_ids=()):
    icon, label = prio(e, top_ids)
    rule = e["_rule"]
    lines = [f"{icon} {label} — {e['country']}: {ev_name(e)}",
             f"   🕐 {tiempo(e['_dt'])} · Consenso {e.get('forecast') or '-'} · Previo {e.get('previous') or '-'}"]
    vig = (rule[2] + (" " + rule[5] if rule[5] else "")) if rule else ""
    if vig:
        lines.append(f"   Vigilar: {vig}")
    lines.append(f"   ▲ Mayor que el consenso → {phrase(e, True)} · ▼ Menor → {phrase(e, False)}")
    return "\n".join(lines)


def trigger_lines(pair, events, limit=2):
    """Qué hacer con el par según el resultado de cada evento clave."""
    b, q = pair[:3], pair[3:]
    evs = sorted([e for e in events if e["country"] in (b, q)],
                 key=lambda e: (-weight(e), e["_dt"]))[:limit]
    lines = []
    for e in sorted(evs, key=lambda e: e["_dt"]):
        cur = e["country"]
        good, bad = ("menor", "mayor") if inv(e) else ("mayor", "menor")
        act_good, act_bad = ("COMPRA", "VENTA") if cur == b else ("VENTA", "COMPRA")
        lines.append(f"   {gmt(e['_dt']):%H:%M} GMT {cur} {ev_name(e)}: {good} que el consenso → {act_good} · "
                     f"{bad} → {act_bad}")
    return lines


def semaforo_text(model):
    groups = {"🟢": [], "🟡": [], "🟠": [], "🔴": []}
    for c in sorted(model, key=lambda c: -model[c]["strength"]):
        groups[luz(model[c]["strength"])].append(f"{c} {model[c]['strength']:+.1f}")
    lines = [f"{k} " + " · ".join(v) for k, v in groups.items() if v]
    lines.append("(🟢 fuerte · 🟡 algo fuerte · 🟠 algo débil · 🔴 débil)")
    return "\n".join(lines)


def porque_text(model):
    order = sorted(model, key=lambda c: -model[c]["strength"])
    pick = []
    for c in order[:2] + order[-2:]:
        if c not in pick:
            pick.append(c)
    rows = []
    for c in pick:
        why = " · ".join(t for _, t, _ in model[c]["top"][:2]) or "sin factores destacados"
        rows.append(f"   {c}: {why}")
    return "Por qué:\n" + "\n".join(rows)


def pares_text(results, model, events, limit=6):
    dirs = [r for r in sorted(results, key=lambda r: -abs(r["total"])) if r["d"]]
    lines = []
    for i, r in enumerate(dirs[:limit], 1):
        b, q = r["pair"][:3], r["pair"][3:]
        act = "COMPRA" if r["d"] > 0 else "VENTA"
        cal = f" · calidad {r['grade']}" if r["grade"] else ""
        lines.append(f"{i}. {r['pair']} → {act} · {r['fuerza']}{cal}")
        lines.append(f"   {luz(model[b]['strength'])} {b} {model[b]['strength']:+.1f} vs "
                     f"{luz(model[q]['strength'])} {q} {model[q]['strength']:+.1f}")
        if r["grade"]:
            lines += trigger_lines(r["pair"], events)
    if not dirs:
        lines.append("Ningún par tiene hoy una dirección fundamental clara: conviene esperar.")
    sin = [r["pair"] for r in results if not r["d"]]
    if sin:
        lines.append("Sin dirección clara: " + ", ".join(sin))
    return "\n".join(lines)


def contexto_text(m, intensity, heads):
    q, L = m["q"], []
    v = q.get("VIX")
    if v:
        lvl = v["last"]
        zona = ("calma" if lvl < 16 else "normal" if lvl < 20 else "nerviosismo" if lvl < 25
                else "miedo alto" if lvl < 30 else "pánico")
        ef = (" → favorece USD, JPY y CHF" if m["riskoff"] > 0.5 else
              " → favorece AUD, NZD y CAD" if m["riskoff"] < -0.5 else "")
        L.append(f"😨 VIX {lvl:.1f} ({v['p1'] * 100:+.1f}%) — {zona}{ef}")
    y10, y3 = to_yield(q.get("US10Y")), to_yield(q.get("US3M"))
    if y10:
        s = f"🏦 Bono EE. UU. 10A {y10['lvl']:.2f}% ({y10['d1bp']:+.0f} pb hoy · {y10['d5bp']:+.0f} en 5 d)"
        if y3:
            s += f" · curva 10A–3M {y10['lvl'] - y3['lvl']:+.2f}"
        s += " → " + ("apoya al USD" if m["yscore"] > 0.5 else "resta apoyo al USD" if m["yscore"] < -0.5
                      else "sin cambio relevante")
        L.append(s)
        y5, y30 = to_yield(q.get("US5Y")), to_yield(q.get("US30Y"))
        if y5 and y30:
            L.append(f"   5A {y5['lvl']:.2f}% ({y5['d1bp']:+.0f} pb) · 30A {y30['lvl']:.2f}% ({y30['d1bp']:+.0f} pb)")
    d = q.get("DXY")
    if d:
        L.append(f"💵 Índice dólar DXY {d['last']:.2f} ({d['p1'] * 100:+.2f}%)")
    w, b = q.get("WTI"), q.get("BRENT")
    if w:
        s = f"🛢 Petróleo WTI {w['last']:.1f} ({w['p1'] * 100:+.1f}%)"
        if b:
            s += f" · Brent {b['last']:.1f} ({b['p1'] * 100:+.1f}%)"
        s += " → " + ("favorece al CAD" if m["oscore"] > 0.5 else "resta apoyo al CAD" if m["oscore"] < -0.5
                      else "sin cambio relevante")
        L.append(s)
    if not L:
        L.append("Sin datos de mercado disponibles.")
    L.append(f"🌍 Tensión geopolítica {['baja', 'moderada', 'alta'][intensity]}"
             + (":" if heads else "."))
    L += [f"   {ic} {t}" for ic, t in heads[:4]]
    return "\n".join(L)


def publicado_text(hs, now):
    start = datetime(gmt(now).year, gmt(now).month, gmt(now).day, tzinfo=UTC)
    rows = []
    for s in sorted(hs, key=lambda s: s["dt"]):
        if s["how"] != "real" or not (start <= s["dt"].astimezone(UTC) <= now.astimezone(UTC)):
            continue
        res = "mejor de lo esperado" if s["sur"] > 0 else "peor de lo esperado" if s["sur"] < 0 else "en línea"
        rows.append(f"✅ {s['cur']}: {s['title']} → real {s['actual']} · consenso {s['forecast'] or '-'} · "
                    f"previo {s['previous'] or '-'} → {res}")
    return "\n".join(rows)


def cambios_text(model, results, prev):
    if not prev or "currencies" not in prev:
        return "Es el primer plan guardado; en el próximo verás qué cambió y por qué."
    cl, pl = [], []
    for c, v in model.items():
        old = prev["currencies"].get(c)
        if not old:
            continue
        dl = v["strength"] - old["strength"]
        if abs(dl) < 1.0:
            continue
        dc = {k: v["comps"][k] - old["comps"].get(k, 0) for k in COMP_NAME}
        drv = sorted([k for k in dc if abs(dc[k]) >= 0.5], key=lambda k: -abs(dc[k]))[:2]
        why = "; ".join(f"{COMP_NAME[k]} {dc[k]:+.1f}" for k in drv)
        cl.append((abs(dl), f"   {luz(old['strength'])}→{luz(v['strength'])} {c} {old['strength']:+.1f} → "
                            f"{v['strength']:+.1f}" + (f" ({why})" if why else "")))
    for r in results:
        old = prev.get("pairs", {}).get(r["pair"])
        if not old:
            continue
        dt_ = r["total"] - old["total"]
        if old["d"] != r["d"] or abs(dt_) >= 1.5:
            pl.append((abs(dt_), f"   {r['pair']}: {DIR_TXT[old['d']]} {old['total']:+.1f} → "
                                 f"{DIR_TXT[r['d']]} {r['total']:+.1f} ({r['fuerza']})"))
    out = [f"Frente al plan del {prev.get('when', '?')}:"]
    out.append("Divisas:\n" + ("\n".join(t for _, t in sorted(cl, reverse=True)) if cl else "   Sin cambios relevantes."))
    out.append("Pares:\n" + ("\n".join(t for _, t in sorted(pl, reverse=True)) if pl else "   Sin cambios relevantes."))
    return "\n".join(out)


def section(title, paras):
    paras = [p for p in paras if p]
    if not paras:
        return []
    paras[0] = f"━━━ {title} ━━━\n{paras[0]}"
    return paras


def temas_text(events, m):
    """Resumen de la semana: CPIs, tasas, empleo, bonos, crudo y VIX."""
    grupos = {"CPIs": lambda e: cat_of(e)[0] == "Inflación" and e["impact"] == "High",
              "Tasas": lambda e: cat_of(e)[0] == "Tasas de interés" and ev_name(e) != "Discurso de banco central",
              "Empleo": lambda e: cat_of(e)[0] in ("Empleo / nóminas", "Empleo", "Desempleo") and e["impact"] == "High"}
    lines = []
    for nombre, f in grupos.items():
        seen, items = set(), []
        for e in sorted(events, key=lambda e: e["_dt"]):
            k = (e["country"], DIAS[gmt(e["_dt"]).weekday()])
            if f(e) and k not in seen:
                seen.add(k)
                items.append(f"{e['country']} ({k[1]})")
        if items:
            lines.append(f"   {nombre}: " + " · ".join(items))
    q = m["q"]
    y = to_yield(q.get("US10Y"))
    if y:
        lines.append(f"   Bonos: US10A {y['lvl']:.2f}% ({y['d5bp']:+.0f} pb en 5 d)")
    if q.get("WTI"):
        lines.append(f"   Crudo: WTI {q['WTI']['last']:.1f} ({q['WTI']['p5'] * 100:+.1f}% en 5 d) "
                     f"{'↑' if q['WTI']['p5'] > 0 else '↓'}")
    if q.get("VIX"):
        lines.append(f"   VIX: {q['VIX']['last']:.1f} ({q['VIX']['p5'] * 100:+.1f}% en 5 d)")
    return "\n".join(lines) or "Sin temas destacados."


def semana_paras(events):
    by = defaultdict(list)
    for e in events:
        by[gmt(e["_dt"]).date()].append(e)
    paras = []
    for d in sorted(by):
        items = sorted(by[d], key=lambda e: (-weight(e), e["_dt"]))[:10]
        rows = [f"{prio(e)[0]} {tiempo(e['_dt'])} · {e['country']} {ev_name(e)} "
                f"(cons {e.get('forecast') or '-'} · prev {e.get('previous') or '-'})"
                for e in sorted(items, key=lambda e: e["_dt"])]
        paras.append(f"{DIAS_LARGO[d.weekday()]} {d:%d/%m}\n" + "\n".join(rows))
    return paras or ["Sin eventos relevantes: semana de baja volatilidad esperada."]


def sesiones_paras(window):
    if not window:
        return ["Sin eventos relevantes en este día: baja volatilidad esperada."]
    top = top_ids_by_currency(window)
    paras = []
    for idx, (nombre, horas) in enumerate(SESIONES):
        evs = sorted([e for e in window if sesion_idx(e["_dt"]) == idx], key=lambda e: e["_dt"])
        if not evs:
            continue
        cards = [event_card(e, top) for e in evs]
        cards[0] = f"━━━ SESIÓN {nombre} ({horas}) ━━━\n{cards[0]}"
        paras += cards
    return paras


# ---------- estado ----------
def load_state():
    return load_json(STATE_FILE, None)


def make_state(model, results, kind, setups):
    return {"when": datetime.now(TZ).strftime("%d/%m %H:%M"), "plan": kind,
            "currencies": {c: {"strength": round(v["strength"], 2),
                               "comps": {k: round(x, 2) for k, x in v["comps"].items()}}
                           for c, v in model.items()},
            "pairs": {r["pair"]: {"total": r["total"], "d": r["d"], "grade": r["grade"],
                                  "fuerza": r["fuerza"]} for r in results},
            "setups": setups}


def save_state(model, results, kind, chosen):
    data = make_state(model, results, kind,
                      [{"pair": r["pair"], "d": r["d"], "grade": r["grade"]} for r in chosen if r["grade"]])
    save_json(STATE_FILE, data)
    save_json(LIVE_FILE, data)




# ---------- registro automático de resultados ----------
_BARS = {}


def get_bars(pair):
    """Velas de 1 hora (solo para medir los resultados del registro)."""
    if pair not in _BARS:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X?range=1mo&interval=60m"
        bars = None
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            bars = [(t, c, h, l) for t, c, h, l in zip(res["timestamp"], q["close"], q["high"], q["low"])
                    if None not in (c, h, l)]
            bars = bars if len(bars) >= 48 else None
        except Exception as e:
            print(f"Sin velas H1 para {pair}: {e}", file=sys.stderr)
        _BARS[pair] = bars
    return _BARS[pair]


def unit_risk(bars):
    """R = ½ del rango diario medio (últimos 10 días completos)."""
    days = {}
    for t, c, h, l in bars:
        d = datetime.fromtimestamp(t, TZ).date()
        x = days.setdefault(d, [h, l])
        x[0], x[1] = max(x[0], h), min(x[1], l)
    rngs = [v[0] - v[1] for k, v in sorted(days.items())][:-1][-10:]
    return 0.5 * (sum(rngs) / len(rngs)) if rngs else None


def log_signals(chosen, kind, now):
    """Registra cada setup (calidad A/B): entrada = último precio; stop = -1R; objetivo = +2R."""
    data = load_json(RESULTS_FILE, [])
    ids = {x["id"] for x in data}
    for r in chosen:
        if not r["grade"]:
            continue
        sid = f"{now:%Y-%m-%d}-{kind}-{r['pair']}"
        bars = get_bars(r["pair"])
        if sid in ids or not bars:
            continue
        R = unit_risk(bars)
        if not R:
            continue
        data.append({"id": sid, "ts": int(now.timestamp()), "when": f"{now:%d/%m %H:%M}", "kind": kind,
                     "pair": r["pair"], "d": r["d"], "grade": r["grade"], "total": r["total"],
                     "aligned": r["aligned"], "entry": bars[-1][1], "R": R,
                     "horizon_h": 24 if kind == "outlook" else 120,
                     "status": "open", "res": None, "mfe": 0.0, "mae": 0.0})
    save_json(RESULTS_FILE, data)


def evaluate_signals(now):
    """Revisa cada setup abierto: -1R (stop), +2R (objetivo) o, al vencer el horizonte
    (24 h diario / 5 días semanal), el resultado en R. Si toca stop y objetivo en la misma
    vela se cuenta como pérdida (criterio conservador)."""
    data = load_json(RESULTS_FILE, [])
    now_ts = int(now.timestamp())
    for s in data:
        if s["status"] != "open":
            continue
        end = s["ts"] + s["horizon_h"] * 3600
        bars = get_bars(s["pair"])
        if not bars:
            if now_ts > end + 3 * 86400:
                s["status"] = "sin datos"
            continue
        d, entry, R = s["d"], s["entry"], s["R"]
        mfe = mae = 0.0
        last, closed = None, False
        for t, c, h, l in bars:
            if t <= s["ts"]:
                continue
            if t > end:
                break
            fav = ((h - entry) if d > 0 else (entry - l)) / R
            adv = ((entry - l) if d > 0 else (h - entry)) / R
            mfe, mae, last = max(mfe, fav), max(mae, adv), c
            if adv >= 1:
                s.update(status="loss", res=-1.0)
                closed = True
                break
            if fav >= 2:
                s.update(status="win", res=2.0)
                closed = True
                break
        s["mfe"], s["mae"] = round(mfe, 2), round(mae, 2)
        if not closed and now_ts >= end and last is not None:
            s.update(status="expired", res=round(((last - entry) * d) / R, 2))
        if s["status"] in ("win", "loss", "expired"):
            s["closed"] = f"{now:%d/%m %H:%M}"
        elif now_ts > end + 3 * 86400:
            s["status"] = "sin datos"
    save_json(RESULTS_FILE, data)
    write_csv(data)
    return data


def write_csv(data):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fecha", "plan", "par", "direccion", "calidad", "puntaje", "factores_alineados",
                    "entrada", "R", "estado", "resultado_R", "max_favor_R", "max_contra_R", "cierre"])
        for x in data:
            w.writerow([x["when"], x["kind"], x["pair"], "COMPRA" if x["d"] > 0 else "VENTA", x["grade"],
                        x["total"], x["aligned"], x["entry"], round(x["R"], 6), x["status"],
                        x["res"] if x["res"] is not None else "", x["mfe"], x["mae"], x.get("closed", "")])


def results_text(data):
    closed = [x for x in data if x["status"] in ("win", "loss", "expired")]
    open_n = sum(1 for x in data if x["status"] == "open")

    def pack(lst):
        n = len(lst)
        return (n, sum(1 for x in lst if x["res"] > 0) / n * 100 if n else 0,
                sum(x["res"] for x in lst) / n if n else 0)

    if not closed:
        return (f"Aún no hay setups cerrados ({open_n} abiertos). Cada setup se evalúa a 24 h (plan diario) "
                f"o 5 días (plan semanal): −1R = stop, +2R = objetivo, R = ½ rango diario medio.")
    n, wr, avg = pack(closed)
    a, b = pack([x for x in closed if x["grade"] == "A"]), pack([x for x in closed if x["grade"] == "B"])
    names = {"win": "objetivo ✅", "loss": "stop ❌", "expired": "venció"}
    lines = [f"{n} cerrados · {open_n} abiertos · acierto {wr:.0f}% · {avg:+.2f}R por setup",
             f"Calidad A: {a[0]} setups, acierto {a[1]:.0f}%, {a[2]:+.2f}R | "
             f"Calidad B: {b[0]} setups, acierto {b[1]:.0f}%, {b[2]:+.2f}R"]
    for x in closed[-5:]:
        lines.append(f"{x['when']} {x['pair']} {'COMPRA' if x['d'] > 0 else 'VENTA'} ({x['grade']}): "
                     f"{names[x['status']]} {x['res']:+.2f}R")
    lines.append("Archivo completo: reports/resultados.csv en tu repositorio.")
    return "\n".join(lines)


# ---------- planes: semanal (domingo) y diario ----------
def build_plan(kind):
    global SHORT_TEXT
    now = datetime.now(TZ)
    u = gmt(now)
    allev = load_events(("this", "next"))
    hist, _ = update_history(allev)
    hist = ff_backfill(hist, now)          # datos reales de las últimas semanas desde ForexFactory
    save_json(HISTORY_FILE, hist)
    hs = history_surprises(hist, now)

    if kind == "weekly":
        start = u.date() if u.weekday() == 0 else u.date() + timedelta(days=(7 - u.weekday()) % 7 or 7)
        end = start + timedelta(days=4)
        cutoff = datetime(start.year, start.month, start.day, 23, 59, 59, tzinfo=UTC).astimezone(TZ)
        scope = [e for e in allev if e["_dt"] > now and e["country"] in CURS
                 and start <= gmt(e["_dt"]).date() <= end]
        window = scope
        title = f"📆 PLAN SEMANAL — semana del {start:%d/%m} al {end:%d/%m}"
    else:
        cutoff = gmt_cutoff(now)
        day = cutoff.astimezone(UTC).date()
        fri = max(u.date() + timedelta(days=(4 - u.weekday()) % 7), day)
        scope = [e for e in allev if e["_dt"] > now and e["country"] in CURS and gmt(e["_dt"]).date() <= fri]
        window = [e for e in scope if e["_dt"] <= cutoff]
        title = f"📋 PLAN DEL DÍA — {DIAS_LARGO[day.weekday()]} {day:%d/%m}"

    m = market_snapshot()
    intensity, heads = geopolitics()
    model = currency_model(scope, cutoff, hs, m, intensity)
    results = [evaluate_pair(p, model) for p in PAIRS if p[:3] in model and p[3:] in model]
    results.sort(key=rank_key)
    chosen = choose_pairs(results)
    prev = load_state()
    data = evaluate_signals(now)
    log_signals(chosen, kind, now)

    head = f"{title}\nHora de Colombia (GMT entre paréntesis) · actualizado {now:%d/%m %H:%M}"
    warn = []
    if not str(STATUS.get(FF_KEY, "")).startswith("✅"):
        warn = [f"⚠️ No pude leer los datos reales de ForexFactory: {STATUS.get(FF_KEY, 'sin respuesta')}. "
                "La fuerza usa solo consensos y el historial."]
    parts = [head] + warn
    parts += section("🚦 FUERZA DE LAS DIVISAS", [semaforo_text(model), porque_text(model)])
    parts += section("🎯 PARES — DIRECCIÓN FUNDAMENTAL", [pares_text(results, model, window)])
    SHORT_TEXT = "\n\n".join(parts)
    parts += section("🔄 CAMBIOS RESPECTO AL PLAN ANTERIOR", [cambios_text(model, results, prev)])
    parts += section("🌍 CONTEXTO DE MERCADO", [contexto_text(m, intensity, heads)])
    if kind == "weekly":
        parts += section("🧭 TEMAS DE LA SEMANA", [temas_text(window, m)])
        parts += section("🗓 AGENDA POR DÍA (días en GMT)", semana_paras(window))
    else:
        pub = publicado_text(hs, now)
        if pub:
            parts += section("✅ YA PUBLICADO HOY", [pub])
        parts += sesiones_paras(window)

    closed = [x for x in data if x["status"] in ("win", "loss", "expired")]
    if closed:
        wr = sum(1 for x in closed if x["res"] > 0) / len(closed) * 100
        reg = (f"📒 Registro: {len(closed)} setups cerrados · acierto {wr:.0f}% · "
               f"{sum(x['res'] for x in closed) / len(closed):+.2f}R promedio.")
    else:
        reg = "📒 Registro: aún sin setups cerrados (se evalúan a 24 h / 5 días)."
    fuentes = "⚙️ Fuentes: " + " | ".join(f"{k}: {v}" for k, v in STATUS.items())
    parts += [reg + "\n" + fuentes, RULES]
    save_state(model, results, kind, chosen)
    return "\n\n".join(parts)


def weekly():
    return build_plan("weekly")


def outlook():
    return build_plan("outlook")


# ---------- alertas antes del dato y actualización después del dato ----------
def setup_lines(e, setups, higher=None):
    """Cómo afecta el dato a los setups activos del último plan."""
    cur, rows = e["country"], []
    for st in setups:
        p = st["pair"]
        if cur not in (p[:3], p[3:]):
            continue

        def effect(h):
            strong = (h != inv(e))
            pair_dir = 1 if (cur == p[:3]) == strong else -1
            return "CONFIRMA" if pair_dir == st["d"] else "CONTRADICE"

        act = "COMPRA" if st["d"] > 0 else "VENTA"
        if higher is None:
            rows.append(f"   📌 {p}: setup {act} (calidad {st['grade']}) → dato MAYOR {effect(True)} · "
                        f"dato MENOR {effect(False)}")
        else:
            rows.append(f"   📌 {p}: el dato {effect(higher)} tu setup de {act} (calidad {st['grade']})")
    return ("\n" + "\n".join(rows)) if rows else ""


def approx_eq(a, b):
    """True/False si coinciden (tolerando cambios de unidad: K, M...); None si falta alguno."""
    if a is None or b is None:
        return None
    return any(abs(a - b * k) <= 1e-6 * max(1.0, abs(a)) for k in (1, 1e3, 1e6, 1e-3, 1e-6))


def provider_actuals(now):
    """Datos reales desde una API opcional (ACTUALS_URL + ACTUALS_KEY). Acepta respuestas tipo
    lista o {'economicCalendar': [...]} con campos actual/estimate(forecast)/previous(prev)/time(date)."""
    if not (ACTUALS_URL and ACTUALS_KEY):
        return []
    url = ACTUALS_URL.format(desde=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
                             hasta=(now + timedelta(days=1)).strftime("%Y-%m-%d"), clave=ACTUALS_KEY)
    data = fetch_json(url, tries=2, wait=5)
    if isinstance(data, dict):
        data = data.get("economicCalendar") or data.get("data") or data.get("result") or []
    out = []
    for x in data or []:
        try:
            act = x.get("actual")
            if act in (None, ""):
                continue
            raw = str(x.get("time") or x.get("date")).replace("Z", "+00:00").replace(" ", "T")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            cur = (x.get("currency") or CTRY2CUR.get(str(x.get("country", "")).upper(), "")).upper()
            out.append({"cur": cur, "dt": dt, "actual": str(act),
                        "est": x.get("estimate", x.get("forecast")), "prev": x.get("previous", x.get("prev"))})
        except Exception:
            continue
    return out


def find_actual(e, web, api):
    """Dato real de un evento: del calendario, de la página de ForexFactory o de la API opcional."""
    if e.get("actual"):
        return e["actual"], "calendario"
    for w in web:
        if w["cur"] != e["country"] or norm_title(w["title"]) != norm_title(e["title"]) or not w["actual"]:
            continue
        if (w["dt"] and abs((w["dt"] - e["_dt"]).total_seconds()) <= 5400) or \
                (not w["dt"] and w["day"] == e["_dt"].date()):
            return w["actual"], "ForexFactory"
    for p in api:
        if p["cur"] != e["country"] or abs((p["dt"] - e["_dt"]).total_seconds()) > 600:
            continue
        fe = approx_eq(num(p["est"]), num(e.get("forecast")))
        pe = approx_eq(num(p["prev"]), num(e.get("previous")))
        if fe is True or (fe is None and pe is True):
            return p["actual"], "API"
    return None, None


def reaction_message(items, now):
    """Actualización ~5 min después de un dato relevante: cómo quedan TODAS las divisas y TODOS los pares.
    items = lista de (evento, dato real o None, fuente) de la misma hora."""
    fmt_d = "%Y-%m-%dT%H:%M"
    hist = load_json(HISTORY_FILE, {})
    for e, actual, _ in items:
        if actual:
            rel = hist.setdefault(f"{e['country']}|{e['title']}", {})
            rel.setdefault(e["_dt"].strftime(fmt_d), {"f": e.get("forecast") or "", "p": e.get("previous") or "",
                                                      "a": "", "i": e["impact"]})["a"] = str(actual)
    save_json(HISTORY_FILE, hist)

    curs = sorted({e["country"] for e, _, _ in items})
    out = [f"✅ PUBLICADO — {', '.join(curs)} ({now:%H:%M} COL)"]
    missing = False
    for e, actual, src in items:
        base = f"• {e['country']} · {ev_name(e)}:"
        if not actual:
            out.append(f"{base} valor real no disponible")
            missing = True
            continue
        a, f = num(actual), num(e.get("forecast"))
        out.append(f"{base} real {actual} · consenso {e.get('forecast') or '-'} · previo {e.get('previous') or '-'}"
                   f" [{src}]")
        if a is not None and f is not None and a != f:
            out.append(f"   → {'MAYOR' if a > f else 'MENOR'} que el consenso: {phrase(e, a > f)}")
        elif a is not None and f is not None:
            out.append("   → en línea con el consenso")
    if missing:
        out.append(f"ℹ️ No pude leer el valor real en ForexFactory ({STATUS.get(FF_KEY, 'sin respuesta')}). "
                   "Se asume que salió como se esperaba. Para escribirlo: Actions → Sistema FX → Run workflow "
                   "→ modo «dato».")

    allev = load_events(("this", "next"), required=False)
    hs = history_surprises(hist, now)
    for e, actual, _ in items:   # dato escrito a mano antes de la hora oficial: cuenta igualmente
        if not actual or e["_dt"] <= now:
            continue
        a, f, p = num(actual), num(e.get("forecast")), num(e.get("previous"))
        sur = 0 if (a is None or f is None or a == f) else (1 if (a > f) != inv(e) else -1)
        tr = 0 if (a is None or p is None or a == p) else (1 if (a > p) != inv(e) else -1)
        if sur or tr:
            hs.append({"cur": e["country"], "title": ev_name(e), "w": weight(e), "sur": sur, "trend": tr,
                       "age": 0.0, "decay": 1.0, "actual": str(actual), "forecast": e.get("forecast") or "",
                       "previous": e.get("previous") or "", "how": "real", "dt": now})
    u = gmt(now)
    fri = max(u.date() + timedelta(days=(4 - u.weekday()) % 7), u.date())
    scope = []
    for x in allev:
        if x["country"] not in CURS or not (u.date() <= gmt(x["_dt"]).date() <= fri):
            continue
        if hist.get(f"{x['country']}|{x['title']}", {}).get(x["_dt"].strftime(fmt_d), {}).get("a"):
            continue   # ya publicado: lo cuenta el historial (sorpresa + tendencia)
        scope.append(x)
    m = market_snapshot()
    intensity, _ = geopolitics(translate=False)
    model = currency_model(scope, gmt_cutoff(now), hs, m, intensity)
    results = [evaluate_pair(p, model) for p in PAIRS if p[:3] in model and p[3:] in model]
    results.sort(key=rank_key)

    live = load_json(LIVE_FILE, None) or load_state() or {}
    old_c, old_p = live.get("currencies", {}), live.get("pairs", {})

    out.append("🚦 CÓMO QUEDAN LAS DIVISAS (antes → ahora)")
    for c in sorted(model, key=lambda c: -model[c]["strength"]):
        n = model[c]["strength"]
        o = old_c.get(c, {}).get("strength")
        mark = "" if o is None or abs(n - o) < 0.5 else (" ▲" if n > o else " ▼")
        out.append(f"   {luz(n)} {c} {f'{o:+.1f} → ' if o is not None else ''}{n:+.1f}{mark}")
    for c in curs:
        if c in model:
            why = " · ".join(t for _, t, _ in model[c]["top"][:3]) or "sin factores destacados"
            out.append(f"   Por qué {c}: {why}")

    out.append("🎯 CÓMO QUEDAN LOS PARES (antes → ahora)")

    def delta(r):
        o = old_p.get(r["pair"])
        return r["total"] - o["total"] if o else 0.0

    for r in sorted(results, key=lambda r: -abs(delta(r))):
        o = old_p.get(r["pair"])
        d = delta(r)
        antes = f"{DIR_TXT[o['d']]} {o['total']:+.1f} → " if o else ""
        favor = f" · favorece {'COMPRA' if d > 0 else 'VENTA'}" if abs(d) >= 0.5 else ""
        out.append(f"   {r['pair']}: {antes}{DIR_TXT[r['d']]} {r['total']:+.1f} ({r['fuerza']}){favor}")

    q = m["q"]
    mk = []
    if q.get("VIX"):
        mk.append(f"VIX {q['VIX']['last']:.1f} ({q['VIX']['p1'] * 100:+.1f}%)")
    y = to_yield(q.get("US10Y"))
    if y:
        mk.append(f"US10A {y['lvl']:.2f}% ({y['d1bp']:+.0f} pb)")
    if q.get("DXY"):
        mk.append(f"DXY {q['DXY']['last']:.2f} ({q['DXY']['p1'] * 100:+.2f}%)")
    if q.get("WTI"):
        mk.append(f"WTI {q['WTI']['last']:.1f} ({q['WTI']['p1'] * 100:+.1f}%)")
    if mk:
        out.append("📈 Mercado ahora: " + " · ".join(mk))

    setups = live.get("setups", [])
    rows = []
    for e, actual, _ in items:
        a, f = num(actual), num(e.get("forecast"))
        if a is not None and f is not None and a != f:
            rows.append(setup_lines(dict(e, actual=str(actual)), setups, a > f))
    rows = [r for r in rows if r]
    by_pair = {r["pair"]: r for r in results}
    warn = [f"   ⚠️ {st['pair']}: tu setup de {'COMPRA' if st['d'] > 0 else 'VENTA'} ya no tiene respaldo "
            f"fundamental (ahora {DIR_TXT[by_pair[st['pair']]['d']]} {by_pair[st['pair']]['total']:+.1f})."
            for st in setups if st["pair"] in by_pair and by_pair[st["pair"]]["d"] != st["d"]]
    if rows or warn:
        out.append("Tus setups:" + "".join(rows))
        out += warn
    out.append("👉 Espera una vela de 5 min a favor antes de entrar; riesgo máximo 1%.")

    save_json(LIVE_FILE, make_state(model, results, "reaction", setups))
    return "\n".join(out)


def manual(cur, kw, val):
    """Dato real escrito a mano (workflow 'dato'): actualiza la dirección al instante."""
    now = datetime.now(TZ)
    cur, kw, val = cur.strip().upper(), kw.strip().lower(), val.strip()
    if not (cur and kw and val):
        return "Faltan datos: indica divisa, nombre del evento y valor real."
    evs = [e for e in load_events(("this", "next")) if e["country"] == cur and kw in e["title"].lower()]
    if not evs:
        return f"No encontré un evento de {cur} cuyo nombre contenga '{kw}' en esta semana ni la próxima."
    e = min(evs, key=lambda e: abs((e["_dt"] - now).total_seconds()))
    sent = load_json(ALERTS_FILE, {})
    sent[f"{e['country']}|{e['title']}|{e['date']}|react"] = now.isoformat()
    save_json(ALERTS_FILE, sent)
    return reaction_message([(e, val, "manual")], now)


def alerts():
    """Se ejecuta cada 5 min, pero solo envía mensaje en dos casos:
    ANTES: aviso 5-30 min antes de cada evento relevante.
    DESPUÉS: ~5 min tras un dato de impacto ALTO, cómo quedan todas las divisas y todos los pares.
    Cada mensaje se envía una sola vez."""
    now = datetime.now(TZ)
    hist = load_json(HISTORY_FILE, {})
    if hist:  # filtro rápido sin red: ¿hay algún evento conocido entre -50 y +30 min?
        near = False
        for rel in hist.values():
            for d in rel:
                mins = (datetime.strptime(d, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ) - now).total_seconds() / 60
                if -50 < mins <= 30:
                    near = True
                    break
            if near:
                break
        if not near:
            return ""
    events = [e for e in load_events(required=False) if e["country"] in CURS]
    hist, changed = update_history(events)
    if changed:
        save_json(HISTORY_FILE, hist)
    cutoff = (now - timedelta(days=3)).isoformat()
    sent = {k: v for k, v in load_json(ALERTS_FILE, {}).items() if v >= cutoff}
    setups = (load_json(LIVE_FILE, None) or load_state() or {}).get("setups", [])
    react_all = os.getenv("REACT_IMPACT", "High") == "Medium"
    web, api, msgs, due = None, None, [], defaultdict(list)
    for e in events:
        key = f"{e['country']}|{e['title']}|{e['date']}"
        mins = (e["_dt"] - now).total_seconds() / 60
        if 5 < mins <= 30 and key + "|pre" not in sent:
            msgs.append(f"⏰ EN {mins:.0f} MIN\n{event_card(e, top_ids_by_currency(events))}"
                        f"{setup_lines(e, setups)}\n   👉 No abras operaciones; define stop y objetivo antes del dato.")
            sent[key + "|pre"] = now.isoformat()
        if -45 <= mins <= -4 and key + "|react" not in sent and (e["impact"] == "High" or react_all):
            due[e["date"]].append(e)
    for date, evs in due.items():
        mins = (evs[0]["_dt"] - now).total_seconds() / 60
        if web is None:
            web, api = web_actuals(now), provider_actuals(now)
        items = [(e,) + find_actual(e, web, api) for e in evs]
        if mins > -12 and any(not a for _, a, _ in items):
            continue   # da unos minutos más: ForexFactory puede tardar en mostrar el valor
        msgs.append(reaction_message(items, now))
        for e in evs:
            sent[f"{e['country']}|{e['title']}|{e['date']}|react"] = now.isoformat()
    if msgs:
        save_json(ALERTS_FILE, sent)
    return "\n\n".join(msgs)




# ---------- envío ----------
def chunks(text, n=3800):
    out, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > n:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + para
    if cur:
        out.append(cur)
    final = []
    for c in out:
        final += [c[i:i + n + 200] for i in range(0, len(c), n + 200)]
    return final


def send_telegram(text):
    """TELEGRAM_CHAT_ID admite varios destinos separados por coma.
    Para enviar a un TEMA (pestaña) de un grupo con temas: -1001234567890:5"""
    token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
    targets = [c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
    if not text:
        return
    if not (token and targets):
        print("ERROR: los secretos TELEGRAM_TOKEN o TELEGRAM_CHAT_ID llegan vacios", file=sys.stderr)
        return
    for target in targets:
        chat, _, thread = target.partition(":")
        for part in chunks(text):
            payload = {"chat_id": chat.strip(), "text": part}
            if thread.strip():
                payload["message_thread_id"] = thread.strip()
            data = urllib.parse.urlencode(payload).encode()
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                       data=data, timeout=30)
                print(f"Telegram: enviado a {target}")
            except Exception as e:
                cuerpo = e.read().decode() if hasattr(e, "read") else ""
                print(f"Telegram ERROR ({target}): {e} {cuerpo}", file=sys.stderr)


def send_whatsapp(text):
    """OPCIONAL y no oficial (Green-API). Requiere GREENAPI_ID, GREENAPI_TOKEN y WHATSAPP_CHAT_ID."""
    inst = (os.getenv("GREENAPI_ID") or "").strip()
    tok = (os.getenv("GREENAPI_TOKEN") or "").strip()
    chat = (os.getenv("WHATSAPP_CHAT_ID") or "").strip()
    if not (inst and tok and chat and text):
        return
    base = (os.getenv("GREENAPI_URL") or "https://api.green-api.com").rstrip("/")
    for part in chunks(text, 3500):
        body = json.dumps({"chatId": chat, "message": part}).encode()
        req = urllib.request.Request(f"{base}/waInstance{inst}/sendMessage/{tok}", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
            print("WhatsApp: enviado")
        except Exception as e:
            cuerpo = e.read().decode() if hasattr(e, "read") else ""
            print(f"WhatsApp ERROR: {e} {cuerpo}", file=sys.stderr)
        time.sleep(3)


def save(name, text):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        f.write(text)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "outlook"
    now = datetime.now(TZ)
    if mode == "alerts" and now.weekday() >= 5 and not FORCE:
        print("Fin de semana: no se envían alertas.")
        return
    if mode == "outlook" and now.weekday() >= 4 and not FORCE:
        print("Viernes a domingo: no se envía el plan diario (el domingo llega el plan semanal).")
        return
    if mode == "alerts":
        text = alerts()
    elif mode == "dato":
        a = (sys.argv[2:5] + ["", "", ""])[:3]
        text = manual(a[0], a[1], a[2])
    elif mode == "weekly":
        text = weekly()
        save(f"semana-{now:%Y-%m-%d}.txt", text)
    else:
        text = outlook()
        save(f"dia-{now:%Y-%m-%d}.txt", text)
    if text:
        print(text)
        send_telegram(text)
        send_whatsapp(SHORT_TEXT or text)


if __name__ == "__main__":
    main()
