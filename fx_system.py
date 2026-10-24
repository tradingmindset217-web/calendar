#!/usr/bin/env python3
"""
Reporte diario de eventos económicos por divisa.
- Descarga el calendario económico (feed público de Forex Factory / Faireconomy)
- Filtra los eventos de HOY (zona horaria configurable)
- Muestra importancia, escenario alcista/bajista por divisa y los pares con mejor potencial
- Guarda reports/AAAA-MM-DD.md y opcionalmente envía a Telegram

Solo usa la librería estándar (Python 3.9+).
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("REPORT_TZ", "America/Bogota"))
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
OUT_DIR = os.getenv("REPORT_DIR", "reports")

IMPACT_W = {"High": 3, "Medium": 2, "Low": 1}
IMPACT_ES = {"High": "🔴 Alta", "Medium": "🟠 Media", "Low": "🟡 Baja"}

# Indicadores donde un valor MÁS ALTO es negativo para la divisa
INVERTED = ("unemployment", "jobless", "claimant", "claims")

PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURAUD", "EURCHF", "AUDNZD",
    "GBPCHF", "CADJPY", "EURCAD", "GBPAUD", "NZDJPY", "CHFJPY",
]


def num(s):
    """Convierte '0.3%', '215K', '-1.2M', '<0.5' en float. None si no se puede."""
    if s is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(s))
    return float(m.group().replace(",", ".")) if m else None


def is_inverted(title):
    t = title.lower()
    return any(k in t for k in INVERTED)


def expected_bias(ev):
    """+1 si se espera mejora vs dato previo, -1 si empeora, 0 si no hay datos."""
    f, p = num(ev.get("forecast")), num(ev.get("previous"))
    if f is None or p is None or f == p:
        return 0
    sign = 1 if f > p else -1
    return -sign if is_inverted(ev["title"]) else sign


def scenario(ev):
    cur = ev["country"]
    if is_inverted(ev["title"]):
        return f"Dato > pronóstico → {cur} ↓ | Dato < pronóstico → {cur} ↑"
    return f"Dato > pronóstico → {cur} ↑ | Dato < pronóstico → {cur} ↓"


def fetch_events():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def todays_events(raw):
    today = datetime.now(TZ).date()
    out = []
    for ev in raw:
        try:
            dt = datetime.fromisoformat(ev["date"]).astimezone(TZ)
        except (KeyError, ValueError):
            continue
        if dt.date() == today and ev.get("impact") in IMPACT_W:
            ev["_dt"] = dt
            out.append(ev)
    return sorted(out, key=lambda e: e["_dt"])


def rank_pairs(vol, score):
    ranked = []
    for p in PAIRS:
        b, q = p[:3], p[3:]
        v = vol.get(b, 0) + vol.get(q, 0)
        if v < 3:  # necesita al menos un evento relevante
            continue
        d = score.get(b, 0) - score.get(q, 0)
        ranked.append((abs(d), v, p, d))
    ranked.sort(reverse=True)
    return ranked[:5]


def build_report(events):
    now = datetime.now(TZ)
    L = [f"# 📅 Calendario económico — {now:%A %d/%m/%Y} ({TZ.key})", ""]
    if not events:
        L.append("Sin eventos relevantes hoy (fin de semana o feriado).")
        return "\n".join(L)

    by_cur = defaultdict(list)
    vol, score = defaultdict(int), defaultdict(int)
    for ev in events:
        by_cur[ev["country"]].append(ev)
        w = IMPACT_W[ev["impact"]]
        vol[ev["country"]] += w
        score[ev["country"]] += w * expected_bias(ev)

    # Divisas ordenadas por volatilidad esperada
    for cur in sorted(by_cur, key=lambda c: -vol[c]):
        s = score[cur]
        tone = "🟢 sesgo positivo" if s > 0 else "🔴 sesgo negativo" if s < 0 else "⚪ neutral / sin sesgo claro"
        L += [f"## {cur} — volatilidad {vol[cur]} | {tone}", ""]
        L += ["| Hora | Evento | Importancia | Prev. | Pronóst. | Expectativa | Escenario |",
              "|---|---|---|---|---|---|---|"]
        for ev in by_cur[cur]:
            b = expected_bias(ev)
            exp = "🟢 mejora" if b > 0 else "🔴 empeora" if b < 0 else "⚪ n/d"
            L.append(
                f"| {ev['_dt']:%H:%M} | {ev['title']} | {IMPACT_ES[ev['impact']]} | "
                f"{ev.get('previous') or '-'} | {ev.get('forecast') or '-'} | {exp} | {scenario(ev)} |"
            )
        L.append("")

    L += ["## 🎯 Pares con mejor potencial hoy", ""]
    top = rank_pairs(vol, score)
    if not top:
        L.append("Ningún par tiene volatilidad suficiente hoy. Mejor esperar.")
    for _, v, p, d in top:
        if d > 0:
            idea = f"sesgo COMPRA (fortaleza {p[:3]} vs {p[3:]})"
        elif d < 0:
            idea = f"sesgo VENTA (debilidad {p[:3]} vs fortaleza {p[3:]})"
        else:
            idea = "sin sesgo direccional: operar la reacción tras el dato"
        L.append(f"- **{p}** — volatilidad {v} — {idea}")

    L += [
        "",
        "## ⚠️ Reglas de gestión",
        "- El sesgo se calcula con *pronóstico vs dato previo*; el efecto real se decide cuando sale el dato.",
        "- Evita abrir operaciones 15 min antes de eventos de impacto Alto; opera la reacción o espera confirmación.",
        "- Riesgo máximo por operación: 1% de la cuenta, con stop loss siempre.",
        "- Esto es información, no asesoría financiera.",
    ]
    return "\n".join(L)


def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    for i in range(0, len(text), 3800):
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i + 3800]}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30
        )


def main():
    try:
        events = todays_events(fetch_events())
    except Exception as e:  # red caída, rate limit, etc.
        print(f"Error descargando el calendario: {e}", file=sys.stderr)
        sys.exit(1)
    report = build_report(events)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{datetime.now(TZ):%Y-%m-%d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    send_telegram(report)


if __name__ == "__main__":
    main()
