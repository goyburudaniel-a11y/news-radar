#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radar de Noticias
-----------------
1) Junta noticias de Google News (filtrado a Paraguay) + RSS directos.
2) Usa Claude para agruparlas en "hilos" temáticos y, para cada uno,
   redactar el CONTEXTO/ORIGEN (para entender de dónde salió el tema)
   y lo que pasó HOY. Recuerda hilos de días anteriores.
3) Genera un sitio web (carpeta docs/) que GitHub Pages publica y
   podés abrir desde cualquier dispositivo.

Se corre una vez por día vía GitHub Actions. También podés correrlo
a mano: python run.py
"""

import os
import re
import json
import html
import unicodedata
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import yaml
import feedparser
from dateutil import parser as dateparser

# ---------- rutas ----------
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "state", "threads.json")
DOCS_DIR = os.path.join(BASE, "docs")
CONFIG_PATH = os.path.join(BASE, "config.yaml")

# zona horaria de Paraguay (aprox, sin DST para simplificar el sello de fecha)
TZ_PY = timezone(timedelta(hours=-3))


# =====================================================================
# 1. RECOLECCIÓN
# =====================================================================
def cargar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def gnews_url(query):
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=es-419&gl=PY&ceid=PY:es-419"


def _norm(txt):
    """Normaliza texto para deduplicar titulares."""
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    txt = re.sub(r"\s+", " ", txt.lower()).strip()
    return txt


def recolectar(cfg):
    ventana = timedelta(hours=cfg.get("horas_de_ventana", 30))
    ahora = datetime.now(timezone.utc)
    vistos = set()
    items = []

    feeds = [("gnews", gnews_url(q)) for q in cfg.get("google_news_queries", [])]
    for d in cfg.get("direct_feeds") or []:
        feeds.append((d.get("nombre", "RSS"), d["url"]))

    for etiqueta, url in feeds:
        try:
            f = feedparser.parse(url)
        except Exception as e:
            print(f"  ! error en feed {etiqueta}: {e}")
            continue
        for e in f.entries[: cfg.get("max_items_por_query", 25)]:
            titulo = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not titulo or not link:
                continue
            # fecha
            pub = None
            for campo in ("published", "updated"):
                if e.get(campo):
                    try:
                        pub = dateparser.parse(e[campo])
                        break
                    except Exception:
                        pass
            if pub and pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if pub and (ahora - pub) > ventana:
                continue  # nota vieja, la salteamos
            # dedupe
            clave = _norm(titulo)[:90]
            if clave in vistos:
                continue
            vistos.add(clave)
            # fuente
            medio = ""
            if e.get("source") and isinstance(e["source"], dict):
                medio = e["source"].get("title", "")
            if not medio:
                # google news suele poner " - Medio" al final del título
                m = re.search(r" - ([^-]+)$", titulo)
                medio = m.group(1).strip() if m else etiqueta
            resumen = re.sub(r"<[^>]+>", "", e.get("summary", ""))[:300]
            items.append({
                "titular": titulo,
                "medio": medio,
                "url": link,
                "resumen": resumen,
                "fecha": pub.isoformat() if pub else "",
            })
    print(f"  recolectadas {len(items)} notas únicas")
    return items


# =====================================================================
# 2. AGRUPAMIENTO + CONTEXTO (Claude)
# =====================================================================
def cargar_estado():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"threads": {}}


def guardar_estado(estado):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def memoria_reciente(estado, dias):
    """Resumen compacto de hilos recientes para pasarle al modelo."""
    corte = datetime.now(TZ_PY) - timedelta(days=dias)
    memo = []
    for tid, t in estado.get("threads", {}).items():
        try:
            last = dateparser.parse(t.get("last_seen", ""))
        except Exception:
            last = None
        if last and last.replace(tzinfo=None) < corte.replace(tzinfo=None):
            continue
        memo.append({
            "id": tid,
            "titulo": t.get("titulo", ""),
            "categoria": t.get("categoria", ""),
            "contexto": t.get("contexto", "")[:400],
            "ultima_fecha": t.get("last_seen", ""),
        })
    return memo


PROMPT = """Sos el editor de un boletín de noticias para el gerente general de una desarrolladora inmobiliaria en Paraguay. Tu trabajo es tomar los titulares de hoy y organizarlos en HILOS temáticos.

REGLAS:
- Agrupá los titulares que hablan del mismo tema en un solo hilo.
- Para cada hilo escribí un campo "contexto": el TRASFONDO / ORIGEN del tema, de forma que alguien que no siguió las noticias anteriores entienda de dónde salió. Este es el campo más importante.
- Escribí un campo "hoy": qué novedad concreta hubo hoy. Con TUS PROPIAS PALABRAS; no copies los titulares textualmente.
- Si un hilo continúa un tema que ya existe en la MEMORIA, reutilizá su mismo "id" y actualizá el contexto sumando lo nuevo.
- Si es un tema nuevo, inventá un id corto en minúsculas con guiones (ej: "tasa-interes-bcp").
- Asigná "categoria": una de [Inmobiliario, Economía, Regulación, Política, Otros].
- "estado": "en curso" si continúa un hilo de la memoria, "nuevo" si no.
- Priorizá lo relevante para inmobiliario, construcción, economía y regulación, pero incluí también noticias importantes del país.
- Ignorá titulares irrelevantes, duplicados o puro clickbait.
- Cada hilo debe incluir sus "fuentes" (los titulares originales con medio y url).

Devolvé SOLO un JSON válido, sin texto adicional ni ```:
{
  "threads": [
    {
      "id": "...",
      "titulo": "...",
      "categoria": "...",
      "estado": "nuevo|en curso",
      "contexto": "...",
      "hoy": "...",
      "fuentes": [{"titular":"...","medio":"...","url":"..."}]
    }
  ]
}

=== MEMORIA (hilos recientes) ===
{memoria}

=== TITULARES DE HOY ===
{titulares}
"""


def extraer_json(texto):
    texto = texto.strip()
    texto = re.sub(r"^```(json)?", "", texto).strip()
    texto = re.sub(r"```$", "", texto).strip()
    i, j = texto.find("{"), texto.rfind("}")
    if i >= 0 and j > i:
        texto = texto[i:j + 1]
    return json.loads(texto)


def agrupar(items, estado, cfg):
    from anthropic import Anthropic

    memo = memoria_reciente(estado, cfg.get("dias_de_memoria", 21))
    titulares = [
        {"titular": it["titular"], "medio": it["medio"], "url": it["url"], "resumen": it["resumen"]}
        for it in items
    ]
    prompt = PROMPT.replace("{memoria}", json.dumps(memo, ensure_ascii=False)) \
                   .replace("{titulares}", json.dumps(titulares, ensure_ascii=False))

    client = Anthropic()  # usa ANTHROPIC_API_KEY del entorno
    resp = client.messages.create(
        model=cfg.get("modelo", "claude-sonnet-5"),
        max_tokens=cfg.get("max_tokens", 8000),
        messages=[{"role": "user", "content": prompt}],
    )
    texto = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = extraer_json(texto)
    return data.get("threads", [])


def fusionar(estado, threads_hoy):
    """Actualiza la memoria con los hilos de hoy."""
    hoy = datetime.now(TZ_PY).strftime("%Y-%m-%d")
    for t in threads_hoy:
        tid = t["id"]
        prev = estado["threads"].get(tid)
        update = {"date": hoy, "hoy": t.get("hoy", ""), "fuentes": t.get("fuentes", [])}
        if prev:
            prev["titulo"] = t.get("titulo", prev["titulo"])
            prev["categoria"] = t.get("categoria", prev.get("categoria", ""))
            prev["contexto"] = t.get("contexto", prev["contexto"])
            prev["last_seen"] = hoy
            # evitar duplicar la actualización del mismo día
            prev["updates"] = [u for u in prev.get("updates", []) if u["date"] != hoy]
            prev["updates"].append(update)
        else:
            estado["threads"][tid] = {
                "titulo": t.get("titulo", ""),
                "categoria": t.get("categoria", "Otros"),
                "contexto": t.get("contexto", ""),
                "first_seen": hoy,
                "last_seen": hoy,
                "updates": [update],
            }
    return estado


# =====================================================================
# 3. GENERACIÓN DEL SITIO
# =====================================================================
CSS = """
:root{--bg:#0f1115;--card:#1a1d24;--line:#2a2f3a;--tx:#e7e9ee;--mut:#9aa3b2;--acc:#4f9cf9;--tag:#242a36}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:16px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:24px 18px 80px}
h1{font-size:22px;margin:0 0 2px}.sub{color:var(--mut);font-size:13px;margin-bottom:24px}
.cat{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);
margin:28px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;margin-bottom:14px}
.tt{font-size:17px;font-weight:600;margin:0 0 6px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--tag);color:var(--mut)}
.badge.en{color:#f5c451}.hoy{margin:6px 0}
details{margin-top:8px}summary{cursor:pointer;color:var(--acc);font-size:14px}
.ctx{color:var(--mut);font-size:14.5px;margin:8px 0 6px;border-left:2px solid var(--line);padding-left:12px}
.time{margin:8px 0}.time .d{font-size:12px;color:var(--mut)}
.srcs{list-style:none;padding:0;margin:8px 0 0}
.srcs li{margin:4px 0;font-size:14px}.srcs a{color:var(--tx);text-decoration:none}
.srcs a:hover{color:var(--acc)}.srcs .m{color:var(--mut);font-size:12px}
.empty{color:var(--mut);padding:40px 0;text-align:center}
"""

ORDEN_CAT = ["Inmobiliario", "Economía", "Regulación", "Política", "Otros"]


def esc(s):
    return html.escape(s or "")


def generar_sitio(threads_hoy, estado, cfg):
    os.makedirs(DOCS_DIR, exist_ok=True)
    fecha = datetime.now(TZ_PY).strftime("%A %d de %B de %Y, %H:%M")

    por_cat = {}
    for t in threads_hoy:
        por_cat.setdefault(t.get("categoria", "Otros"), []).append(t)

    partes = [f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(cfg.get('titulo_sitio','Radar de Noticias'))}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>{esc(cfg.get('titulo_sitio','Radar de Noticias'))}</h1>
<div class="sub">Actualizado: {esc(fecha)} · {sum(len(v) for v in por_cat.values())} hilos</div>"""]

    if not threads_hoy:
        partes.append('<div class="empty">Hoy no hubo novedades relevantes.</div>')

    for cat in ORDEN_CAT:
        hilos = por_cat.get(cat)
        if not hilos:
            continue
        partes.append(f'<div class="cat">{esc(cat)}</div>')
        for t in hilos:
            tid = t["id"]
            estado_txt = t.get("estado", "nuevo")
            badge_cls = "badge en" if estado_txt == "en curso" else "badge"
            # historial del hilo (memoria)
            hist = estado["threads"].get(tid, {}).get("updates", [])
            hist_prev = [u for u in hist if u["date"] != datetime.now(TZ_PY).strftime("%Y-%m-%d")]
            timeline = ""
            if hist_prev:
                filas = ""
                for u in sorted(hist_prev, key=lambda x: x["date"], reverse=True):
                    filas += f'<div class="time"><span class="d">{esc(u["date"])}</span> — {esc(u["hoy"])}</div>'
                timeline = f"<details><summary>Ver evolución ({len(hist_prev)} días previos)</summary>{filas}</details>"

            fuentes = "".join(
                f'<li><a href="{esc(s["url"])}" target="_blank" rel="noopener">{esc(s["titular"])}</a> '
                f'<span class="m">· {esc(s.get("medio",""))}</span></li>'
                for s in t.get("fuentes", [])
            )
            partes.append(f"""<div class="card">
<div class="tt">{esc(t.get('titulo',''))} <span class="{badge_cls}">{esc(estado_txt)}</span></div>
<div class="ctx"><b>Contexto:</b> {esc(t.get('contexto',''))}</div>
<div class="hoy">{esc(t.get('hoy',''))}</div>
{timeline}
<ul class="srcs">{fuentes}</ul>
</div>""")

    partes.append("</div></body></html>")
    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write("\n".join(partes))
    # marcador para que GitHub Pages no aplique Jekyll
    open(os.path.join(DOCS_DIR, ".nojekyll"), "w").close()
    print(f"  sitio generado en {DOCS_DIR}/index.html")


# =====================================================================
# MAIN
# =====================================================================
def main():
    cfg = cargar_config()
    print("1) recolectando...")
    items = recolectar(cfg)
    estado = cargar_estado()

    if not items:
        print("  sin notas nuevas; genero sitio de todos modos")
        generar_sitio([], estado, cfg)
        return

    print("2) agrupando con Claude...")
    threads_hoy = agrupar(items, estado, cfg)
    print(f"  {len(threads_hoy)} hilos")

    print("3) actualizando memoria...")
    estado = fusionar(estado, threads_hoy)
    guardar_estado(estado)

    print("4) generando sitio...")
    generar_sitio(threads_hoy, estado, cfg)
    print("listo.")


if __name__ == "__main__":
    main()
