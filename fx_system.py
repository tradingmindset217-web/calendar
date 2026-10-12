#!/usr/bin/env python3
"""
Sistema de eventos económicos para Forex.

Modos:
  python fx_system.py briefing  -> resumen del día (lunes a viernes, 00:00)
  python fx_system.py alerts    -> avisos previos y lectura de datos (lunes a viernes)
  python fx_system.py weekly    -> PLAN SEMANAL principal (domingo 9 pm)
  python fx_system.py outlook   -> sesgos y escenarios para el día siguiente (lun a jue, 9 pm)

Variables opcionales:
  FORCE=1          ejecuta aunque sea fin de semana (para pruebas)
  PAIRS=...        pares a evaluar, separados por coma
  FOCUS_PAIRS=...  pares que SIEMPRE llevan escenarios completos (por defecto GBPAUD)
  FIB_H1_BARS=120  velas H1 para el impulso de Fibonacci
  LIQ_H1_BARS=240  velas H1 para buscar zonas de liquidez

Solo usa la librería estándar (Python 3.9+).
"""
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
UA = {"User-Agent": "Mozilla/5.0"}
FORCE = os.getenv("FORCE") == "1"

PAIRS = [p.strip().upper() for p in os.getenv(
    "PAIRS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD,GBPAUD,GBPJPY,EURJPY,EURGBP,AUDJPY"
).split(",") if p.strip()]
FOCUS = [p.strip().upper() for p in os.getenv("FOCUS_PAIRS", "GBPAUD").split(",") if p.strip()]

W = {"High": 3, "Medium": 2, "Low": 1}
ES = {"High": "🔴 ALTA", "Medium": "🟠 MEDIA", "Low": "🟡 BAJA"}
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
FIB_H1_BARS = int(os.getenv("FIB_H1_BARS", "120"))   # velas H1 del impulso de Fibonacci (~5 días)
LIQ_H1_BARS = int(os.getenv("LIQ_H1_BARS", "240"))   # velas H1 para buscar liquidez (~10 días)
LIQ_RECENT = 36                                       # una toma de liquidez es "reciente" si ocurrió en las últimas 36 velas H1

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


# ---------- utilidades ----------
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


def weight(ev):
    return W[ev["impact"]] + (1 if ev["_rule"] and ev["_rule"][4] else 0)


def phrase(ev, higher):
    """higher=True si el dato sale MAYOR que el pronóstico."""
    strong = (higher != inv(ev))
    return f"{ev['country']} se FORTALECE" if strong else f"{ev['country']} se DEBILITA"


def expected_bias(ev):
    """Expectativa previa: pronóstico vs dato anterior."""
    f, p = num(ev.get("forecast")), num(ev.get("previous"))
    if f is None or p is None or f == p:
        return 0
    sign = 1 if f > p else -1
    return -sign if inv(ev) else sign


def surprise(ev):
    """Sorpresa real (si el feed trae el dato): +1 favorable a la divisa, -1 desfavorable."""
    a, f = num(ev.get("actual")), num(ev.get("forecast"))
    if a is None or f is None or a == f:
        return 0
    return 1 if (a > f) != inv(ev) else -1


def fmt(pair, x):
    return f"{x:.3f}" if "JPY" in pair else f"{x:.5f}"


# ---------- descarga de datos ----------
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


def load_events(which=("this",)):
    raw = []
    for w in which:
        data = fetch_json(FEEDS[w])
        if data is None and w == "this":
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
    return sorted(out, key=lambda e: e["_dt"])


def price_history(pair):
    """Velas diarias de Yahoo Finance (fuente gratuita no oficial)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X?range=6mo&interval=1d"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            q = json.load(r)["chart"]["result"][0]["indicators"]["quote"][0]
        rows = [(c, h, l) for c, h, l in zip(q["close"], q["high"], q["low"])
                if None not in (c, h, l)]
        return rows if len(rows) >= 55 else None
    except Exception as e:
        print(f"Sin precios para {pair}: {e}", file=sys.stderr)
        return None


def fibonacci(bars, price):
    """Retroceso de Fibonacci del último impulso en velas H1 (máx. y mín. de las últimas
    FIB_H1_BARS velas). Si el mínimo ocurrió antes que el máximo, el impulso fue alcista
    y el retroceso baja; si no, el impulso fue bajista y el retroceso sube.
    bars = lista de (timestamp, cierre, máximo, mínimo)."""
    seg = bars[-FIB_H1_BARS:]
    hi_i = max(range(len(seg)), key=lambda i: seg[i][2])
    lo_i = min(range(len(seg)), key=lambda i: seg[i][3])
    hi, lo = seg[hi_i][2], seg[lo_i][3]
    rng = hi - lo
    if rng <= 0:
        return None
    ratios = (0.382, 0.5, 0.618, 0.786)
    if lo_i < hi_i:
        leg = "up"
        lv = {r: hi - rng * r for r in ratios}
        retr = (hi - price) / rng * 100
    else:
        leg = "down"
        lv = {r: lo + rng * r for r in ratios}
        retr = (price - lo) / rng * 100
    return {"leg": leg, "hi": hi, "lo": lo, "lv": lv, "retr": retr}


def technicals(pair):
    rows = price_history(pair)
    if not rows:
        return None
    closes = [r[0] for r in rows]
    price = closes[-1]
    sma20 = sum(closes[-20:]) / 20
    sma50 = sum(closes[-50:]) / 50
    wk = rows[-5:]
    ret = (price / closes[-6] - 1) * 100
    score = (1 if price > sma20 else -1) + (1 if sma20 > sma50 else -1) + (1 if ret > 0 else -1)
    return {"price": price, "sma20": sma20, "sma50": sma50,
            "hi": max(r[1] for r in wk), "lo": min(r[2] for r in wk),
            "ret": ret, "score": score}


# ---------- velas H1: Fibonacci y liquidez ----------
_H1_CACHE = {}


def h1_history(pair):
    """Velas de 1 hora de Yahoo Finance (gratuito, no oficial): (timestamp, cierre, máx, mín)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{pair}=X?range=1mo&interval=60m"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        bars = [(t, c, h, l) for t, c, h, l in zip(res["timestamp"], q["close"], q["high"], q["low"])
                if None not in (c, h, l)]
        return bars if len(bars) >= 60 else None
    except Exception as e:
        print(f"Sin velas H1 para {pair}: {e}", file=sys.stderr)
        return None


def pip(pair):
    return 0.01 if "JPY" in pair else 0.0001


def cluster(levels, tol):
    """Agrupa niveles cercanos (máximos o mínimos iguales = liquidez más fuerte)."""
    out = []
    for lv in sorted(levels):
        if out and lv - out[-1][-1] <= tol:
            out[-1].append(lv)
        else:
            out.append([lv])
    return [(sum(c) / len(c), len(c)) for c in out]


def liquidity(bars, k=3):
    """Zonas de liquidez en H1.
    - Pendiente: máximos/mínimos oscilantes (fractales de k velas) que el precio aún NO ha superado.
      Arriba de los máximos hay liquidez compradora (BSL); debajo de los mínimos, vendedora (SSL).
    - Toma de liquidez: una vela superó un máximo/mínimo con la mecha pero CERRÓ de vuelta
      dentro (barrido con rechazo) en las últimas LIQ_RECENT velas y el rechazo sigue vigente."""
    seg = bars[-LIQ_H1_BARS:]
    n = len(seg)
    cs = [b[1] for b in seg]
    hs = [b[2] for b in seg]
    ls = [b[3] for b in seg]
    price = cs[-1]
    trs = [max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1])) for i in range(1, n)]
    atr = sum(trs[-14:]) / 14
    tol = 0.15 * atr
    pend_hi, pend_lo, sweeps = [], [], []
    for i in range(k, n - k):
        if hs[i] > max(hs[i - k:i]) and hs[i] >= max(hs[i + 1:i + k + 1]):
            j = next((j for j in range(i + k + 1, n) if hs[j] > hs[i]), None)
            if j is None:
                pend_hi.append(hs[i])
            elif cs[j] < hs[i] and j >= n - LIQ_RECENT and max(cs[j:]) <= hs[i]:
                sweeps.append({"side": "high", "level": hs[i], "ago": n - 1 - j})
        if ls[i] < min(ls[i - k:i]) and ls[i] <= min(ls[i + 1:i + k + 1]):
            j = next((j for j in range(i + k + 1, n) if ls[j] < ls[i]), None)
            if j is None:
                pend_lo.append(ls[i])
            elif cs[j] > ls[i] and j >= n - LIQ_RECENT and min(cs[j:]) >= ls[i]:
                sweeps.append({"side": "low", "level": ls[i], "ago": n - 1 - j})
    above = [c for c in cluster(pend_hi, tol) if c[0] > price][:2]
    below = [c for c in cluster(pend_lo, tol) if c[0] < price][::-1][:2]
    sweeps.sort(key=lambda s_: s_["ago"])
    return {"above": above, "below": below, "sweeps": sweeps[:2], "atr": atr}


def day_levels(bars):
    """Máximo y mínimo del día anterior (PDH/PDL), por día calendario de Colombia."""
    days = {}
    for t, c, h, l in bars:
        d = datetime.fromtimestamp(t, TZ).date()
        x = days.setdefault(d, [h, l])
        x[0], x[1] = max(x[0], h), min(x[1], l)
    ds = sorted(days)
    if len(ds) < 2:
        return None
    pdh, pdl = days[ds[-2]]
    return {"pdh": pdh, "pdl": pdl,
            "pdh_taken": days[ds[-1]][0] > pdh, "pdl_taken": days[ds[-1]][1] < pdl}


def h1_analysis(pair):
    if pair not in _H1_CACHE:
        bars = h1_history(pair)
        res = None
        if bars:
            price = bars[-1][1]
            res = {"price": price, "fib": fibonacci(bars, price),
                   "liq": liquidity(bars), "days": day_levels(bars)}
        _H1_CACHE[pair] = res
    return _H1_CACHE[pair]


# ---------- geopolítica (titulares por palabras clave) ----------
RSS = ["http://feeds.bbci.co.uk/news/world/rss.xml",
       "http://feeds.bbci.co.uk/news/business/rss.xml",
       "https://www.fxstreet.com/rss/news"]
RISK_OFF = ["war", "attack", "missile", "strike", "escalat", "sanction", "tariff", "nuclear",
            "invasion", "conflict", "crisis", "shutdown", "default", "tension", "threat",
            "bomb", "drone"]
RISK_ON = ["ceasefire", "truce", "peace", "de-escalat", "trade deal"]
THEMES = ["oil", "opec", "election", "inflation", "central bank", "interest rate",
          "fed ", "ecb", "boe", "boj", "rba"]
# Aversión al riesgo: refugios suben, divisas de riesgo bajan
TILT = {"JPY": 1.0, "CHF": 1.0, "USD": 0.5, "EUR": -0.25, "GBP": -0.5,
        "CAD": -0.5, "AUD": -1.0, "NZD": -1.0}


def has(words, text):
    return any(re.search(r"\b" + re.escape(w), text) for w in words)


def translate_es(text):
    """Traduce al español con el servicio gratuito de Google Translate (no oficial).
    Si falla, devuelve el titular original en inglés."""
    try:
        url = ("https://translate.googleapis.com/translate_a/single"
               "?client=gtx&sl=auto&tl=es&dt=t&q=" + urllib.parse.quote(text))
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        return out or text
    except Exception as e:
        print(f"Traducción fallida: {e}", file=sys.stderr)
        return text


def geopolitics():
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
    # La clasificación se hizo en inglés; solo se traduce lo que se muestra
    shown = [(ic, translate_es(t)) for ic, t in tagged[:8]]
    return intensity, shown, len(titles)


def geo_section():
    intensity, heads, total_heads = geopolitics()
    msg = {0: "Sin señales fuertes de aversión al riesgo en los titulares.",
           1: "Tensión moderada: favorece a JPY, CHF y USD frente a AUD, NZD, CAD y GBP.",
           2: "Aversión al riesgo ALTA: refugios (JPY, CHF, USD) con ventaja; AUD y NZD vulnerables."}[intensity]
    txt = "🌍 CONTEXTO GEOPOLÍTICO\n" + msg
    if heads:
        txt += "\nTitulares (🔻 riesgo, 🟢 distensión, 📰 tema de mercado):\n" + \
               "\n".join(f"{ic} {t}" for ic, t in heads)
    if total_heads == 0:
        txt += "\n(No se pudieron leer titulares; este bloque no se usó en los sesgos.)"
    txt += "\nLa lectura es por palabras clave: revisa tú mismo las noticias antes de operar."
    return intensity, txt


# ---------- bloques de texto ----------
def event_block(ev):
    rule = ev["_rule"]
    name = rule[1] if rule else ev["title"]
    lines = [
        f"🕐 {ev['_dt']:%H:%M} | {ES[ev['impact']]} | {ev['country']} — {name}",
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


def currency_scores(events):
    vol, score = defaultdict(int), defaultdict(float)
    for e in events:
        vol[e["country"]] += weight(e)
        score[e["country"]] += weight(e) * expected_bias(e)
    return vol, score


def rank_pairs(events):
    vol, score = currency_scores(events)
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
             "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURAUD", "EURCHF", "AUDNZD",
             "GBPCHF", "CADJPY", "EURCAD", "GBPAUD", "GBPCAD", "NZDJPY", "AUDCAD"]
    res = []
    for p in pairs:
        v = vol.get(p[:3], 0) + vol.get(p[3:], 0)
        if v >= 4:
            res.append((v, abs(score[p[:3]] - score[p[3:]]), p, score[p[:3]] - score[p[3:]]))
    res.sort(reverse=True)
    return res[:3]


def trigger_lines(pair, events, limit=3, with_day=False):
    """Línea por evento clave: qué hacer según el resultado."""
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


def pair_plan(pair, d, events):
    tilt = ("sesgo previo: COMPRA" if d > 0 else "sesgo previo: VENTA" if d < 0
            else "sin sesgo previo, opera la reacción")
    return "\n".join([f"▪️ {pair} ({tilt})"] + trigger_lines(pair, events))


RULES = (
    "⚠️ REGLAS DE EJECUCIÓN\n"
    "1. No abras operaciones 15 min antes de un evento 🔴.\n"
    "2. Espera el dato y una vela de 5 min cerrada a favor de la dirección.\n"
    "3. Si el dato sale en línea con el pronóstico, la reacción suele ser débil: no fuerces.\n"
    "4. Stop más allá del extremo de la vela del dato; objetivo mínimo 1:2.\n"
    "5. Riesgo máximo 1% por operación. Evita varios pares de la misma divisa a la vez.\n"
    "Esto es información, no asesoría financiera."
)


# ---------- modo: resumen diario ----------
def briefing():
    now = datetime.now(TZ)
    events = [e for e in load_events() if e["_dt"].date() == now.date()]
    head = f"📅 EVENTOS RELEVANTES — {DIAS[now.weekday()]} {now:%d/%m/%Y} ({TZ.key})"
    if not events:
        return head + "\n\nHoy no hay eventos relevantes. Día de baja volatilidad esperada."
    by_cur = defaultdict(list)
    for e in events:
        by_cur[e["country"]].append(e)
    parts = [head]
    for cur in sorted(by_cur, key=lambda c: -sum(weight(e) for e in by_cur[c])):
        parts.append(f"━━━ {cur} ━━━\n" + "\n\n".join(event_block(e) for e in by_cur[cur]))
    top = rank_pairs(events)
    if top:
        parts.append("🎯 MEJORES ESCENARIOS DEL DÍA\n" +
                     "\n\n".join(pair_plan(p, d, events) for _, _, p, d in top))
    else:
        parts.append("🎯 Ningún par reúne suficiente catalizador hoy. Esperar.")
    parts.append(RULES)
    return "\n\n".join(parts)


# ---------- modo: alertas ----------
def release_read(ev):
    a, f = num(ev.get("actual")), num(ev.get("forecast"))
    if a is None or f is None:
        return f"Actual: {ev.get('actual')} (no comparable con el pronóstico)"
    if a == f:
        return f"Actual {ev['actual']} = pronóstico → reacción débil, decide el precio"
    return f"Actual {ev['actual']} vs pronóstico {ev.get('forecast')} → {phrase(ev, a > f)}"


def alerts():
    now = datetime.now(TZ)
    msgs = []
    for e in load_events():
        if now + timedelta(minutes=10) < e["_dt"] <= now + timedelta(minutes=25):
            msgs.append(f"⏰ EN ~15 MIN\n{event_block(e)}\n   👉 No abras operaciones; "
                        f"define stop/objetivo antes del dato.")
        if e.get("actual") and now - timedelta(minutes=15) < e["_dt"] <= now:
            msgs.append(f"📢 DATO PUBLICADO — {e['country']} {e['title']}\n   {release_read(e)}\n"
                        f"   👉 Espera una vela de 5 min a favor antes de entrar.")
    return "\n\n".join(msgs)


# ---------- sesgos y escenarios (compartido por weekly y outlook) ----------
def evaluate(pair, tech, intensity, score):
    b, q = pair[:3], pair[3:]
    fund = max(-3.0, min(3.0, (score.get(b, 0) - score.get(q, 0)) / 3))
    geo = max(-3.0, min(3.0, intensity * (TILT.get(b, 0) - TILT.get(q, 0))))
    t = tech["score"] if tech else 0
    total = t + fund + geo
    if total >= 2:
        label = "📈 ALCISTA"
    elif total <= -2:
        label = "📉 BAJISTA"
    else:
        label = "↔️ NEUTRAL / RANGO"
    conf = "alta" if abs(total) >= 4 else "media" if abs(total) >= 2.5 else "baja"
    return {"pair": pair, "tech": t, "fund": round(fund, 1), "geo": round(geo, 1),
            "total": round(total, 1), "label": label, "conf": conf, "t": tech}


def fib_lines(p, total, f):
    """Escenario con retroceso de Fibonacci centrado en la zona 61.8%."""
    lv = {k: fmt(p, v) for k, v in f["lv"].items()}
    hi, lo = fmt(p, f["hi"]), fmt(p, f["lo"])
    zone = " 📍 EL PRECIO ESTÁ EN LA ZONA 61.8%" if 55 <= f["retr"] <= 70 else ""
    if f["leg"] == "up":
        head = (f"   Fibonacci: impulso alcista {lo}→{hi} (últimas {FIB_H1_BARS} velas H1). "
                f"Retroceso actual {f['retr']:.0f}% | 50%: {lv[0.5]} · 61.8%: {lv[0.618]} · "
                f"78.6%: {lv[0.786]}{zone}")
        if total >= 2:
            act = (f"   C) COMPRA en retroceso a la zona 61.8% ({lv[0.618]}) con vela de rechazo; "
                   f"stop bajo 78.6% ({lv[0.786]}); objetivo: máximo del impulso ({hi}).")
        elif total <= -2:
            act = (f"   C) Si cierra por debajo del 61.8% ({lv[0.618]}) → VENTA hacia 78.6% "
                   f"({lv[0.786]}) y el mínimo del impulso ({lo}).")
        else:
            act = f"   C) La zona 61.8% ({lv[0.618]}) es la reacción probable: soporte si el precio baja hasta ahí."
    else:
        head = (f"   Fibonacci: impulso bajista {hi}→{lo} (últimas {FIB_H1_BARS} velas H1). "
                f"Rebote actual {f['retr']:.0f}% | 50%: {lv[0.5]} · 61.8%: {lv[0.618]} · "
                f"78.6%: {lv[0.786]}{zone}")
        if total <= -2:
            act = (f"   C) VENTA en rebote a la zona 61.8% ({lv[0.618]}) con vela de rechazo; "
                   f"stop sobre 78.6% ({lv[0.786]}); objetivo: mínimo del impulso ({lo}).")
        elif total >= 2:
            act = (f"   C) Si cierra por encima del 61.8% ({lv[0.618]}) → COMPRA hacia 78.6% "
                   f"({lv[0.786]}) y el máximo del impulso ({hi}).")
        else:
            act = f"   C) La zona 61.8% ({lv[0.618]}) es la reacción probable: resistencia si el precio sube hasta ahí."
    return [head, act]


def liquidity_lines(p, total, h1):
    L, f, d, price = h1["liq"], h1["fib"], h1["days"], h1["price"]

    def dist(x):
        return f"{abs(x - price) / pip(p):.0f} pips"

    def fmt_levels(cl, tag):
        return " · ".join(f"{fmt(p, lv)} ({dist(lv)}" + (f", {tag} x{n}" if n > 1 else "") + ")"
                          for lv, n in cl) or "ninguna cercana"

    lines = [f"   Liquidez en H1 (~{LIQ_H1_BARS // 24} días):",
             f"   🔼 Pendiente arriba (BSL, liquidez compradora): {fmt_levels(L['above'], 'máx. iguales')}",
             f"   🔽 Pendiente abajo (SSL, liquidez vendedora): {fmt_levels(L['below'], 'mín. iguales')}"]
    if d:
        lines.append(f"   📅 Día anterior: PDH {fmt(p, d['pdh'])} ({'tomado' if d['pdh_taken'] else 'pendiente'})"
                     f" · PDL {fmt(p, d['pdl'])} ({'tomado' if d['pdl_taken'] else 'pendiente'})")
    for s_ in L["sweeps"]:
        if s_["side"] == "high":
            txt = (f"barrido de máximos (BSL) en {fmt(p, s_['level'])} hace {s_['ago']} h; cerró de vuelta "
                   f"abajo → rechazo BAJISTA")
            favor = total <= -2
        else:
            txt = (f"barrido de mínimos (SSL) en {fmt(p, s_['level'])} hace {s_['ago']} h; cerró de vuelta "
                   f"arriba → rechazo ALCISTA")
            favor = total >= 2
        tag = " ✅ a favor del sesgo" if favor else (" ⚠️ contra el sesgo" if abs(total) >= 2 else "")
        lines.append(f"   ⚡ Toma de liquidez: {txt}{tag}")
    if not L["sweeps"]:
        lines.append("   ⚡ Sin tomas de liquidez recientes con rechazo.")

    # Confluencia: 61.8% de Fibonacci cerca de liquidez pendiente
    if f:
        lv618 = f["lv"][0.618]
        near = [lv for lv, _ in L["above"] + L["below"] if abs(lv - lv618) <= 0.5 * L["atr"]]
        if near:
            lines.append(f"   ⭐ Confluencia: el 61.8% ({fmt(p, lv618)}) coincide con liquidez pendiente "
                         f"en {fmt(p, near[0])}. Zona de alta probabilidad de reacción.")

    up = L["above"][0][0] if L["above"] else None
    dn = L["below"][0][0] if L["below"] else None
    if total >= 2:
        tgt = f"{fmt(p, up)} ({dist(up)})" if up else "el máximo reciente"
        lines.append(f"   D) Objetivo en la liquidez pendiente de arriba: {tgt}. Entrada de mayor calidad: "
                     f"barrido de mínimos con cierre de vuelta arriba, idealmente desde la zona 61.8%.")
    elif total <= -2:
        tgt = f"{fmt(p, dn)} ({dist(dn)})" if dn else "el mínimo reciente"
        lines.append(f"   D) Objetivo en la liquidez pendiente de abajo: {tgt}. Entrada de mayor calidad: "
                     f"barrido de máximos con cierre de vuelta abajo, idealmente desde la zona 61.8%.")
    else:
        a = f"{fmt(p, up)} ({dist(up)})" if up else "-"
        b = f"{fmt(p, dn)} ({dist(dn)})" if dn else "-"
        lines.append(f"   D) Sin sesgo claro: el precio suele ir por la liquidez más cercana (arriba {a} / "
                     f"abajo {b}). Opera el barrido + reversión, no la ruptura.")
    return lines


def scenario_block(r, events, with_day=True):
    p, t = r["pair"], r["t"]
    lines = [f"▪️ {p} — {r['label']} (confianza {r['conf']}) | puntaje {r['total']:+} "
             f"[técnico {r['tech']:+} · fundamental {r['fund']:+} · geopolítico {r['geo']:+}]"]
    if t:
        lines.append(f"   Precio {fmt(p, t['price'])} | SMA20 {fmt(p, t['sma20'])} | "
                     f"Semana pasada: máx {fmt(p, t['hi'])} · mín {fmt(p, t['lo'])}")
        if r["total"] >= 2:
            lines += [f"   A) Continuación: cierre diario sobre {fmt(p, t['hi'])} → COMPRA; "
                      f"stop bajo SMA20 ({fmt(p, t['sma20'])}); objetivo mínimo 1:2.",
                      f"   B) Retroceso: toque de SMA20 ({fmt(p, t['sma20'])}) con vela de rechazo → COMPRA; "
                      f"stop bajo {fmt(p, t['lo'])}.",
                      f"   ✖ Invalidación: cierre diario bajo {fmt(p, t['lo'])} → anular el sesgo."]
        elif r["total"] <= -2:
            lines += [f"   A) Continuación: cierre diario bajo {fmt(p, t['lo'])} → VENTA; "
                      f"stop sobre SMA20 ({fmt(p, t['sma20'])}); objetivo mínimo 1:2.",
                      f"   B) Rebote: toque de SMA20 ({fmt(p, t['sma20'])}) con vela de rechazo → VENTA; "
                      f"stop sobre {fmt(p, t['hi'])}.",
                      f"   ✖ Invalidación: cierre diario sobre {fmt(p, t['hi'])} → anular el sesgo."]
        else:
            lines += [f"   Rango probable entre {fmt(p, t['lo'])} y {fmt(p, t['hi'])}: "
                      f"vender cerca del máximo / comprar cerca del mínimo con confirmación.",
                      f"   Ruptura con cierre diario fuera del rango → seguir la ruptura."]
    else:
        lines.append("   (sin datos de precio diarios: solo se usó fundamental y geopolítica)")
    h1 = h1_analysis(p)
    if h1:
        if h1["fib"]:
            lines += fib_lines(p, r["total"], h1["fib"])
        lines += liquidity_lines(p, r["total"], h1)
    else:
        lines.append("   (sin velas H1: no hay Fibonacci ni liquidez para este par)")
    trig = trigger_lines(p, events, limit=3, with_day=with_day)
    if trig:
        lines.append("   Gatillos de datos:")
        lines += trig
    return "\n".join(lines)


def bias_rows(results):
    return "\n".join(f"{r['pair']}  {r['label']} ({r['conf']})  {r['total']:+}  "
                     f"[téc {r['tech']:+} · fund {r['fund']:+} · geo {r['geo']:+}]" for r in results)


LEGEND = ("ALCISTA = sube la divisa base frente a la cotizada. Puntaje: técnico (precio vs medias, "
          "semana pasada) + fundamental (eventos de las divisas) + geopolítico. Fibonacci y liquidez se calculan en H1.")


def choose_pairs(results):
    chosen = [r for r in results if r["pair"] in FOCUS]
    for r in results:
        if r not in chosen and abs(r["total"]) >= 2 and len(chosen) < len(FOCUS) + 3:
            chosen.append(r)
    return chosen


# ---------- modo: plan semanal (domingo) ----------
def weekly():
    now = datetime.now(TZ)
    start = ((now + timedelta(days=1)) if now.weekday() == 6
             else (now + timedelta(days=7 - now.weekday()))).date()
    end = start + timedelta(days=4)
    events = [e for e in load_events(("this", "next")) if start <= e["_dt"].date() <= end]
    parts = [f"📆 PLAN SEMANAL — {start:%d/%m} al {end:%d/%m/%Y} ({TZ.key})"]

    intensity, geo_txt = geo_section()
    parts.append(geo_txt)

    if events:
        by_day = defaultdict(list)
        for e in events:
            by_day[e["_dt"].date()].append(e)
        agenda = ["🗓 AGENDA CLAVE DE LA SEMANA"]
        for d in sorted(by_day):
            top = sorted(by_day[d], key=lambda e: (-weight(e), e["_dt"]))[:8]
            rows = [f"   {e['_dt']:%H:%M} {'🔴' if e['impact'] == 'High' else '🟠'} {e['country']} "
                    f"{e['title']} (prev {e.get('previous') or '-'} | pron {e.get('forecast') or '-'})"
                    for e in sorted(top, key=lambda e: e["_dt"])]
            agenda.append(f"{DIAS[d.weekday()]} {d:%d/%m}\n" + "\n".join(rows))
        parts += ["\n".join(agenda[:2])] + agenda[2:]
    else:
        parts.append("🗓 No se encontraron eventos relevantes para la semana "
                     "(el calendario de la próxima semana puede no estar publicado aún).")

    _, score = currency_scores(events)
    results = [evaluate(p, technicals(p), intensity, score) for p in PAIRS]
    results.sort(key=lambda r: -abs(r["total"]))
    parts.append("🧭 SESGOS SEMANALES (dirección del par)\n" + bias_rows(results) + "\n" + LEGEND)
    parts.append("🎯 ESCENARIOS LISTOS (SEMANA)")
    parts += [scenario_block(r, events, with_day=True) for r in choose_pairs(results)]
    parts.append(RULES)
    return "\n\n".join(parts)


# ---------- modo: sesgos y escenarios del día siguiente (9 pm) ----------
def next_trading_day(now):
    d = now.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def outlook():
    now = datetime.now(TZ)
    nd = next_trading_day(now)
    monday = (now - timedelta(days=now.weekday())).date()
    week_end = monday + timedelta(days=4)
    allev = load_events(("this", "next"))

    past = [e for e in allev if monday <= e["_dt"].date() and e["_dt"] <= now and e.get("actual")]
    future = [e for e in allev if e["_dt"] > now and e["_dt"].date() <= week_end]
    next_ev = [e for e in future if e["_dt"].date() <= nd]

    parts = [f"🌙 SESGOS Y ESCENARIOS PARA {DIAS[nd.weekday()].upper()} {nd:%d/%m/%Y} ({TZ.key})"]

    intensity, geo_txt = geo_section()
    parts.append(geo_txt)

    # Datos ya publicados esta semana (si el feed trae el valor real)
    if past:
        parts.append("📊 DATOS YA PUBLICADOS ESTA SEMANA\n" + "\n".join(
            f"   {DIAS[e['_dt'].weekday()]} {e['_dt']:%H:%M} {e['country']} {e['title']}: "
            f"{release_read(e)}" for e in past[-10:]))
    else:
        parts.append("📊 DATOS PUBLICADOS: el calendario gratuito no incluye los valores reales; "
                     "el sesgo recoge su efecto a través del comportamiento del precio.")

    # Agenda de mañana
    if next_ev:
        rows = [f"   {DIAS[e['_dt'].weekday()]} {e['_dt']:%H:%M} "
                f"{'🔴' if e['impact'] == 'High' else '🟠'} {e['country']} {e['title']} "
                f"(prev {e.get('previous') or '-'} | pron {e.get('forecast') or '-'})"
                for e in sorted(next_ev, key=lambda e: e["_dt"])[:14]]
        parts.append("🗓 PRÓXIMAS HORAS (hasta el cierre de mañana)\n" + "\n".join(rows))
    else:
        parts.append("🗓 Sin eventos relevantes hasta el cierre de mañana: día de baja volatilidad esperada.")

    # Puntaje fundamental acumulado: sorpresas reales + expectativas (mañana pesa el doble)
    score = defaultdict(float)
    for e in past:
        score[e["country"]] += weight(e) * surprise(e)
    for e in future:
        mult = 2 if e["_dt"].date() <= nd else 1
        score[e["country"]] += mult * weight(e) * expected_bias(e)

    results = [evaluate(p, technicals(p), intensity, score) for p in PAIRS]
    results.sort(key=lambda r: -abs(r["total"]))
    parts.append("🧭 SESGOS PARA MAÑANA (dirección del par)\n" + bias_rows(results) + "\n" + LEGEND)
    parts.append("🎯 ESCENARIOS LISTOS (MAÑANA)")
    parts += [scenario_block(r, next_ev, with_day=False) for r in choose_pairs(results)]
    parts.append(RULES)
    return "\n\n".join(parts)


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
    for c in out:  # seguridad: ningún trozo supera el límite de Telegram
        final += [c[i:i + 4000] for i in range(0, len(c), 4000)]
    return final


def send_telegram(text):
    """TELEGRAM_CHAT_ID admite varios destinos separados por coma.
    Para enviar a un TEMA (pestaña) de un grupo con temas: -1001234567890:5
    (id del grupo, dos puntos, id del tema)."""
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


def save(name, text):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        f.write(text)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "briefing"
    now = datetime.now(TZ)
    # Sin mensajes diarios en fin de semana (el plan semanal del domingo sí se envía)
    if mode in ("briefing", "alerts") and now.weekday() >= 5 and not FORCE:
        print("Fin de semana: no se envía resumen ni alertas diarias.")
        return
    # El análisis del día siguiente solo de lunes a jueves por la noche
    if mode == "outlook" and now.weekday() >= 4 and not FORCE:
        print("Viernes a domingo: no se envía el análisis del día siguiente.")
        return
    if mode == "alerts":
        text = alerts()
    elif mode == "weekly":
        text = weekly()
        save(f"semana-{now:%Y-%m-%d}.txt", text)
    elif mode == "outlook":
        text = outlook()
        save(f"dia-{now:%Y-%m-%d}.txt", text)
    else:
        text = briefing()
        save(f"{now:%Y-%m-%d}.txt", text)
    if text:
        print(text)
        send_telegram(text)


if __name__ == "__main__":
    main()
