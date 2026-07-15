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
    import socket
    socket.setdefaulttimeout(20)  # que ningún sitio lento cuelgue la corrida

    ventana = timedelta(hours=cfg.get("horas_de_ventana", 30))
    ahora = datetime.now(timezone.utc)
    vistos = set()
    items = []

    feeds = [("gnews", gnews_url(q)) for q in cfg.get("google_news_queries", [])]
    # medios seguidos: todo lo que publique ese dominio, vía Google News
    for dom in cfg.get("medios_seguidos") or []:
        feeds.append((dom, gnews_url(f"site:{dom}")))
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
- Asigná "categoria": una de [Inmobiliario, Economía, Agro, Regulación, Política, Deportes, Otros].
- "estado": "en curso" si continúa un hilo de la memoria, "nuevo" si no.
- Priorizá lo relevante para inmobiliario, construcción, economía, finanzas, agro y regulación, pero incluí también noticias importantes del país.
- En Deportes incluí solo lo destacado (selección paraguaya, torneos importantes, hechos relevantes de los clubes grandes); no cada partido menor.
- Ignorá titulares irrelevantes, duplicados o puro clickbait.
- En "fuentes" poné SOLO los números (campo "n") de los titulares que forman el hilo, ej: [3, 17, 42]. NO copies los textos ni las urls.

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
      "fuentes": [3, 17, 42]
    }
  ]
}

=== MEMORIA (hilos recientes) ===
{memoria}

=== TITULARES DE HOY ===
{titulares}
"""


# Esquema de salida: la API garantiza que la respuesta cumpla esta estructura
# (structured outputs), así el JSON siempre es válido por más larga que sea.
ESQUEMA_SALIDA = {
    "type": "object",
    "properties": {
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "titulo": {"type": "string"},
                    "categoria": {"type": "string", "enum": ["Inmobiliario", "Economía", "Agro", "Regulación", "Política", "Deportes", "Otros"]},
                    "estado": {"type": "string", "enum": ["nuevo", "en curso"]},
                    "contexto": {"type": "string"},
                    "hoy": {"type": "string"},
                    "fuentes": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["id", "titulo", "categoria", "estado", "contexto", "hoy", "fuentes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["threads"],
    "additionalProperties": False,
}


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
    # se numeran los titulares; el modelo referencia por número y acá
    # reconstruimos titular/medio/url (respuestas mucho más cortas)
    titulares = [
        {"n": i, "titular": it["titular"], "medio": it["medio"], "resumen": it["resumen"]}
        for i, it in enumerate(items)
    ]
    prompt = PROMPT.replace("{memoria}", json.dumps(memo, ensure_ascii=False)) \
                   .replace("{titulares}", json.dumps(titulares, ensure_ascii=False))

    client = Anthropic()  # usa ANTHROPIC_API_KEY del entorno
    # streaming: obligatorio para respuestas largas (max_tokens alto)
    # output_config.format: la API valida el JSON contra ESQUEMA_SALIDA
    with client.messages.stream(
        model=cfg.get("modelo", "claude-sonnet-5"),
        max_tokens=cfg.get("max_tokens", 8000),
        output_config={"format": {"type": "json_schema", "schema": ESQUEMA_SALIDA}},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Respuesta cortada por max_tokens; subí max_tokens en config.yaml")
    texto = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    data = extraer_json(texto)
    threads = data.get("threads", [])
    for t in threads:
        t["fuentes"] = [
            {"titular": items[n]["titular"], "medio": items[n]["medio"], "url": items[n]["url"]}
            for n in t.get("fuentes", [])
            if isinstance(n, int) and 0 <= n < len(items)
        ]
    return threads


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
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

ORDEN_CAT = ["Inmobiliario", "Economía", "Agro", "Regulación", "Política", "Deportes", "Otros"]

CSS = """
:root{--bg:#0d1017;--card:#161b26;--line:#252c3b;--tx:#e8ebf2;--mut:#98a2b5;
--acc:#5ba3f5;--head1:#101624;--head2:#0d1017;
--c-inmobiliario:#f2a541;--c-economia:#4ade80;--c-regulacion:#5ba3f5;
--c-politica:#f472b6;--c-agro:#a3e635;--c-deportes:#c084fc;--c-otros:#9aa3b2}
@media (prefers-color-scheme: light){
:root{--bg:#f4f5f8;--card:#ffffff;--line:#dfe3ea;--tx:#1c2230;--mut:#5c6575;
--acc:#1d6fd6;--head1:#e9edf5;--head2:#f4f5f8;
--c-inmobiliario:#b26a00;--c-economia:#0f8a3d;--c-regulacion:#1d6fd6;
--c-politica:#c02670;--c-agro:#4d7c0f;--c-deportes:#7c3aed;--c-otros:#5c6575}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:16px/1.65 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:780px;margin:0 auto;padding:0 18px 90px}
header{background:linear-gradient(180deg,var(--head1),var(--head2));
padding:34px 0 18px;margin-bottom:4px}
h1{font-size:26px;letter-spacing:-.02em;margin:0 0 4px}
h1 .dot{color:var(--acc)}
.sub{color:var(--mut);font-size:13.5px}
.filters{position:sticky;top:0;z-index:5;background:var(--bg);
display:flex;gap:8px;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid var(--line)}
.fbtn{font-size:13px;padding:5px 13px;border-radius:20px;border:1px solid var(--line);
background:transparent;color:var(--mut);cursor:pointer;font-family:inherit}
.fbtn.on{background:var(--acc);border-color:var(--acc);color:#fff}
.day{margin-top:34px}
.dayhead{display:flex;align-items:baseline;gap:10px;margin-bottom:14px}
.dayhead h2{font-size:15px;letter-spacing:.06em;text-transform:uppercase;margin:0}
.dayhead .rel{color:var(--acc)}
.dayhead .n{color:var(--mut);font-size:12.5px;font-weight:400}
.dayhead::after{content:"";flex:1;height:1px;background:var(--line);align-self:center}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;margin-bottom:12px}
.meta{display:flex;gap:8px;align-items:center;margin-bottom:7px;flex-wrap:wrap}
.chip{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;
padding:3px 9px;border-radius:20px;color:var(--catc,var(--mut));
background:color-mix(in srgb,var(--catc,var(--mut)) 14%,transparent)}
.badge{font-size:11px;padding:3px 9px;border-radius:20px;color:var(--mut);
border:1px solid var(--line)}
.badge.nuevo{color:#f5c451;border-color:color-mix(in srgb,#f5c451 40%,transparent)}
h3{font-size:17px;margin:0 0 7px;line-height:1.4}
.hoy{margin:0}
details{margin-top:10px}
summary{cursor:pointer;color:var(--acc);font-size:13.5px;user-select:none}
summary:hover{text-decoration:underline}
.ctx{color:var(--mut);font-size:14.5px;margin:10px 0 4px;
border-left:2px solid var(--acc);padding-left:12px}
.time{margin:8px 0;font-size:14.5px}
.time .d{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
.srcs{list-style:none;padding:0;margin:8px 0 0}
.srcs li{margin:5px 0;font-size:14px}
.srcs a{color:var(--tx);text-decoration:none}
.srcs a:hover{color:var(--acc)}
.srcs .m{color:var(--mut);font-size:12px}
.empty{color:var(--mut);padding:30px 0 10px;text-align:center;font-size:14.5px}
footer{margin-top:50px;color:var(--mut);font-size:12.5px;text-align:center}
"""

JS = """
const btns=document.querySelectorAll('.fbtn');
btns.forEach(b=>b.addEventListener('click',()=>{
  btns.forEach(x=>x.classList.toggle('on',x===b));
  const f=b.dataset.f;
  document.querySelectorAll('.card').forEach(c=>{
    c.style.display=(f==='*'||c.dataset.cat===f)?'':'none';});
  document.querySelectorAll('.day').forEach(d=>{
    const alguno=[...d.querySelectorAll('.card')].some(c=>c.style.display!=='none');
    d.style.display=alguno?'':'none';});
}));
"""

FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
           "viewBox='0 0 100 100'><text y='.9em' font-size='90'>📡</text></svg>")


def esc(s):
    return html.escape(s or "")


def fecha_es(d, con_anio=False):
    """'lunes 14 de julio' a partir de un datetime/date."""
    base = f"{DIAS_ES[d.weekday()]} {d.day} de {MESES_ES[d.month - 1]}"
    return f"{base} de {d.year}" if con_anio else base


def slug_cat(cat):
    # las variables CSS solo existen para las categorías conocidas
    return _norm(cat) if cat in ORDEN_CAT else "otros"


def _card(tid, t, u, fecha_dia, es_hoy):
    """HTML de una tarjeta: el hilo `t` con su actualización `u` del día."""
    cat = t.get("categoria", "Otros")
    var_cat = f"--c-{slug_cat(cat)}"
    nuevo = t.get("first_seen", "") == fecha_dia
    badge = '<span class="badge nuevo">nuevo</span>' if nuevo else '<span class="badge">en curso</span>'

    contexto = f'<div class="ctx">{esc(t.get("contexto", ""))}</div>'
    # evolución: las otras actualizaciones del hilo (días distintos a esta tarjeta)
    otras = [x for x in t.get("updates", []) if x["date"] != fecha_dia and x.get("hoy")]
    evo = ""
    if otras:
        filas = "".join(
            f'<div class="time"><span class="d">{esc(x["date"])}</span> — {esc(x["hoy"])}</div>'
            for x in sorted(otras, key=lambda x: x["date"], reverse=True)
        )
        evo = f'<div class="time" style="margin-top:12px"><b>Evolución:</b></div>{filas}'

    fuentes = u.get("fuentes", [])
    lis = "".join(
        f'<li><a href="{esc(s.get("url", ""))}" target="_blank" rel="noopener">{esc(s.get("titular", ""))}</a> '
        f'<span class="m">· {esc(s.get("medio", ""))}</span></li>'
        for s in fuentes
    )
    n_f = len(fuentes)
    src = f'<details><summary>{n_f} fuente{"s" if n_f != 1 else ""}</summary><ul class="srcs">{lis}</ul></details>' if n_f else ""

    if es_hoy:
        detalle = contexto + (f"<details><summary>Ver evolución</summary>{evo}</details>" if evo else "")
    else:
        detalle = f"<details><summary>Contexto</summary>{contexto}{evo}</details>"

    return f"""<article class="card" data-cat="{esc(cat)}" style="--catc:var({var_cat})">
<div class="meta"><span class="chip">{esc(cat)}</span>{badge}</div>
<h3>{esc(t.get('titulo', ''))}</h3>
<p class="hoy">{esc(u.get('hoy', ''))}</p>
{detalle}
{src}
</article>"""


def generar_sitio(estado, cfg):
    os.makedirs(DOCS_DIR, exist_ok=True)
    ahora = datetime.now(TZ_PY)
    hoy_str = ahora.strftime("%Y-%m-%d")
    n_dias = cfg.get("dias_en_pagina", 5)
    titulo = cfg.get("titulo_sitio", "Radar de Noticias")

    # categorías presentes (para los filtros)
    cats_presentes = []

    # secciones por día: hoy, ayer, ... (últimos n_dias)
    secciones = []
    for i in range(n_dias):
        d = ahora - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        # tarjetas: hilos que tuvieron actualización ese día
        tarjetas = []
        for tid, t in estado.get("threads", {}).items():
            u = next((x for x in t.get("updates", []) if x["date"] == d_str), None)
            if u:
                tarjetas.append((tid, t, u))
        if not tarjetas:
            continue
        # ordenar por categoría (orden editorial) y luego por título
        tarjetas.sort(key=lambda x: (
            ORDEN_CAT.index(x[1].get("categoria", "Otros")) if x[1].get("categoria") in ORDEN_CAT else 99,
            x[1].get("titulo", ""),
        ))
        for _, t, _u in tarjetas:
            c = t.get("categoria", "Otros")
            if c not in cats_presentes:
                cats_presentes.append(c)

        rel = "Hoy" if i == 0 else ("Ayer" if i == 1 else "")
        etiqueta = f'<span class="rel">{rel} · </span>{esc(fecha_es(d))}' if rel else esc(fecha_es(d))
        n = len(tarjetas)
        cuerpo = "".join(_card(tid, t, u, d_str, i == 0) for tid, t, u in tarjetas)
        secciones.append(f"""<section class="day">
<div class="dayhead"><h2>{etiqueta}</h2><span class="n">{n} tema{"s" if n != 1 else ""}</span></div>
{cuerpo}
</section>""")

    cats_orden = [c for c in ORDEN_CAT if c in cats_presentes]
    filtros = '<button class="fbtn on" data-f="*">Todas</button>' + "".join(
        f'<button class="fbtn" data-f="{esc(c)}">{esc(c)}</button>' for c in cats_orden
    )

    cuerpo = "\n".join(secciones) if secciones else \
        '<div class="empty">Todavía no hay noticias registradas.</div>'

    html_doc = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Resumen diario de noticias de Paraguay: inmobiliario, economía, regulación y política.">
<link rel="icon" href="{FAVICON}">
<title>{esc(titulo)}</title>
<style>{CSS}</style></head><body>
<header><div class="wrap" style="padding-bottom:0">
<h1>{esc(titulo)}<span class="dot">.</span></h1>
<div class="sub">Actualizado: {esc(fecha_es(ahora, con_anio=True))}, {ahora.strftime("%H:%M")} (hora Paraguay) · últimos {n_dias} días</div>
</div></header>
<div class="wrap">
<nav class="filters">{filtros}</nav>
{cuerpo}
<footer>Generado automáticamente cada mañana · Radar de Noticias</footer>
</div>
<script>{JS}</script>
</body></html>"""

    with open(os.path.join(DOCS_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html_doc)
    # marcador para que GitHub Pages no aplique Jekyll
    open(os.path.join(DOCS_DIR, ".nojekyll"), "w").close()
    print(f"  sitio generado en {DOCS_DIR}/index.html")


# =====================================================================
# MAIN
# =====================================================================
def podar_estado(estado, dias=60):
    """Borra hilos sin actividad hace más de `dias` para que la memoria no crezca sin límite."""
    corte = (datetime.now(TZ_PY) - timedelta(days=dias)).strftime("%Y-%m-%d")
    viejos = [tid for tid, t in estado.get("threads", {}).items()
              if t.get("last_seen", "") < corte]
    for tid in viejos:
        del estado["threads"][tid]
    if viejos:
        print(f"  podados {len(viejos)} hilos viejos")
    return estado


def main():
    cfg = cargar_config()
    print("1) recolectando...")
    items = recolectar(cfg)
    estado = cargar_estado()

    if items:
        print("2) agrupando con Claude...")
        threads_hoy = agrupar(items, estado, cfg)
        print(f"  {len(threads_hoy)} hilos")

        print("3) actualizando memoria...")
        estado = fusionar(estado, threads_hoy)
        estado = podar_estado(estado)
        guardar_estado(estado)
    else:
        print("  sin notas nuevas; genero el sitio con la memoria existente")

    print("4) generando sitio...")
    generar_sitio(estado, cfg)
    print("listo.")


if __name__ == "__main__":
    main()
