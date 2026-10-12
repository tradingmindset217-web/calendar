#!/usr/bin/env python3
"""
Sistema de eventos económicos para Forex.

Modos:
  python fx_system.py briefing  -> resumen del día (solo lunes a viernes)
  python fx_system.py alerts    -> avisos previos y lectura de datos (solo lunes a viernes)
  python fx_system.py weekly    -> plan semanal: sesgos, geopolítica y escenarios (domingo)

Variables opcionales:
  FORCE=1          ejecuta aunque sea fin de semana (para pruebas)
  PAIRS=...        pares a evaluar en el plan semanal, separados por coma
  FOCUS_PAIRS=...  pares que SIEMPRE aparecen con escenarios (por defecto GBPAUD)

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
    f, p = num(ev.get("forecast")), num(ev.get("previous"))
    if f is None or p is None or f == p:
        return 0
    sign = 1 if f > p else -1
    return -sign if inv(ev) else sign


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


# ---------- modo: plan semanal ----------
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


def scenario_block(r, events):
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
        lines.append("   (sin datos de precio: solo se usó fundamental y geopolítica)")
    trig = trigger_lines(p, events, limit=3, with_day=True)
    if trig:
        lines.append("   Gatillos de datos de la semana:")
        lines += trig
    return "\n".join(lines)


def weekly():
    now = datetime.now(TZ)
    start = ((now + timedelta(days=1)) if now.weekday() == 6
             else (now + timedelta(days=7 - now.weekday()))).date()
    end = start + timedelta(days=4)
    events = [e for e in load_events(("this", "next")) if start <= e["_dt"].date() <= end]
    parts = [f"📆 PLAN SEMANAL — {start:%d/%m} al {end:%d/%m/%Y} ({TZ.key})"]

    # 1. Geopolítica
    intensity, heads, total_heads = geopolitics()
    msg = {0: "Sin señales fuertes de aversión al riesgo en los titulares.",
           1: "Tensión moderada: favorece a JPY, CHF y USD frente a AUD, NZD, CAD y GBP.",
           2: "Aversión al riesgo ALTA: refugios (JPY, CHF, USD) con ventaja; AUD y NZD vulnerables."}[intensity]
    geo_txt = "🌍 CONTEXTO GEOPOLÍTICO\n" + msg
    if heads:
        geo_txt += "\nTitulares (🔻 riesgo, 🟢 distensión, 📰 tema de mercado):\n" + \
                   "\n".join(f"{ic} {t}" for ic, t in heads)
    if total_heads == 0:
        geo_txt += "\n(No se pudieron leer titulares; este bloque no se usó en los sesgos.)"
    geo_txt += "\nLa lectura es por palabras clave: revisa tú mismo las noticias antes de operar."
    parts.append(geo_txt)

    # 2. Agenda
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
        parts += ["\n".join(agenda[:1] + [agenda[1]])] + agenda[2:]
    else:
        parts.append("🗓 No se encontraron eventos relevantes para la semana "
                     "(el calendario de la próxima semana puede no estar publicado aún).")

    # 3. Sesgos
    _, score = currency_scores(events)
    results = [evaluate(p, technicals(p), intensity, score) for p in PAIRS]
    results.sort(key=lambda r: -abs(r["total"]))
    rows = [f"{r['pair']}  {r['label']} ({r['conf']})  {r['total']:+}  "
            f"[téc {r['tech']:+} · fund {r['fund']:+} · geo {r['geo']:+}]" for r in results]
    parts.append("🧭 SESGOS SEMANALES (dirección del par)\n" + "\n".join(rows) +
                 "\nALCISTA = sube la divisa base frente a la cotizada. "
                 "Puntaje: técnico (precio vs medias y semana pasada) + fundamental "
                 "(pronóstico vs previo) + geopolítico.")

    # 4. Escenarios
    chosen = [r for r in results if r["pair"] in FOCUS]
    for r in results:
        if r not in chosen and abs(r["total"]) >= 2 and len(chosen) < len(FOCUS) + 3:
            chosen.append(r)
    parts.append("🎯 ESCENARIOS LISTOS")
    parts += [scenario_block(r, events) for r in chosen]
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
    token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
    chats = [c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
    if not text:
        return
    if not (token and chats):
        print("ERROR: los secretos TELEGRAM_TOKEN o TELEGRAM_CHAT_ID llegan vacios", file=sys.stderr)
        return
    for chat in chats:
        for part in chunks(text):
            data = urllib.parse.urlencode({"chat_id": chat, "text": part}).encode()
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                       data=data, timeout=30)
                print(f"Telegram: enviado a {chat}")
            except Exception as e:
                cuerpo = e.read().decode() if hasattr(e, "read") else ""
                print(f"Telegram ERROR ({chat}): {e} {cuerpo}", file=sys.stderr)


def save(name, text):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        f.write(text)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "briefing"
    now = datetime.now(TZ)
    if mode in ("briefing", "alerts") and now.weekday() >= 5 and not FORCE:
        print("Fin de semana: no se envía resumen ni alertas diarias.")
        return
    if mode == "alerts":
        text = alerts()
    elif mode == "weekly":
        text = weekly()
        save(f"semana-{now:%Y-%m-%d}.txt", text)
    else:
        text = briefing()
        save(f"{now:%Y-%m-%d}.txt", text)
    if text:
        print(text)
        send_telegram(text)


if __name__ == "__main__":
    main()
