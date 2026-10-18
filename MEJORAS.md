# Correcciones de octubre de 2026

Rama `correcciones-busqueda-perfiles`. Ningún reporte Excel de `data/` se modificó.

## Qué estaba roto y qué se corrigió

| Problema | Corrección | Dónde |
|---|---|---|
| **Perfil de proveedor**: todos respondían error 500 (un `numpy.int64` que FastAPI no podía convertir a JSON) | Conversión a tipos nativos antes de responder | `main.py` (`_json_seguro`) |
| **Perfil de organismo**: no encontraba nombres sin acento ("ejercito", "economia") ni siglas (ANSES, ARCA); un paréntesis daba error 500; "S.A." coincidía con cualquier cosa; los nombres con "/" daban 404; "AGENCIA DE RECAUDACIÓN Y CONTROL ADUANERO" sumaba sus 20 direcciones regionales | Búsqueda literal sin acentos ni mayúsculas, siglas comunes, coincidencia exacta primero, lista para elegir cuando hay varias (o "ver todos juntos"), sugerencias si no hay ninguna, nuevo `GET /api/organismo?nombre=` (por parámetro, admite "/") y autocompletado | `main.py`, `templates/index.html` |
| Un organismo aparecía partido en varios ("SERVICIO PENITENCIARIO FEDERAL Solicitudes de Provisión", "... Adjudicaciones Mayo/2026", "NOTA ACLARATORIA - ...") | Nombre normalizado (`organismo_base`) para ranking, perfil y listas | `main.py` (`_organismo_base`) |
| **Contratos, fechas**: desde junio los reportes guardan la fecha en otra columna; el 61% de los contratos quedaba sin fecha y desaparecía de los filtros "Desde/Hasta" y del gráfico mensual | Se unifican `fecha`, `fecha_publicacion` y `fecha_extraccion` | `main.py` (`_cargar_df`) |
| **Contratos, CUIT**: un CUIT completo (con o sin guiones) nunca coincidía | Se comparan solo los dígitos en los dos lados | `main.py` |
| **Contratos, "Sin CUIT"**: buscaba un texto que los reportes nuevos ya no usan (encontraba 281 de 470) | Filtra por CUIT vacío | `main.py` |
| Contratos sin orden | De la más reciente a la más antigua | `main.py` |
| HHI del perfil de organismo: los avisos sin CUIT contaban como un único proveedor gigante; con montos casi siempre vacíos daba 0 ("Competitivo") | Solo avisos con CUIT; por monto si hay montos suficientes, si no por cantidad (el perfil lo indica) | `main.py` |
| Alertas MEACI nunca se cargaban (el código leía `res.proveedores`, la API devuelve `res.data`) | Corregido | `templates/index.html` |
| CUIT escrito sin guiones ("20180531778") nunca se leía del aviso (el patrón pedía 12 dígitos) | Patrón de 11 dígitos | `diario.py` (`_normalizar_cuit`) |

## Qué se agregó

- **Sección "Sin CUIT"** (menú lateral) y `GET /api/sin-cuit`: avisos sin CUIT con enlace al BORA, organismos con más casos y filtro por organismo. Antes eran invisibles: Proveedores agrupa por CUIT.
- **Recuperación de datos**: `tools/reextraer_avisos.py` vuelve a leer del BORA los avisos sin CUIT, proveedor o monto y guarda lo recuperado en `data/correcciones/avisos_reextraidos.csv`. El sitio completa con ese archivo **solo las celdas vacías**; los Excel originales no se tocan. Es reanudable (`data/correcciones/.revisados.txt`).
- **Clave nueva para Estadísticas** (`STATS_KEY`): permite ver las estadísticas sin entregar `REFRESH_TOKEN`, que además dispara el scraping. Se compara en tiempo constante. `ADMIN_KEY` y `REFRESH_TOKEN` siguen funcionando.
- **Manual en español e inglés** (pestañas en "Manual de Uso"), con las reglas de riesgo reales, los límites de TGN y las secciones nuevas. Se corrigieron afirmaciones falsas: "muestra quién cobró (TGN)" y "umbral de $10M".
- Enter para buscar en todos los buscadores; clic en una barra del gráfico de organismos abre su perfil; el perfil muestra las últimas adjudicaciones con enlace al BORA.
- `test_busquedas.py` (19 tests) agregado a `.github/workflows/test.yml`.

## Habilitar la clave nueva de Estadísticas

1. En Railway → servicio → **Variables** → **New Variable**: nombre `STATS_KEY`, valor una clave larga. Para generarla en PowerShell:
   ```powershell
   -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
   ```
2. Railway redespliega solo. En el sitio: **Estadísticas** → pegar la clave → Enter.

Nota: si `DATABASE_URL` no está configurada, Estadísticas responde "DB no configurada" aunque la clave sea correcta.

## Recuperar CUIT y montos de todo el histórico

```bash
python tools/reextraer_avisos.py
```

Tarda unos 10 segundos por aviso (el BORA responde lento y hay una pausa de cortesía). En esta rama ya se revisaron 67 de los 737 avisos incompletos y se recuperaron datos en 29 (9 CUIT, 8 proveedores, 17 montos). El resto queda pendiente: la herramienta retoma donde quedó. Al terminar, commitear `data/correcciones/`.

## Pendiente (propuestas)

- Correr `tools/reextraer_avisos.py` también en el cron diario, para reintentar los avisos del día que quedaron incompletos.
- El cron hace dos commits por día (09:00 y 12:00) aunque el de la mañana haya funcionado.
- `registrar_visita` abre una conexión nueva a PostgreSQL en cada consulta a la API: conviene un pool o registrar en segundo plano.
- `POST /api/refresh` corre el scraping completo dentro de la request (Railway corta a los ~90 s): pasarlo a segundo plano como en gob_bo_comprar_tgn.
- `/api/stats` devuelve nombre y email de quienes donaron: si la clave se comparte, conviene ocultarlos.
