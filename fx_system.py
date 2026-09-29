#!/usr/bin/env python3
"""
Sistema de eventos económicos para Forex.

Modos:
  python fx_system.py briefing   -> resumen del día (correr a las 00:00)
  python fx_system.py alerts     -> avisos 10-25 min antes de cada evento relevante
                                    y lectura de los datos recién publicados
                                    (correr cada 15 min)

Envía a Telegram si existen TELEGRAM_TOKEN y TELEGRAM_CHAT_ID.
Solo usa la librería estándar (Python 3.9+).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("REPORT_TZ", "America/Bogota"))
FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
OUT_DIR = os.getenv("REPORT_DIR", "reports")

W = {"High": 3, "Medium": 2, "Low": 1}
ES = {"High": "🔴 ALTA", "Medium": "🟠 MEDIA", "Low": "🟡 BAJA"}

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


# ---------- datos ----------
def load_events():
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(FEED, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = json.load(r)
            break
        except Exception as e:  # límite de peticiones o red
            last = e
            time.sleep(20)
    else:
        print(f"No se pudo descargar el calendario: {last}", file=sys.stderr)
        sys.exit(1)
    out = []
    for e in raw:
        if e.get("impact") not in W:
            continue
        try:
            e["_dt"] = datetime.fromisoformat(e["date"]).astimezone(TZ)
        except (KeyError, ValueError):
            continue
        e["_rule"] = match_rule(e["title"])
        if relevant(e):
            out.append(e)
    return sorted(out, key=lambda e: e["_dt"])


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


def rank_pairs(events):
    vol, score = defaultdict(int), defaultdict(int)
    for e in events:
        vol[e["country"]] += weight(e)
        score[e["country"]] += weight(e) * expected_bias(e)
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


def pair_plan(pair, d, events):
    b, q = pair[:3], pair[3:]
    tilt = ("sesgo previo: COMPRA" if d > 0 else "sesgo previo: VENTA" if d < 0
            else "sin sesgo previo, opera la reacción")
    lines = [f"▪️ {pair} ({tilt})"]
    for cur in (b, q):
        evs = [e for e in events if e["country"] == cur]
        if not evs:
            continue
        e = max(evs, key=weight)
        good = "menor" if inv(e) else "mayor"
        bad = "mayor" if inv(e) else "menor"
        act_good, act_bad = ("COMPRA", "VENTA") if cur == b else ("VENTA", "COMPRA")
        lines.append(f"   {e['_dt']:%H:%M} {e['title']}: dato {good} que el pronóstico "
                     f"→ {act_good} {pair} | dato {bad} → {act_bad} {pair}")
    return "\n".join(lines)


RULES = (
    "⚠️ REGLAS DE EJECUCIÓN\n"
    "1. No abras operaciones 15 min antes de un evento 🔴.\n"
    "2. Espera el dato y una vela de 5 min cerrada a favor de la dirección.\n"
    "3. Si el dato sale en línea con el pronóstico, la reacción suele ser débil: no fuerces.\n"
    "4. Stop más allá del extremo de la vela del dato; objetivo mínimo 1:2.\n"
    "5. Riesgo máximo 1% por operación. Evita varios pares de la misma divisa a la vez.\n"
    "Esto es información, no asesoría financiera."
)


# ---------- modos ----------
def briefing():
    now = datetime.now(TZ)
    events = [e for e in load_events() if e["_dt"].date() == now.date()]
    head = f"📅 EVENTOS RELEVANTES — {now:%A %d/%m/%Y} ({TZ.key})"
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


def release_read(ev):
    a, f = num(ev.get("actual")), num(ev.get("forecast"))
    if a is None or f is None:
        return f"Actual: {ev.get('actual')} (no comparable con el pronóstico)"
    if a == f:
        return f"Actual {ev['actual']} = pronóstico → reacción débil, decide el precio"
    return f"Actual {ev['actual']} vs pronóstico {ev.get('forecast')} → {phrase(ev, a > f)}"


def alerts():
    now = datetime.now(TZ)
    events = load_events()
    msgs = []
    for e in events:
        if now + timedelta(minutes=10) < e["_dt"] <= now + timedelta(minutes=25):
            msgs.append(f"⏰ EN ~15 MIN\n{event_block(e)}\n   👉 No abras operaciones; "
                        f"define stop/objetivo antes del dato.")
        if e.get("actual") and now - timedelta(minutes=15) < e["_dt"] <= now:
            msgs.append(f"📢 DATO PUBLICADO — {e['country']} {e['title']}\n   {release_read(e)}\n"
                        f"   👉 Espera una vela de 5 min a favor antes de entrar.")
    return "\n\n".join(msgs)


def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat) or not text:
        return
    for i in range(0, len(text), 3800):
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i + 3800]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                               data=data, timeout=30)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "briefing"
    if mode == "alerts":
        text = alerts()
    else:
        text = briefing()
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, f"{datetime.now(TZ):%Y-%m-%d}.txt"), "w",
                  encoding="utf-8") as f:
            f.write(text)
    if text:
        print(text)
        send_telegram(text)


if __name__ == "__main__":
    main()
