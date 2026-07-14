# Radar de Noticias

Junta noticias de Paraguay de muchos medios, las **agrupa por tema** y para
cada hilo te explica el **contexto/origen** y lo que pasó hoy. Recuerda los
temas de días anteriores, así entendés de dónde salió cada conversación.

Corre solo **una vez por día** en la nube (GitHub Actions) y publica un sitio
web que abrís **desde cualquier dispositivo**. No depende de tu PC.

---

## Cómo funciona (en simple)

1. Trae titulares de Google News filtrados a Paraguay (ABC, Última Hora, 5Días,
   InfoNegocios, La Tribuna, etc.) según los temas de `config.yaml`.
2. Claude los agrupa en hilos, escribe el contexto de origen y el resumen de hoy,
   enlazando con hilos de días previos.
3. Genera el sitio en `docs/` y lo guarda. GitHub Pages lo publica en una URL.

---

## Puesta en marcha (una sola vez, ~10 min)

Necesitás una cuenta de **GitHub** y una **API key de Anthropic**
(https://console.anthropic.com → API Keys).

1. **Crear el repositorio.** En GitHub → *New repository* → nombre `news-radar`
   → *Public* → *Create*. Subí todos estos archivos (arrastrarlos en
   *uploading an existing file*, o usá Claude Code / git).
   > Tiene que ser **público** porque GitHub Pages gratis solo publica repos
   > públicos. El repo no guarda ninguna clave (la API key va como *secreto*),
   > y el sitio son noticias públicas resumidas. Si querés que sea privado,
   > necesitás el plan GitHub Pro (~US$4/mes).

2. **Guardar la API key como secreto.** En el repo → *Settings* → *Secrets and
   variables* → *Actions* → *New repository secret*.
   - Name: `ANTHROPIC_API_KEY`
   - Secret: tu clave `sk-ant-...`

3. **Activar GitHub Pages.** *Settings* → *Pages* → en *Source* elegí
   *Deploy from a branch* → Branch: `main`, carpeta: `/docs` → *Save*.
   Te dará una URL tipo `https://TU-USUARIO.github.io/news-radar/`.

4. **Primera corrida.** *Actions* → *Radar de noticias (diario)* → *Run workflow*.
   En 1–2 minutos se genera el sitio. Abrí la URL de Pages.

Desde ahí corre solo todos los días a las ~08:00 (Paraguay).

---

## Ajustes rápidos

- **Temas y fuentes:** editá `config.yaml` (lista `google_news_queries`).
  Podés agregar/quitar líneas cuando quieras.
- **Horario:** en `.github/workflows/daily.yml`, la línea `cron: "0 11 * * *"`
  (11 UTC = 8 AM PY). Cambiá el `11` por otra hora UTC.
- **Modelo/costo:** en `config.yaml`, `modelo`. Sonnet da mejor redacción del
  contexto; si querés abaratar, se puede probar un modelo más liviano.
- **Correr a mano:** en local, con la clave en `.env`,
  `pip install -r requirements.txt && python run.py`.

## Costo aproximado
Solo pagás las llamadas a la API (una por día, unos pocos miles de tokens).
El hosting en GitHub Pages y la ejecución en Actions son gratis en cuentas
personales.
