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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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
    """Sorpresas de datos ya publicados (últimos 60 días): real vs pronóstico.
    El calendario gratuito NO trae el dato real, así que se infiere del 'previo'
    del lanzamiento siguiente (el previo de hoy = el real de la vez anterior)."""
    out = []
    for key, rel in hist.items():
        country, title = key.split("|", 1)
        rule = match_rule(title)
        dates = sorted(rel)
        for i, d in enumerate(dates):
            r = rel[d]
            dt = datetime.strptime(d, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ)
            age = (now - dt).total_seconds() / 86400
            if age < 0 or age > 60 or not r.get("f"):
                continue
            actual, how = r.get("a"), "real"
            if not actual and i + 1 < len(dates):
                actual, how = rel[dates[i + 1]].get("p"), "inferido"
            a, f = num(actual), num(r["f"])
            if a is None or f is None or a == f:
                continue
            ev = {"title": title, "country": country, "impact": r.get("i", "High"), "_rule": rule}
            out.append({"cur": country, "title": rule[1] if rule else title, "w": weight(ev),
                        "sur": 1 if (a > f) != inv(ev) else -1, "age": age,
                        "decay": 0.5 ** (age / HALF_LIFE_DAYS), "actual": actual,
                        "forecast": r["f"], "how": how, "dt": dt})
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


def currency_model(scope, first_day, hs, m, geo_int):
    """Fuerza de cada divisa = datos por venir + datos publicados + mercado + geopolítica."""
    U, P = defaultdict(float), defaultdict(float)
    up_w, past_w = defaultdict(list), defaultdict(list)
    for e in scope:
        c = e["country"]
        if c not in CURS:
            continue
        mult = TIME_MULT if e["_dt"].date() <= first_day else 1.0
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
        v = 1.5 * s["w"] * s["sur"] * s["decay"]
        P[c] += v
        past_w[c].append((abs(v) / 4, f"{s['title']} {s['actual']} vs pron {s['forecast']} "
                          + ("✅" if s["sur"] > 0 else "❌")))
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


# ---------- bloques de texto ----------
def event_block(ev):
    rule = ev["_rule"]
    lines = [
        f"🕐 {ev['_dt']:%H:%M} | {ES[ev['impact']]} | {ev['country']} — {ev_name(ev)}",
        f"   ({ev['title']})",
        f"   Previo: {ev.get('previous') or '-'} | Pronóstico: {ev.get('forecast') or '-'}",
    ]
    if rule:
        lines.append(f"   Qué es: {rule[2]}")
    lines.append(f"   ▲ Si sale MAYOR que el pronóstico → {phrase(ev, True)}")
    lines.append(f"   ▼ Si sale MENOR que el pronóstico → {phrase(ev, False)}")
    if rule and rule[5]:
        lines.append(f"   Nota: {rule[5]}")
    return "\n".join(lines)


def trigger_lines(pair, events, limit=3, with_day=False):
    """Qué hacer con el par según el resultado de cada evento clave."""
    b, q = pair[:3], pair[3:]
    evs = sorted([e for e in events if e["country"] in (b, q)],
                 key=lambda e: (-weight(e), e["_dt"]))[:limit]
    lines = []
    for e in sorted(evs, key=lambda e: e["_dt"]):
        cur = e["country"]
        good = "menor" if inv(e) else "mayor"
        bad = "mayor" if inv(e) else "menor"
        act_good, act_bad = ("COMPRA", "VENTA") if cur == b else ("VENTA", "COMPRA")
        when = f"{DIAS[e['_dt'].weekday()]} {e['_dt']:%H:%M}" if with_day else f"{e['_dt']:%H:%M}"
        lines.append(f"   {when} {e['title']} ({cur}): dato {good} que el pronóstico "
                     f"→ {act_good} {pair} | dato {bad} → {act_bad} {pair}")
    return lines


RULES = (
    "⚠️ REGLAS DE EJECUCIÓN\n"
    "1. No abras operaciones 15 min antes de un evento 🔴.\n"
    "2. Espera el dato y una vela de 5 min cerrada a favor de la dirección.\n"
    "3. Si el dato sale en línea con el pronóstico, la reacción suele ser débil: no fuerces.\n"
    "4. Stop más allá del extremo de la vela del dato; objetivo mínimo 1:2.\n"
    "5. Riesgo máximo 1% por operación. Evita varios pares de la misma divisa a la vez.\n"
    "Esto es información, no asesoría financiera."
)


def scenario_block(r, model, events, with_day, near, m):
    p = r["pair"]
    act = "COMPRA" if r["d"] > 0 else "VENTA" if r["d"] < 0 else "SIN DIRECCIÓN"
    calidad = f"calidad {r['grade']} · {r['aligned']}/4 factores alineados" if r["grade"] else "seguimiento"
    c = r["comps"]
    lines = [f"▪️ {p} → {act} · dirección {r['fuerza']} · {calidad} · puntaje {r['total']:+.1f}",
             f"   [por venir {c['up']:+.1f} · publicados {c['past']:+.1f} · mercado {c['mkt']:+.1f} "
             f"· geopolítica {c['geo']:+.1f}]"]
    lines += [f"   Por qué: {w}" for w in r["why"]]
    for e in (near or []):
        if e["country"] in (p[:3], p[3:]) and e["impact"] == "High":
            lines.append(f"   ⏳ DATO CLAVE {DIAS[e['_dt'].weekday()]} {e['_dt']:%H:%M} — {e['country']} "
                         f"{ev_name(e)}: no entres antes; puede confirmar o invalidar el sesgo.")
    q = m["q"]
    watch = []
    if q.get("VIX"):
        watch.append(f"VIX {q['VIX']['last']:.1f}")
    y = to_yield(q.get("US10Y"))
    if y:
        watch.append(f"US10A {y['lvl']:.2f}%")
    if q.get("WTI"):
        watch.append(f"WTI {q['WTI']['last']:.1f}")
    if watch:
        lines.append(f"   Vigila: {' · '.join(watch)} (si se mueven contra el sesgo, reduce o espera).")
    trig = trigger_lines(p, events, limit=3, with_day=with_day)
    if trig:
        lines.append("   Confirma o invalida con el dato:")
        lines += trig
    return "\n".join(lines)


# ---------- estado y cambios entre planes ----------
def load_state():
    return load_json(STATE_FILE, None)


def make_state(model, results, kind, setups):
    return {"when": datetime.now(TZ).strftime("%d/%m %H:%M"), "plan": kind,
            "currencies": {c: {"strength": round(v["strength"], 2),
                               "comps": {k: round(x, 2) for k, x in v["comps"].items()},
                               "top": {k: (v["why"][k][0][1] if v["why"][k] else "") for k in COMP_NAME}}
                           for c, v in model.items()},
            "pairs": {r["pair"]: {"total": r["total"], "d": r["d"], "grade": r["grade"],
                                  "fuerza": r["fuerza"]} for r in results},
            "setups": setups}


def save_state(model, results, kind, chosen):
    data = make_state(model, results, kind,
                      [{"pair": r["pair"], "d": r["d"], "grade": r["grade"]} for r in chosen if r["grade"]])
    save_json(STATE_FILE, data)
    save_json(LIVE_FILE, data)   # el estado en vivo parte del último plan


def changes_paras(model, results, prev):
    if not prev or "currencies" not in prev:
        return ["Es el primer plan guardado; desde el próximo verás cómo cambió cada divisa y cada par, y por qué."]
    head = f"Comparado con el plan del {prev.get('when', '?')}:"
    cur_lines, deltas = [], {}
    for c, v in model.items():
        old = prev["currencies"].get(c)
        if not old:
            continue
        deltas[c] = v["strength"] - old["strength"]
        if abs(deltas[c]) < 1.0:
            continue
        dc = {k: v["comps"][k] - old["comps"].get(k, 0) for k in COMP_NAME}
        drivers = sorted([k for k in dc if abs(dc[k]) >= 0.5], key=lambda k: -abs(dc[k]))[:2]
        why = "; ".join(f"{COMP_NAME[k]} {dc[k]:+.1f}" + (f" ({v['why'][k][0][1]})" if v["why"][k] else "")
                        for k in drivers) or "ajustes menores"
        cur_lines.append((abs(deltas[c]),
                          f"   {c}: {old['strength']:+.1f} → {v['strength']:+.1f} ({deltas[c]:+.1f}) "
                          f"{strength_tag(v['strength'])} — {why}"))
    pair_lines = []
    for r in results:
        old = prev.get("pairs", {}).get(r["pair"])
        if not old:
            continue
        dtot = r["total"] - old["total"]
        if old["d"] != r["d"] or abs(dtot) >= 1.5 or old["grade"] != r["grade"]:
            b, q = r["pair"][:3], r["pair"][3:]
            motivo = f"{b} {deltas.get(b, 0):+.1f} y {q} {deltas.get(q, 0):+.1f} en fuerza"
            pair_lines.append((abs(dtot),
                               f"   {r['pair']}: {DIR_TXT[old['d']]} {old['total']:+.1f} ({old.get('fuerza', '-')}, "
                               f"calidad {old['grade'] or '-'}) → {DIR_TXT[r['d']]} {r['total']:+.1f} "
                               f"({r['fuerza']}, calidad {r['grade'] or '-'}) — {motivo}"))
    cur_txt = "\n".join(t for _, t in sorted(cur_lines, reverse=True)) or "   Sin cambios relevantes."
    par_txt = "\n".join(t for _, t in sorted(pair_lines, reverse=True)) or "   Sin cambios relevantes."
    return [f"{head}\n💱 DIVISAS (fuerza y por qué):\n{cur_txt}", f"📊 PARES (sesgo y fuerza):\n{par_txt}"]


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


# ---------- secciones del plan ----------
def section(title, paras):
    paras = [p for p in paras if p]
    if not paras:
        return []
    paras[0] = f"━━━ {title} ━━━\n{paras[0]}"
    return paras


def strength_paras(model, n_hist):
    lines = []
    for i, c in enumerate(sorted(model, key=lambda c: -model[c]["strength"]), 1):
        v = model[c]
        k = v["comps"]
        why = " · ".join(t for _, t, _ in v["top"][:3]) or "sin factores destacados"
        lines.append(f"{i}. {c} {v['strength']:+.1f} {strength_tag(v['strength'])}\n"
                     f"   [por venir {k['up']:+.1f} · publicados {k['past']:+.1f} · mercado {k['mkt']:+.1f} "
                     f"· geo {k['geo']:+.1f}]\n   Por qué: {why}")
    note = (f"Historial: {n_hist} datos publicados con resultado (✅ sorpresa positiva, ❌ negativa)."
            if n_hist else "Datos publicados: historial en construcción (el calendario gratuito no trae el "
                           "dato real; se infiere del 'previo' del siguiente lanzamiento y se llena con los días).")
    return ["\n".join(lines), note + "\n⚡ = dato de hoy/mañana, con peso x2.5."]


def fundamentals_paras(scope, hs):
    by = defaultdict(list)
    for e in scope:
        if e["country"] in CURS:
            by[e["country"]].append(e)
    pubs = defaultdict(list)
    for s in sorted(hs, key=lambda s: s["age"]):
        pubs[s["cur"]].append(s)
    paras = []
    for cur in sorted(set(by) | set(pubs), key=lambda c: c):
        rows = [f"💱 {cur}"]
        for e in sorted(by[cur], key=lambda e: (-weight(e), e["_dt"]))[:4]:
            b = expected_bias(e)
            arrow = "↑ mejora" if b > 0 else "↓ deterioro" if b < 0 else "→ sin cambio"
            rows.append(f"   ⏭ {cat_of(e)[0]}: {ev_name(e)} ({DIAS[e['_dt'].weekday()]} {e['_dt']:%H:%M}) "
                        f"prev {e.get('previous') or '-'} → pron {e.get('forecast') or '-'} · {arrow}")
        for s in pubs[cur][:3]:
            rows.append(f"   ✔ Publicado: {s['title']} {s['actual']} vs pron {s['forecast']} "
                        f"{'✅ mejor' if s['sur'] > 0 else '❌ peor'} ({s['how']})")
        paras.append("\n".join(rows))
    return paras or ["Sin eventos fundamentales relevantes en el periodo."]


def agenda_paras(events):
    events = [e for e in events if e["country"] in CURS]
    if not events:
        return ["Sin eventos relevantes en el periodo: baja volatilidad esperada."]
    by_day = defaultdict(list)
    for e in events:
        by_day[e["_dt"].date()].append(e)
    paras = []
    for d in sorted(by_day):
        top = sorted(by_day[d], key=lambda e: (-weight(e), e["_dt"]))[:8]
        rows = [f"   {e['_dt']:%H:%M} {'🔴' if e['impact'] == 'High' else '🟠'} {e['country']} "
                f"{e['title']} (prev {e.get('previous') or '-'} | pron {e.get('forecast') or '-'})"
                for e in sorted(top, key=lambda e: e["_dt"])]
        paras.append(f"{DIAS[d.weekday()]} {d:%d/%m}\n" + "\n".join(rows))
    return paras


def next_trading_day(now):
    d = now.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


SHORT_TEXT = ""  # versión corta para WhatsApp


def build_plan(kind):
    global SHORT_TEXT
    now = datetime.now(TZ)
    allev = load_events(("this", "next"))
    hist, changed = update_history(allev)
    if changed:
        save_json(HISTORY_FILE, hist)
    hs = history_surprises(hist, now)
    monday = (now - timedelta(days=now.weekday())).date()

    if kind == "weekly":
        start = ((now + timedelta(days=1)) if now.weekday() == 6
                 else (now + timedelta(days=7 - now.weekday()))).date()
        end = start + timedelta(days=4)
        scope = [e for e in allev if start <= e["_dt"].date() <= end]
        agenda_ev, trig_ev, with_day, first_day = scope, scope, True, start
        near = [e for e in scope if e["_dt"].date() <= start]
        title = f"📆 PLAN SEMANAL — {start:%d/%m} al {end:%d/%m/%Y} ({TZ.key})"
        agenda_title = "🗓 AGENDA DE LA SEMANA"
    else:
        nd = next_trading_day(now)
        week_end = max(monday + timedelta(days=4), nd)
        scope = [e for e in allev if e["_dt"] > now and e["_dt"].date() <= week_end]
        agenda_ev = [e for e in scope if e["_dt"].date() <= nd]
        trig_ev, near, with_day, first_day = agenda_ev, agenda_ev, False, nd
        title = f"📋 PLAN DIARIO — para el {DIAS[nd.weekday()]} {nd:%d/%m/%Y} ({TZ.key})"
        agenda_title = "🗓 PRÓXIMAS HORAS (hasta el cierre de mañana)"

    m = market_snapshot()
    intensity, heads = geopolitics()
    model = currency_model(scope, first_day, hs, m, intensity)
    results = [evaluate_pair(p, model) for p in PAIRS if p[:3] in model and p[3:] in model]
    results.sort(key=rank_key)
    chosen = choose_pairs(results)
    prev = load_state()

    data = evaluate_signals(now)
    log_signals(chosen, kind, now)

    # --- resumen ---
    setups = [r for r in chosen if r["grade"]]
    res = []
    if setups:
        res.append("🎯 Pares con dirección fundamental más clara:")
        for i, r in enumerate(setups, 1):
            res.append(f"   {i}. {r['pair']} → {'COMPRA' if r['d'] > 0 else 'VENTA'} · {r['fuerza']} · "
                       f"calidad {r['grade']} · {r['aligned']}/4 factores · {r['total']:+.1f}")
    else:
        res.append("🎯 Ningún par tiene hoy una dirección clara (datos, mercado y geopolítica alineados). "
                   "Conviene esperar o reducir el riesgo.")
    key_events = sorted(sorted([e for e in agenda_ev if e["country"] in CURS],
                               key=lambda e: -weight(e))[:4], key=lambda e: e["_dt"])
    if key_events:
        res.append("⚠️ Eventos clave:")
        res += [f"   {DIAS[e['_dt'].weekday()]} {e['_dt']:%H:%M} {e['country']} — {ev_name(e)}"
                for e in key_events]
    ranked = sorted(model, key=lambda c: -model[c]["strength"])
    res.append(f"💪 Más fuerte: {ranked[0]} ({model[ranked[0]]['strength']:+.1f}) · "
               f"más débil: {ranked[-1]} ({model[ranked[-1]]['strength']:+.1f})")
    res.append(f"🌍 Geopolítica: tensión {['baja', 'moderada', 'alta'][intensity]}.")
    closed = [x for x in data if x["status"] in ("win", "loss", "expired")]
    if closed:
        wr = sum(1 for x in closed if x["res"] > 0) / len(closed) * 100
        res.append(f"📒 Registro: {len(closed)} setups cerrados · acierto {wr:.0f}% · "
                   f"{sum(x['res'] for x in closed) / len(closed):+.2f}R promedio.")
    res.append("ℹ️ La calidad (A/B) mide cuántos factores coinciden; no es una probabilidad "
               "estadística ni está validada con datos históricos.")

    resumen = section("📌 RESUMEN", ["\n".join(res)])
    cambios = section("🔄 CAMBIOS RESPECTO AL PLAN ANTERIOR", changes_paras(model, results, prev))
    fuerza = section("💪 FUERZA FUNDAMENTAL DE CADA DIVISA (de más fuerte a más débil)",
                     strength_paras(model, len(hs)))
    if chosen:
        esc = section("🎯 ESCENARIOS (pares más claros)",
                      [scenario_block(r, model, trig_ev, with_day, near, m) for r in chosen])
    else:
        esc = section("🎯 ESCENARIOS", ["Hoy no hay pares con dirección clara: no se generan escenarios."])
    tabla = section("🧭 DIRECCIÓN Y FUERZA DE TODOS LOS PARES", [
        "\n".join(f"{r['pair']}  {r['label']} {r['fuerza']}  {r['total']:+.1f}  calidad {r['grade'] or '-'}  "
                  f"[venir {r['comps']['up']:+.1f} · publ {r['comps']['past']:+.1f} · "
                  f"merc {r['comps']['mkt']:+.1f} · geo {r['comps']['geo']:+.1f}]" for r in results)])
    mercado = section("📈 MERCADO: VIX, BONOS, DÓLAR Y PETRÓLEO", ["\n".join(m["lines"])])
    fund = section("💱 FUNDAMENTALES POR DIVISA (por venir ⏭ y publicados ✔)", fundamentals_paras(scope, hs))
    agenda = section(agenda_title, agenda_paras(agenda_ev))
    registro = section("📒 REGISTRO DE RESULTADOS", [results_text(data)])
    geo = section("🌍 GEOPOLÍTICA", [geo_text(intensity, heads)])
    estado = section("⚙️ ESTADO DE LAS FUENTES", ["\n".join(f"{k}: {v}" for k, v in STATUS.items())])

    parts_out = [title] + resumen + cambios + fuerza + esc
    SHORT_TEXT = "\n\n".join(parts_out)
    parts_out += tabla + mercado + fund + agenda + registro + geo + estado + [RULES]
    save_state(model, results, kind, chosen)
    return "\n\n".join(parts_out)


def weekly():
    return build_plan("weekly")


def outlook():
    return build_plan("outlook")


# ---------- alertas antes del dato y reacción después del dato ----------
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


def find_actual(e, provs):
    """Dato real de un evento: del calendario, o de la API (se empareja por divisa, hora ±10 min
    y coincidencia del pronóstico para no confundir indicadores que salen a la misma hora)."""
    if e.get("actual"):
        return e["actual"], "calendario"
    for p in provs:
        if p["cur"] != e["country"] or abs((p["dt"] - e["_dt"]).total_seconds()) > 600:
            continue
        fe = approx_eq(num(p["est"]), num(e.get("forecast")))
        pe = approx_eq(num(p["prev"]), num(e.get("previous")))
        if fe is True or (fe is None and pe is True):
            return p["actual"], "API"
    return None, None


def reaction_message(items, now):
    """Actualización completa ~5 min después de un dato relevante: fuerza de CADA divisa y sesgo
    de CADA par, antes → ahora. items = lista de (evento, dato real o None, fuente) de la misma hora."""
    fmt_d = "%Y-%m-%dT%H:%M"
    hist = load_json(HISTORY_FILE, {})
    for e, actual, _ in items:
        if actual:
            rel = hist.setdefault(f"{e['country']}|{e['title']}", {})
            rel.setdefault(e["_dt"].strftime(fmt_d), {"f": e.get("forecast") or "", "p": e.get("previous") or "",
                                                      "a": "", "i": e["impact"]})["a"] = str(actual)
    save_json(HISTORY_FILE, hist)

    curs = sorted({e["country"] for e, _, _ in items})
    out = [f"📊 DATO PUBLICADO — {', '.join(curs)} ({now:%H:%M})"]
    missing = False
    for e, actual, src in items:
        base = f"   • {e['country']} {ev_name(e)} ({e['title']})"
        if not actual:
            out.append(f"{base}: valor real no disponible")
            missing = True
            continue
        a, f = num(actual), num(e.get("forecast"))
        line = f"{base}: real {actual} vs pron {e.get('forecast') or '-'} (previo {e.get('previous') or '-'}) [{src}]"
        if a is not None and f is not None and a != f:
            line += f" → {'MAYOR' if a > f else 'MENOR'}: {phrase(e, a > f)}"
        elif a is not None and f is not None:
            line += " → en línea con el pronóstico"
        out.append(line)
    if missing:
        out.append("ℹ️ Sin el valor real (el calendario gratuito no lo incluye) se asume que el dato salió como "
                   "se esperaba; la actualización recoge la reacción del mercado. Para añadirlo: Actions → "
                   "Sistema FX → Run workflow → modo «dato».")

    # --- recalcular el modelo con lo publicado hasta ahora ---
    allev = load_events(("this", "next"), required=False)
    hs = history_surprises(hist, now)
    for e, actual, _ in items:   # dato escrito a mano antes de la hora oficial: cuenta igualmente
        sur = surprise(dict(e, actual=str(actual))) if actual else 0
        if e["_dt"] > now and sur:
            hs.append({"cur": e["country"], "title": ev_name(e), "w": weight(e), "sur": sur, "age": 0.0,
                       "decay": 1.0, "actual": str(actual), "forecast": e.get("forecast") or "",
                       "how": "real", "dt": now})
    first_day = next_trading_day(now)
    monday = (now - timedelta(days=now.weekday())).date()
    week_end = max(monday + timedelta(days=4), first_day)
    scope = []
    for x in allev:   # eventos de hoy (ya publicados o no) y siguientes; el real sustituye al pronóstico
        if x["country"] not in CURS or not (now.date() <= x["_dt"].date() <= week_end):
            continue
        known = hist.get(f"{x['country']}|{x['title']}", {}).get(x["_dt"].strftime(fmt_d), {}).get("a")
        scope.append(dict(x, forecast=known) if known else x)
    m = market_snapshot()
    intensity, _ = geopolitics(translate=False)
    model = currency_model(scope, first_day, hs, m, intensity)
    results = [evaluate_pair(p, model) for p in PAIRS if p[:3] in model and p[3:] in model]
    results.sort(key=rank_key)

    live = load_json(LIVE_FILE, None) or load_state() or {}
    old_c, old_p = live.get("currencies", {}), live.get("pairs", {})

    out.append("💪 FUERZA DE CADA DIVISA (antes → ahora):")
    for i, c in enumerate(sorted(model, key=lambda c: -model[c]["strength"]), 1):
        n = model[c]["strength"]
        o = old_c.get(c, {}).get("strength")
        mark = "" if o is None or abs(n - o) < 0.5 else (" ▲" if n > o else " ▼")
        out.append(f"   {i}. {c} {f'{o:+.1f} → ' if o is not None else ''}{n:+.1f} {strength_tag(n)}{mark}")
    for c in sorted(model, key=lambda c: -abs(model[c]["strength"] - old_c.get(c, {}).get("strength", 0))):
        o = old_c.get(c, {}).get("strength")
        if c in curs or (o is not None and abs(model[c]["strength"] - o) >= 1.0):
            why = " · ".join(t for _, t, _ in model[c]["top"][:3]) or "sin factores destacados"
            out.append(f"   Por qué {c}: {why}")

    out.append("📊 DIRECCIÓN Y FUERZA DE CADA PAR (antes → ahora):")
    def pair_delta(r):
        o = old_p.get(r["pair"])
        return r["total"] - o["total"] if o else 0.0
    for r in sorted(results, key=lambda r: -abs(pair_delta(r))):
        o = old_p.get(r["pair"])
        d = pair_delta(r)
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
    warn = []
    for st in setups:
        r = by_pair.get(st["pair"])
        if r and r["d"] != st["d"]:
            warn.append(f"   ⚠️ {st['pair']}: tu setup de {'COMPRA' if st['d'] > 0 else 'VENTA'} ya no tiene "
                        f"respaldo fundamental (ahora {DIR_TXT[r['d']]} {r['total']:+.1f}).")
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
    DESPUÉS: ~5 min tras un dato de impacto ALTO, actualización de la fuerza de cada divisa y
    el sesgo de cada par. Cada mensaje se envía una sola vez."""
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
    has_source = bool(ACTUALS_URL and ACTUALS_KEY)
    react_all = os.getenv("REACT_IMPACT", "High") == "Medium"   # "Medium" incluye también datos clave de impacto medio
    provs, msgs, due = None, [], defaultdict(list)
    for e in events:
        key = f"{e['country']}|{e['title']}|{e['date']}"
        mins = (e["_dt"] - now).total_seconds() / 60
        if 5 < mins <= 30 and key + "|pre" not in sent:
            msgs.append(f"⏰ EN {mins:.0f} MIN\n{event_block(e)}{setup_lines(e, setups)}\n"
                        f"   👉 No abras operaciones; define stop/objetivo antes del dato.")
            sent[key + "|pre"] = now.isoformat()
        if -45 <= mins <= -4 and key + "|react" not in sent and (e["impact"] == "High" or react_all):
            due[e["date"]].append(e)
    for date, evs in due.items():
        mins = (evs[0]["_dt"] - now).total_seconds() / 60
        if provs is None:
            provs = provider_actuals(now)
        items = []
        for e in evs:
            actual, src = find_actual(e, provs)
            items.append((e, actual, src))
        if has_source and mins > -10 and any(not a for _, a, _ in items):
            continue   # da unos minutos más a la fuente del dato real
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
