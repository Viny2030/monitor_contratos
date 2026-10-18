"""
main.py — FastAPI Monitor de Contratos v3
Ph.D. Monteverde (2020) — Algoritmos contra la Corrupción

Endpoints UI:
  GET  /                  → dashboard HTML
  GET  /manual            → manual de uso

Endpoints API:
  GET  /api/status        → estado del servicio
  GET  /api/resumen       → KPIs ejecutivos
  GET  /api/contratos     → tabla de contratos con filtros
  GET  /api/organismos    → ranking de organismos
  GET  /api/organismos/{nombre} → perfil individual
  GET  /api/proveedores   → ranking por cobro TGN
  GET  /api/proveedores/{cuit}  → perfil individual por CUIT
  GET  /api/monitor       → HHI, fragmentación, ráfagas, fantasmas
  GET  /api/stats         → estadísticas de uso (requiere token)
  POST /api/refresh       → dispara scraping manual (requiere token)
"""
from dotenv import load_dotenv
load_dotenv()

import glob
import os
# Antes apuntaba a mapatransparencia-production y a /api/cruce-cuit, que no existen (404).
MEACI_URL = os.getenv("MEACI_URL", "https://meaci-production.up.railway.app").rstrip("/")
import re
import unicodedata
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import init_db, registrar_visita, get_stats

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR      = "/app/data" if os.path.exists("/app") else "data"

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "")
if not REFRESH_TOKEN:
    # Antes caía en el default adivinable "dev-token". Si no está configurado
    # generamos uno aleatorio por proceso y lo logueamos, para que /api/refresh
    # y /api/stats no queden protegidos por un token público conocido.
    import secrets as _secrets
    REFRESH_TOKEN = _secrets.token_urlsafe(24)
    print(
        "⚠️ REFRESH_TOKEN no configurado — generé uno temporal para este proceso: "
        f"{REFRESH_TOKEN}\n"
        "   Configurá REFRESH_TOKEN en las variables de Railway y en el secret "
        "de GitHub Actions para que /api/refresh siga funcionando entre reinicios."
    )

ADMIN_KEY = os.getenv("ADMIN_KEY", REFRESH_TOKEN)   # alias para compatibilidad
GA_API_SECRET    = os.getenv("GA_API_SECRET", "")
GA_MEASUREMENT_ID = os.getenv("GA_MEASUREMENT_ID", "")
os.makedirs(DATA_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    print("✅ Monitor Contratos v3 arrancando")
    yield

app = FastAPI(
    title="Monitor de Contratos — Ph.D. Monteverde",
    description="Algoritmos contra la Corrupción — BORA → Comprar → TGN",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(Exception)
async def _handler_errores_no_capturados(request: Request, exc: Exception):
    # Cualquier excepción que se nos escape de un endpoint (por ej. antes de
    # entrar a un try/except propio) caía en el handler default de Starlette,
    # que devuelve texto plano sin cuerpo JSON — el frontend lo mostraba como
    # "HTTP 500:" vacío. Acá siempre devolvemos JSON con el detalle real.
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "path": str(request.url.path)},
    )
# ─────────────────────────────────────────────────────────────────────────────
# CACHE Y HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_cache: dict = {"df": None, "timestamp": None}


def _buscar_xlsx() -> list[str]:
    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    # Ordenar por nombre (contiene la fecha YYYY-MM-DD) en vez de mtime,
    # porque en Railway todos los archivos tienen el mismo mtime del build.
    return sorted(
        glob.glob(patron, recursive=True),
        key=lambda x: os.path.basename(x),
        reverse=True,
    )


def _cargar_df() -> pd.DataFrame:
    if _cache["df"] is not None:
        return _cache["df"]
    archivos = _buscar_xlsx()
    if not archivos:
        return pd.DataFrame()
    # Cargar todos los reportes y concatenar
    dfs = []
    hojas_ok = ["🚨 Flujo Completo", "🔗 Flujo Cruzado"]
    for archivo in archivos:
        try:
            xl = pd.ExcelFile(archivo)
            hoja = next((h for h in hojas_ok if h in xl.sheet_names), None)
            if not hoja:
                # Antes se tomaba la primera hoja del archivo: en días sin
                # adjudicaciones eso metía en el tablero las licitaciones BORA o
                # las 22.281 adjudicaciones de COMPR.AR 2020 (fallback histórico),
                # que eran el 96% de los "23.368 contratos".
                continue
            df = xl.parse(hoja, dtype={"cuit_proveedor": str}).fillna("")
            df["_archivo"] = os.path.basename(archivo)
            dfs.append(df)
        except Exception:
            pass
    if not dfs:
        return pd.DataFrame()
    df_total = pd.concat(dfs, ignore_index=True)
    # Los reportes van de distintos meses y alguno puede tener una columna
    # duplicada (mismo nombre repetido). Si eso pasa, df_total[col] deja de
    # ser una Series y rompe cualquier .astype()/.apply() más abajo (500
    # opaco). Nos quedamos con la primera ocurrencia de cada nombre.
    df_total = df_total.loc[:, ~df_total.columns.duplicated()]
    # Un mismo aviso puede aparecer en dos reportes (en feriados se guardaba la
    # edición anterior): se cuenta una sola vez.
    if "link_bora" in df_total.columns:
        con_link = df_total["link_bora"].astype(str).str.strip() != ""
        df_total = pd.concat([
            df_total[con_link].drop_duplicates(subset=["link_bora"], keep="last"),
            df_total[~con_link],
        ], ignore_index=True)
    if "cuit_proveedor" in df_total.columns:
        df_total["cuit_proveedor"] = df_total["cuit_proveedor"].astype(str).str.replace(r"\.0$", "", regex=True).replace("nan", "")
    # Recalcular el riesgo con las reglas vigentes (los reportes viejos guardaron el
    # nivel con indicadores ya retirados: "Sin CUIT", "Sin cobro TGN", "monto < $10M").
    # No se modifican los archivos, solo lo que muestra la web.
    try:
        from diario import _agregar_riesgo
        base = df_total.copy()
        if "cancelada" in base.columns:
            base["cancelada"] = base["cancelada"].astype(str).str.strip().str.lower().isin(["true", "1"])
        recalculado = _agregar_riesgo(base)
        for c in ["indicadores_riesgo", "score_riesgo_licit", "nivel_riesgo_licit"]:
            df_total[c] = recalculado[c].values
    except Exception as e:
        print(f"⚠️ No se pudo recalcular el riesgo: {e}")
    # Normalizar columna fecha. Hasta mayo los reportes usaban "fecha"; desde junio,
    # "fecha_publicacion". Antes se convertía solo la primera columna encontrada
    # ("fecha"), así que el 61% de los contratos (junio en adelante) quedaba sin
    # fecha y desaparecía de cualquier filtro por fechas y de la evolución mensual.
    fechas = [pd.to_datetime(df_total[c], errors="coerce")
              for c in ["fecha", "fecha_publicacion", "fecha_extraccion"] if c in df_total.columns]
    if fechas:
        fecha = fechas[0]
        for otra in fechas[1:]:
            fecha = fecha.fillna(otra)
        df_total["fecha"] = fecha
    # Correcciones manuales/recuperadas (CUIT, proveedor, monto) por aviso: no se
    # tocan los Excel originales, se completan solo celdas vacías.
    df_total = _aplicar_correcciones(df_total)
    # Nombre de organismo normalizado (sin sufijos como "Solicitudes de Provisión")
    col_org = _col(df_total, ["organismo_contratante", "organismo"])
    if col_org:
        df_total["organismo_base"] = df_total[col_org].astype(str).map(_organismo_base)
    _cache["df"]        = df_total
    _cache["timestamp"] = datetime.now().isoformat()
    return df_total


def _invalidar_cache():
    _cache["df"] = None
    _cache["timestamp"] = None


def _col(df, opciones):
    return next((c for c in opciones if c in df.columns), None)


def _parsear_monto(v) -> float:
    """
    Parsea montos que llegan en dos formatos distintos según la columna
    de origen:
      - texto crudo scrapeado de BORA, formato argentino: '.' = separador
        de miles, ',' = separador decimal (ej. "$419.039.352." o
        "$93.842.991,30", a veces con un '.' final suelto como artefacto
        del scraping).
      - números ya parseados por pandas/Excel (monto_comprar, monto_pagado_tgn,
        monto_devengado_tgn), que llegan como float/int de Python y cuyo
        str() usa '.' como separador DECIMAL (ej. "460800.0").

    BUG ANTERIOR: se hacía `.replace(",", ".")` y después se dejaba pasar
    cualquier '.', así que un monto con separador de miles como
    "$93.842.991,30" quedaba "93.842.991.30" (3 puntos) y `float()` tiraba
    ValueError → se devolvía 0.0 silenciosamente. Como casi todos los montos
    reales superan los $1.000, casi toda la plata desaparecía del total
    (de ahí el "$4K" para 888 contratos en vez de miles de millones).
    """
    if v is None:
        return 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return 0.0
    s = s.rstrip(".,")  # artefacto de scraping: punto/coma final suelto
    if not s:
        return 0.0
    if "," in s:
        # Formato argentino con decimales explícitos: '.' miles, ',' decimal.
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        # Varios puntos sin coma: todos son separadores de miles.
        s = s.replace(".", "")
    elif s.count(".") == 1:
        # Un solo punto sin coma: ambiguo entre separador de miles
        # ("58.782" = 58782) y decimal ("460800.0" = 460800.0, ya viene
        # de un float de pandas). Un grupo de exactamente 3 dígitos
        # después del punto es el patrón de miles argentino; cualquier
        # otra longitud (típicamente .0, .00 o decimales largos de un
        # float ya numérico) se deja como parte decimal.
        entero, frac = s.split(".")
        if len(frac) == 3 and entero:
            s = entero + frac
    try:
        return float(s)
    except Exception:
        return 0.0


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFD", texto.upper())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


# ── Búsqueda de texto ────────────────────────────────────────────────────────
# Antes las búsquedas usaban str.contains con expresión regular y distinguían
# acentos: "ejercito" no encontraba "EJÉRCITO", "economia" no encontraba
# "ECONOMÍA", "S.A." coincidía con cualquier cosa (el punto es comodín) y un
# paréntesis suelto daba error 500.
def _contiene(serie: pd.Series, consulta: str) -> pd.Series:
    """Coincidencia literal, sin distinguir mayúsculas ni acentos."""
    q = _norm(str(consulta))
    if not q:
        return pd.Series(True, index=serie.index)
    return serie.fillna("").astype(str).map(_norm).str.contains(q, regex=False)


def _solo_digitos(v) -> str:
    return re.sub(r"\D", "", str(v))


# Siglas de uso común → nombre como figura en el BORA
SIGLAS_ORGANISMOS = {
    "ANSES": "ADMINISTRACION NACIONAL DE LA SEGURIDAD SOCIAL",
    "ARCA":  "AGENCIA DE RECAUDACION Y CONTROL ADUANERO",
    "AFIP":  "AGENCIA DE RECAUDACION Y CONTROL ADUANERO",
    "PFA":   "POLICIA FEDERAL ARGENTINA",
    "SPF":   "SERVICIO PENITENCIARIO FEDERAL",
    "EANA":  "EMPRESA ARGENTINA DE NAVEGACION AEREA",
    "ANES":  "ADMINISTRACION NACIONAL DE ESTABLECIMIENTOS DE SALUD",
    "BNA":   "BANCO DE LA NACION ARGENTINA",
    "MPF":   "MINISTERIO PUBLICO FISCAL",
    "PAMI":  "INSTITUTO NACIONAL DE SERVICIOS SOCIALES PARA JUBILADOS Y PENSIONADOS",
    "INTA":  "INSTITUTO NACIONAL DE TECNOLOGIA AGROPECUARIA",
    "CONICET": "CONSEJO NACIONAL DE INVESTIGACIONES CIENTIFICAS Y TECNICAS",
}


def _expandir_sigla(consulta: str) -> str:
    return SIGLAS_ORGANISMOS.get(_norm(consulta), consulta)


# El BORA a veces agrega al nombre del organismo el título del aviso
# ("SERVICIO PENITENCIARIO FEDERAL Solicitudes de Provisión", "... Adjudicaciones
# Mayo/2026", "NOTA ACLARATORIA - ..."): eso partía un organismo en varios.
_SUFIJOS_ORGANISMO = re.compile(
    r"\s+(?:Solicitud(?:es)?\s+de\s+Provisi[oó]n|Adjudicaciones\s+\w+/\d{4}|Licitaciones\s+P[uú]blicas"
    r"|Licitaci[oó]n(?:es)?\s+Privadas?|Contrataci[oó]n(?:es)?\s+Directas?)\s*$",
    re.IGNORECASE,
)


def _organismo_base(nombre: str) -> str:
    n = re.sub(r"\s+", " ", str(nombre)).strip()
    n = re.sub(r"^(?:NOTA ACLARATORIA|FE DE ERRATAS|AVISO ACLARATORIO)\s*[-–:]\s*", "", n, flags=re.IGNORECASE)
    anterior = None
    while anterior != n:  # puede haber más de un sufijo
        anterior = n
        n = _SUFIJOS_ORGANISMO.sub("", n).strip()
    return n


# ── Correcciones por aviso (data/correcciones/*.csv) ─────────────────────────
CORRECCIONES_DIR = os.path.join(DATA_DIR, "correcciones")
_CAMPOS_CORREGIBLES = ["cuit_proveedor", "proveedor_adjudicado", "monto_adjudicado_bora"]


def _aplicar_correcciones(df: pd.DataFrame) -> pd.DataFrame:
    """
    Completa CUIT, proveedor y monto vacíos con los CSV de data/correcciones/
    (columnas: link_bora + cualquiera de _CAMPOS_CORREGIBLES). Solo llena celdas
    vacías: nunca reemplaza un dato que ya vino del aviso. Los genera, por
    ejemplo, tools/reextraer_avisos.py al volver a leer avisos sin CUIT.
    """
    archivos = sorted(glob.glob(os.path.join(CORRECCIONES_DIR, "*.csv")))
    if not archivos or "link_bora" not in df.columns:
        return df
    try:
        corr = pd.concat([pd.read_csv(a, dtype=str).fillna("") for a in archivos], ignore_index=True)
    except Exception as e:
        print(f"⚠️ No se pudieron leer las correcciones: {e}")
        return df
    if "link_bora" not in corr.columns:
        return df
    corr = corr.drop_duplicates(subset=["link_bora"], keep="last").set_index("link_bora")
    df = df.copy()
    for campo in _CAMPOS_CORREGIBLES:
        if campo not in corr.columns or campo not in df.columns:
            continue
        nuevo = df["link_bora"].map(corr[campo]).fillna("")
        vacio = df[campo].astype(str).str.strip() == ""
        df.loc[vacio & (nuevo != ""), campo] = nuevo[vacio & (nuevo != "")]
    return df


def _json_seguro(obj):
    """Convierte tipos de numpy/pandas a tipos nativos para que FastAPI los serialice."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _json_seguro(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_seguro(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# UI ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    registrar_visita("dashboard")
    with open("templates/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/manual", response_class=HTMLResponse)
async def manual(request: Request):
    registrar_visita("manual")
    with open("templates/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ─────────────────────────────────────────────────────────────────────────────
# API — STATUS Y RESUMEN
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    archivos = _buscar_xlsx()
    df = _cargar_df()
    return {
        "servicio":          "Monitor Contratos v3",
        "version":           "3.0.0",
        "status":            "activo",
        "total_registros":   len(df),
        "total_archivos":    len(archivos),
        "ultimo_reporte":    os.path.basename(archivos[0]) if archivos else None,
        "cache_timestamp":   _cache["timestamp"],
        "timestamp":         datetime.now().isoformat(),
    }


@app.get("/api/resumen")
def resumen():
    registrar_visita("resumen")
    df = _cargar_df()
    if df.empty:
        return {"total": 0, "mensaje": "Sin datos"}

    col_riesgo  = _col(df, ["nivel_riesgo_licit"])
    col_alerta  = _col(df, ["alerta"])
    col_cobro   = _col(df, ["cobro_en_tgn"])
    col_cuit    = _col(df, ["cuit_proveedor", "cuit"])
    col_org     = _col(df, ["organismo_contratante", "organismo"])
    col_monto   = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_escen   = _col(df, ["tipo_decision"])

    alto  = int((df[col_riesgo] == "Alto").sum())  if col_riesgo else 0
    medio = int((df[col_riesgo] == "Medio").sum()) if col_riesgo else 0
    flujo = int(df[col_alerta].str.contains("FLUJO COMPLETO", na=False).sum()) if col_alerta else 0
    tgn   = int((df[col_cobro] == "✅ SÍ").sum()) if col_cobro else 0

    con_cuit  = int(df[col_cuit].astype(str).str.strip().astype(bool).sum()) if col_cuit else 0
    organismos_u = int(df[col_org].nunique()) if col_org else 0
    monto_total  = df[col_monto].apply(_parsear_monto).sum() if col_monto else 0

    escenarios = {}
    if col_escen:
        escenarios = df[col_escen].value_counts().head(7).to_dict()

    return {
        "total":              len(df),
        "riesgo_alto":        alto,
        "riesgo_medio":       medio,
        "flujo_completo":     flujo,
        "cobraron_tgn":       tgn,
        "con_cuit":           con_cuit,
        "organismos_unicos":  organismos_u,
        "monto_total_ars":    round(monto_total, 2),
        "escenarios_monteverde": escenarios,
        "timestamp":          datetime.now().isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# API — CONTRATOS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/contratos")
def contratos(
    alerta:       str | None = Query(None),
    nivel_riesgo: str | None = Query(None),
    organismo:    str | None = Query(None),
    escenario:    str | None = Query(None),
    cuit:         str | None = Query(None),
    desde:        str | None = Query(None),
    hasta:        str | None = Query(None),
    limit:        int        = Query(200, le=1000),
    offset:       int        = Query(0),
):
    registrar_visita("contratos")
    df = _cargar_df().copy()
    if df.empty:
        return {"total": 0, "data": []}

    col_fecha  = _col(df, ["fecha", "fecha_publicacion"])
    col_riesgo = _col(df, ["nivel_riesgo_licit"])
    col_org    = _col(df, ["organismo_contratante", "organismo"])
    col_cuit   = _col(df, ["cuit_proveedor", "cuit"])
    col_escen  = _col(df, ["tipo_decision"])
    if cuit and not _solo_digitos(cuit):
        cuit = None  # texto sin dígitos: no filtra (antes devolvía todo igual)

    if alerta and _norm(alerta) == "SIN CUIT" and col_cuit:
        # Antes buscaba el texto "SIN CUIT" en la columna alerta, que los reportes
        # nuevos ya no usan ("SOLO BORA"): se perdían 187 de los 470 avisos sin CUIT.
        df = df[df[col_cuit].astype(str).str.strip() == ""]
    elif alerta and "alerta" in df.columns:
        df = df[_contiene(df["alerta"], alerta)]
    if nivel_riesgo and col_riesgo:
        df = df[df[col_riesgo].str.upper() == nivel_riesgo.upper()]
    if organismo and col_org:
        df = df[_contiene(df[col_org], _expandir_sigla(organismo))]
    if escenario and col_escen:
        df = df[_contiene(df[col_escen], escenario)]
    if cuit and col_cuit:
        # Se comparan solo los dígitos en los dos lados: antes la consulta perdía los
        # guiones pero los CUIT guardados los conservaban, y un CUIT completo nunca coincidía.
        df = df[df[col_cuit].map(_solo_digitos).str.contains(_solo_digitos(cuit), regex=False)]
    if col_fecha:
        df = df.sort_values(col_fecha, ascending=False, na_position="last")
    if desde and col_fecha:
        df = df[df[col_fecha] >= pd.to_datetime(desde, errors="coerce")]
    if hasta and col_fecha:
        df = df[df[col_fecha] <= pd.to_datetime(hasta, errors="coerce")]

    total = len(df)
    # Serializar fechas
    for c in df.select_dtypes(include=["datetime64"]).columns:
        df[c] = df[c].dt.strftime("%Y-%m-%d")

    return {
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "data":   df.iloc[offset: offset + limit].fillna("").to_dict(orient="records"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# API — ORGANISMOS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/organismos")
def organismos(top: int = Query(20, le=100)):
    registrar_visita("organismos")
    df = _cargar_df()
    if df.empty:
        return {"data": []}

    col_org   = _col(df, ["organismo_base", "organismo_contratante", "organismo"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_cuit  = _col(df, ["cuit_proveedor", "cuit"])
    col_cobro = _col(df, ["cobro_en_tgn"])

    if not col_org:
        return {"data": []}

    df = df.copy()
    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0

    grp = df.groupby(col_org).agg(
        adjudicaciones=("_monto", "count"),
        monto_total=("_monto", "sum"),
        proveedores_distintos=(col_cuit, "nunique") if col_cuit else ("_monto", "count"),
        cobraron_tgn=(col_cobro, lambda x: (x == "✅ SÍ").sum()) if col_cobro else ("_monto", "count"),
    ).reset_index()
    # Solo ~1 de cada 9 avisos trae monto: a igual monto se ordena por cantidad
    grp = grp.sort_values(["monto_total", "adjudicaciones"], ascending=False).head(top)

    grp.columns = ["organismo", "adjudicaciones", "monto_total", "proveedores_distintos", "cobraron_tgn"]
    return {"data": grp.fillna(0).to_dict(orient="records")}


def _resolver_organismos(df: pd.DataFrame, nombre: str, agrupar: bool = False) -> dict:
    """
    Decide qué organismos abarca una búsqueda. Devuelve {"incluidos": [...]} o
    {"ambiguo": True, "coincidencias": [...]} o {"no_encontrado": True, "sugerencias": [...]}.

    Antes se tomaba toda fila cuyo nombre CONTUVIERA el texto (como expresión
    regular y distinguiendo acentos): "AGENCIA DE RECAUDACIÓN Y CONTROL ADUANERO"
    sumaba sus 20 direcciones regionales (160 contratos en lugar de 15), y
    "economia" o "ANSES" no encontraban nada.
    """
    bases = df["organismo_base"].value_counts()
    norm = {b: _norm(b) for b in bases.index}
    q = _norm(_expandir_sigla(nombre))
    if not q:
        return {"no_encontrado": True, "sugerencias": []}
    exactos = [b for b, n in norm.items() if n == q]
    if exactos and not agrupar:
        return {"incluidos": exactos}
    candidatos = [b for b, n in norm.items() if q in n]
    if not candidatos:
        import difflib
        sug = difflib.get_close_matches(q, list(norm.values()), n=5, cutoff=0.5)
        inverso = {n: b for b, n in norm.items()}
        return {"no_encontrado": True, "sugerencias": [inverso[s] for s in sug]}
    if len(candidatos) == 1 or agrupar:
        return {"incluidos": candidatos}
    return {"ambiguo": True, "coincidencias": [
        {"organismo": b, "contratos": int(bases[b])}
        for b in sorted(candidatos, key=lambda b: -int(bases[b]))[:40]
    ]}


def _perfil_organismo(nombre: str, agrupar: bool = False):
    registrar_visita("perfil_organismo")
    try:
        df = _cargar_df()
        if df.empty:
            raise HTTPException(404, "Sin datos")
        if "organismo_base" not in df.columns:
            raise HTTPException(404, "Columna organismo no encontrada")

        r = _resolver_organismos(df, nombre, agrupar)
        if r.get("no_encontrado"):
            sug = r["sugerencias"]
            detalle = f"Organismo '{nombre}' no encontrado."
            if sug:
                detalle += " ¿Quisiste decir: " + " · ".join(sug) + "?"
            raise HTTPException(404, detalle)
        if r.get("ambiguo"):
            return {"ambiguo": True, "consulta": nombre, "coincidencias": r["coincidencias"]}

        df_org = df[df["organismo_base"].isin(r["incluidos"])].copy()

        col_monto  = _col(df_org, ["monto_adjudicado_bora", "monto_adjudicado"])
        col_cuit   = _col(df_org, ["cuit_proveedor", "cuit"])
        col_fecha  = _col(df_org, ["fecha", "fecha_publicacion"])
        col_prov   = _col(df_org, ["proveedor_adjudicado", "proveedor_nombre"])

        df_org["_monto"] = df_org[col_monto].apply(_parsear_monto) if col_monto else 0.0
        con_cuit = df_org[df_org[col_cuit].astype(str).str.strip() != ""] if col_cuit else df_org.iloc[0:0]

        # HHI de proveedores: solo avisos con CUIT (antes los "sin CUIT" contaban
        # como un único proveedor gigante). Si casi no hay montos, se calcula por
        # cantidad de adjudicaciones y se informa la base usada.
        hhi, hhi_base = 0.0, "monto"
        if not con_cuit.empty:
            por_monto = con_cuit.groupby(col_cuit)["_monto"].sum()
            con_monto = (con_cuit["_monto"] > 0).mean()
            if por_monto.sum() > 0 and con_monto >= 0.5:
                participacion = por_monto / por_monto.sum() * 100
            else:
                hhi_base = "cantidad"
                por_cant = con_cuit.groupby(col_cuit).size()
                participacion = por_cant / por_cant.sum() * 100
            hhi = float((participacion ** 2).sum())

        top_provs = []
        if not con_cuit.empty:
            grp_p = con_cuit.groupby(col_cuit).agg(
                contratos=("_monto", "count"),
                monto=("_monto", "sum"),
                nombre=(col_prov, "first") if col_prov else ("_monto", "count"),
            ).reset_index().sort_values(["monto", "contratos"], ascending=False).head(10)
            top_provs = grp_p.fillna("").to_dict(orient="records")

        evol = []
        if col_fecha:
            meses = df_org[col_fecha].dt.to_period("M").astype(str)
            evol = (df_org.assign(_mes=meses).groupby("_mes")
                    .agg(monto=("_monto", "sum"), contratos=("_monto", "count"))
                    .reset_index().rename(columns={"_mes": "mes"}))
            evol = evol[evol["mes"] != "NaT"].to_dict(orient="records")

        flags = {}
        if "indicadores_riesgo" in df_org.columns:
            from collections import Counter
            todos = []
            for f in df_org["indicadores_riesgo"].dropna():
                todos.extend([x.strip() for x in str(f).split("|") if "⚠️" in x])
            flags = dict(Counter(todos).most_common(10))

        if col_fecha:
            df_org = df_org.sort_values(col_fecha, ascending=False, na_position="last")
        for c in df_org.select_dtypes(include=["datetime64"]).columns:
            df_org[c] = df_org[c].dt.strftime("%Y-%m-%d")

        incluidos = r["incluidos"]
        return _json_seguro({
            "organismo":           incluidos[0] if len(incluidos) == 1 else nombre,
            "organismos_incluidos": incluidos,
            "total_contratos":     len(df_org),
            "contratos_sin_cuit":  int(len(df_org) - len(con_cuit)),
            "contratos_con_monto": int((df_org["_monto"] > 0).sum()),
            "monto_total":         round(float(df_org["_monto"].sum()), 2),
            "proveedores_unicos":  int(con_cuit[col_cuit].nunique()) if col_cuit else 0,
            "hhi":                 round(hhi, 1),
            "hhi_base":            hhi_base,
            "hhi_interpretacion":  "Alta concentración" if hhi >= 2500 else ("Moderada" if hhi >= 1500 else "Competitivo"),
            "top_proveedores":     top_provs,
            "evolucion_mensual":   evol,
            "red_flags":           flags,
            "contratos":           df_org.drop(columns=["_monto"]).fillna("").head(200).to_dict(orient="records"),
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error interno en perfil_organismo({nombre!r}): {type(e).__name__}: {e}")


@app.get("/api/organismo")
def perfil_organismo_query(nombre: str = Query(..., min_length=1), agrupar: bool = Query(False)):
    """
    Perfil de un organismo por nombre (sin distinguir acentos; acepta siglas como
    ANSES o ARCA). Con agrupar=true suma todas las coincidencias (por ejemplo,
    todas las direcciones regionales de ARCA). Si hay varias y no se agrupa,
    devuelve {"ambiguo": true, "coincidencias": [...]} para elegir.
    Se pasa por parámetro y no en la ruta porque hay nombres con "/".
    """
    return _perfil_organismo(nombre, agrupar)


@app.get("/api/organismos/lista")
def lista_organismos():
    """Nombres de organismos (normalizados) con su cantidad de avisos, para autocompletar."""
    df = _cargar_df()
    if df.empty or "organismo_base" not in df.columns:
        return {"data": []}
    vc = df["organismo_base"].value_counts()
    return {"data": [{"organismo": k, "contratos": int(v)} for k, v in vc.items()]}


@app.get("/api/organismos/{nombre}")
def perfil_organismo(nombre: str, agrupar: bool = Query(False)):
    """Compatibilidad: igual que /api/organismo?nombre=… (no admite nombres con "/")."""
    return _perfil_organismo(nombre, agrupar)


# ─────────────────────────────────────────────────────────────────────────────
# API — PROVEEDORES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/proveedores")
def proveedores(
    top:      int        = Query(20, le=100),
    orden:    str        = Query("monto_tgn", regex="^(monto_tgn|monto_adj|riesgo|contratos)$"),
    busqueda: str | None = Query(None),
):
    registrar_visita("proveedores")
    df = _cargar_df()
    if df.empty:
        return {"data": []}

    col_cuit   = _col(df, ["cuit_proveedor", "cuit"])
    col_prov   = _col(df, ["proveedor_adjudicado", "proveedor_nombre", "beneficiario_tgn"])
    col_monto  = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    # monto_pagado_tgn es lo pagado por el ORGANISMO (no por proveedor): no se
    # atribuye a cada proveedor. Se mantiene la clave "monto_tgn" en 0 por compatibilidad.
    col_tgn    = None
    col_riesgo = _col(df, ["score_riesgo_licit", "indice_riesgo_licit"])
    if orden == "monto_tgn":
        orden = "monto_adj"

    if not col_cuit:
        return {"data": []}

    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0
    df["_tgn"]   = df[col_tgn].apply(_parsear_monto)   if col_tgn   else 0

    agg = {
        "contratos":   ("_monto", "count"),
        "monto_adj":   ("_monto", "sum"),
        "monto_tgn":   ("_tgn", "sum"),
    }
    if col_prov:
        agg["nombre"] = (col_prov, "first")
    if col_riesgo:
        agg["riesgo_promedio"] = (col_riesgo, "mean")

    grp = df[df[col_cuit].astype(str).str.strip().astype(bool)].groupby(col_cuit).agg(
        **{k: v for k, v in agg.items()}
    ).reset_index()
    grp.columns = [col_cuit] + list(agg.keys())
    grp = grp.rename(columns={col_cuit: "cuit"})

    if busqueda:
        # Literal y sin acentos; el CUIT se compara solo por dígitos (con o sin guiones)
        digitos = _solo_digitos(busqueda)
        mask = (grp["cuit"].map(_solo_digitos).str.contains(digitos, regex=False) if digitos
                else pd.Series(False, index=grp.index))
        if "nombre" in grp.columns:
            mask = mask | _contiene(grp["nombre"], busqueda)
        grp = grp[mask]

    orden_col = {"monto_tgn": "monto_tgn", "monto_adj": "monto_adj",
                 "riesgo": "riesgo_promedio", "contratos": "contratos"}.get(orden, "monto_tgn")
    if orden_col in grp.columns:
        grp = grp.sort_values(orden_col, ascending=False)

    return {"data": grp.head(top).fillna(0).to_dict(orient="records")}


@app.get("/api/proveedores/{cuit}")
def perfil_proveedor(cuit: str):
    registrar_visita("perfil_proveedor")
    try:
        df = _cargar_df()
        if df.empty:
            raise HTTPException(404, "Sin datos")

        col_cuit = _col(df, ["cuit_proveedor", "cuit"])
        if not col_cuit:
            raise HTTPException(404, "Sin columna CUIT")

        cuit_norm = re.sub(r"[^\d]", "", str(cuit))
        df_prov = df[df[col_cuit].apply(
            lambda x: re.sub(r"[^\d]", "", str(x)) == cuit_norm
        )].copy()

        if df_prov.empty:
            raise HTTPException(404, f"CUIT {cuit} no encontrado")

        col_monto = _col(df_prov, ["monto_adjudicado_bora", "monto_adjudicado"])
        col_tgn   = None  # el monto TGN es del organismo, no de este proveedor
        col_org   = _col(df_prov, ["organismo_base", "organismo_contratante", "organismo"])
        col_fecha = _col(df_prov, ["fecha", "fecha_publicacion"])
        col_prov  = _col(df_prov, ["proveedor_adjudicado", "proveedor_nombre"])

        df_prov["_monto"] = df_prov[col_monto].apply(_parsear_monto) if col_monto else 0.0
        df_prov["_tgn"]   = df_prov[col_tgn].apply(_parsear_monto)   if col_tgn   else 0.0

        nombres = df_prov[col_prov].astype(str).str.strip() if col_prov else pd.Series(dtype=str)
        nombres = nombres[nombres != ""]
        nombre = nombres.mode().iloc[0] if not nombres.empty else cuit

        # Organismos contratantes
        top_orgs = []
        if col_org:
            top_orgs = (df_prov.groupby(col_org)["_monto"].sum().sort_values(ascending=False).head(10)
                        .reset_index().rename(columns={col_org: "organismo"}).to_dict(orient="records"))

        # Evolución mensual
        evol = []
        if col_fecha:
            df_prov["_mes"] = df_prov[col_fecha].dt.to_period("M").astype(str)
            evol = df_prov.groupby("_mes")["_monto"].sum().reset_index().rename(columns={"_mes": "mes", "_monto": "monto"}).to_dict(orient="records")

        # Red flags
        flags = {}
        if "indicadores_riesgo" in df_prov.columns:
            from collections import Counter
            todos = []
            for f in df_prov["indicadores_riesgo"].dropna():
                todos.extend([x.strip() for x in str(f).split("|") if "⚠️" in x])
            flags = dict(Counter(todos).most_common(10))

        for c in df_prov.select_dtypes(include=["datetime64"]).columns:
            df_prov[c] = df_prov[c].dt.strftime("%Y-%m-%d")

        # _json_seguro: "monto_cobrado_tgn" quedaba como numpy.int64 (la columna _tgn es
        # 0 desde que se dejó de atribuir TGN por proveedor) y FastAPI no podía
        # serializarlo: TODOS los perfiles de proveedor respondían error 500.
        return _json_seguro({
            "cuit":            cuit,
            "nombre":          nombre,
            "total_contratos": len(df_prov),
            "monto_adjudicado": round(float(df_prov["_monto"].sum()), 2),
            "monto_cobrado_tgn": round(float(df_prov["_tgn"].sum()), 2),
            "organismos_unicos": int(df_prov[col_org].nunique()) if col_org else 0,
            "top_organismos":  top_orgs,
            "evolucion_mensual": evol,
            "red_flags":       flags,
            "contratos":       df_prov.drop(columns=["_monto", "_tgn", "_mes"], errors="ignore").fillna("").head(200).to_dict(orient="records"),
        })
    except HTTPException:
        raise
    except Exception as e:
        # Diagnóstico temporal: exponemos el error real en vez de un 500 opaco,
        # para poder detectar la causa exacta del "no se pudo cargar el perfil".
        raise HTTPException(500, f"Error interno en perfil_proveedor({cuit!r}): {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# API — MONITOR
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/monitor")
def monitor():
    registrar_visita("monitor")
    df = _cargar_df()
    if df.empty:
        return {"fragmentacion": [], "proveedor_unico": [], "hhi": [], "fantasmas": []}

    col_org   = _col(df, ["organismo_contratante", "organismo"])
    col_cuit  = _col(df, ["cuit_proveedor", "cuit"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_cobro = _col(df, ["cobro_en_tgn"])
    col_fecha = _col(df, ["fecha", "fecha_publicacion"])
    col_tipo  = _col(df, ["tipo_procedimiento", "tipo_proceso_bora", "tipo_proceso"])

    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0

    # ── Fragmentación ─────────────────────────────────────────────────────────
    # Organismos con muchos contratos de bajo monto (posible división para evadir umbral)
    fragmentacion = []
    if col_org:
        UMBRAL = 1_000 * float(os.getenv("MODULO_ARS", "50000"))  # tope compulsa abreviada (1.000 módulos)
        df_bajo = df[df["_monto"].between(1, UMBRAL)]
        grp_f = df_bajo.groupby(col_org).agg(
            contratos=("_monto", "count"),
            monto_promedio=("_monto", "mean"),
            monto_total=("_monto", "sum"),
        ).reset_index()
        grp_f.columns = ["organismo", "contratos", "monto_promedio", "monto_total"]
        grp_f = grp_f[grp_f["contratos"] >= 3].sort_values("contratos", ascending=False).head(20)
        fragmentacion = grp_f.round(2).to_dict(orient="records")

    # ── Proveedor único ───────────────────────────────────────────────────────
    proveedor_unico = []
    if col_org and col_cuit:
        grp_u = df[df[col_cuit].astype(str).str.strip().astype(bool)].groupby(col_org).agg(
            total_contratos=("_monto", "count"),
            cuits_distintos=(col_cuit, "nunique"),
            monto_total=("_monto", "sum"),
        ).reset_index()
        grp_u.columns = ["organismo", "total_contratos", "cuits_distintos", "monto_total"]
        grp_u["concentracion_pct"] = (1 / grp_u["cuits_distintos"] * 100).round(1)
        proveedor_unico = grp_u[
            (grp_u["total_contratos"] >= 3) &
            (grp_u["cuits_distintos"] == 1)
        ].sort_values("monto_total", ascending=False).head(20).round(2).to_dict(orient="records")

    # ── HHI por organismo ─────────────────────────────────────────────────────
    hhi_ranking = []
    if col_org and col_cuit:
        resultados_hhi = []
        for org, grupo in df[df[col_cuit].astype(str).str.strip().astype(bool)].groupby(col_org):
            por_cuit = grupo.groupby(col_cuit)["_monto"].sum()
            total_m  = por_cuit.sum()
            if total_m > 0 and len(grupo) >= 3:
                hhi = float(((por_cuit / total_m * 100) ** 2).sum())
                resultados_hhi.append({
                    "organismo":    org,
                    "hhi":          round(hhi, 0),
                    "contratos":    len(grupo),
                    "interpretacion": "Alta concentración" if hhi >= 2500 else ("Moderada" if hhi >= 1500 else "Competitivo"),
                })
        hhi_ranking = sorted(resultados_hhi, key=lambda x: x["hhi"], reverse=True)[:20]

    # ── Fantasmas (adjudicados sin cobro TGN) ─────────────────────────────────
    # DESACTIVADO: la API de TGN no informa el CUIT del beneficiario, así que
    # "sin cobro" nunca se puede verificar por proveedor; la lista mostraba como
    # posibles "fantasmas" a cualquier proveedor con CUIT.
    fantasmas = []
    if False and col_cuit and col_cobro:
        col_prov = _col(df, ["proveedor_adjudicado", "proveedor_nombre"])
        df_con_cuit = df[df[col_cuit].astype(str).str.strip().astype(bool)]
        df_sin_cobro = df_con_cuit[df_con_cuit[col_cobro] == "❌ NO"]
        if not df_sin_cobro.empty:
            agg = {"contratos": ("_monto", "count"), "monto_total": ("_monto", "sum")}
            if col_prov:
                agg["nombre"] = (col_prov, "first")
            grp_fan = df_sin_cobro.groupby(col_cuit).agg(**agg).reset_index()
            grp_fan.columns = [col_cuit] + list(agg.keys())
            grp_fan = grp_fan.rename(columns={col_cuit: "cuit"})
            fantasmas = grp_fan.sort_values("monto_total", ascending=False).head(20).round(2).fillna("").to_dict(orient="records")

    return {
        "fragmentacion":   fragmentacion,
        "proveedor_unico": proveedor_unico,
        "hhi":             hhi_ranking,
        "fantasmas":       fantasmas,
        "fantasmas_nota":  "No disponible: la Tesorería no publica el CUIT del beneficiario, así que no se puede verificar cobro por proveedor.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# API — LICITACIONES/DATOS  (consumido por repo monitor / IRI)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/sin-cuit")
def sin_cuit(
    organismo: str | None = Query(None),
    limit:     int        = Query(100, le=1000),
    offset:    int        = Query(0),
):
    """
    Adjudicaciones cuyo CUIT no se pudo extraer del aviso del BORA. No aparecen en
    Proveedores (que agrupa por CUIT), así que acá se listan con el enlace al aviso
    para revisarlas a mano. Algunas traen igual el nombre del proveedor.
    """
    registrar_visita("sin_cuit")
    df = _cargar_df()
    col_cuit = _col(df, ["cuit_proveedor", "cuit"]) if not df.empty else None
    if not col_cuit:
        return {"total": 0, "data": [], "por_organismo": [], "por_mes": []}
    total_general = len(df)
    df = df[df[col_cuit].astype(str).str.strip() == ""].copy()
    if "cancelada" in df.columns:
        df = df[~df["cancelada"].astype(str).str.lower().isin(["true", "1"])]
    if organismo:
        df = df[_contiene(df["organismo_base"], _expandir_sigla(organismo))]
    col_prov = _col(df, ["proveedor_adjudicado"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    con_nombre = int((df[col_prov].astype(str).str.strip() != "").sum()) if col_prov else 0

    por_org = df["organismo_base"].value_counts().head(20)
    meses = df["fecha"].dt.to_period("M").astype(str) if "fecha" in df.columns else pd.Series(dtype=str)
    por_mes = meses[meses != "NaT"].value_counts().sort_index()

    df = df.sort_values("fecha", ascending=False, na_position="last") if "fecha" in df.columns else df
    filas = []
    for _, r in df.iloc[offset: offset + limit].iterrows():
        filas.append({
            "fecha":      r["fecha"].strftime("%Y-%m-%d") if pd.notna(r.get("fecha")) else "",
            "organismo":  r.get("organismo_base", ""),
            "tipo":       r.get("tipo_proceso_bora", "") or r.get("tipo_proceso", ""),
            "proveedor":  r.get(col_prov, "") if col_prov else "",
            "monto":      r.get(col_monto, "") if col_monto else "",
            "link_bora":  r.get("link_bora", ""),
        })
    return _json_seguro({
        "total":          len(df),
        "porcentaje":     round(len(df) / total_general * 100, 1) if total_general else 0,
        "con_nombre_proveedor": con_nombre,
        "por_organismo":  [{"organismo": k, "avisos": int(v)} for k, v in por_org.items()],
        "por_mes":        [{"mes": k, "avisos": int(v)} for k, v in por_mes.items()],
        "limit": limit, "offset": offset,
        "data":           filas,
    })


@app.get("/api/licitaciones/datos")
def licitaciones_datos(fecha: str | None = Query(None)):
    """
    Endpoint consumido por el IRI (repo monitor).
    Devuelve flujo, comprar, tgn y totales del último reporte disponible
    (o del reporte de `fecha` si se especifica, formato YYYY-MM-DD).
    """
    registrar_visita("licitaciones_datos")

    # Buscar archivo de reporte
    if fecha:
        patron = os.path.join(DATA_DIR, "**", f"reporte_{fecha}.xlsx")
        archivos = glob.glob(patron, recursive=True)
    else:
        archivos = _buscar_xlsx()

    if not archivos:
        return {"sin_datos": True, "flujo": [], "comprar": [], "tgn": [], "totales": {}}

    archivo = archivos[0]

    try:
        xl = pd.ExcelFile(archivo)
    except Exception as e:
        return {"sin_datos": True, "error": str(e), "flujo": [], "comprar": [], "tgn": [], "totales": {}}

    def _hoja(opciones):
        return next((h for h in opciones if h in xl.sheet_names), None)

    # ── Flujo completo ────────────────────────────────────────────────────────
    flujo = []
    hoja_flujo = _hoja(["🚨 Flujo Completo", "Flujo Completo"])
    if hoja_flujo:
        df_f = xl.parse(hoja_flujo).fillna("")
        # Mapear columnas al esquema que espera el IRI
        for c in df_f.select_dtypes(include=["datetime64"]).columns:
            df_f[c] = df_f[c].dt.strftime("%Y-%m-%d")
        # Renombrar para que el IRI los encuentre
        renames = {
            "score_riesgo_licit":  "indice_fenomeno_corruptivo",
            "nivel_riesgo_licit":  "nivel_riesgo_teorico",
            "organismo_contratante": "organismo",
            "proveedor_adjudicado":  "proveedor",
            "monto_adjudicado_bora": "monto",
        }
        df_f = df_f.rename(columns={k: v for k, v in renames.items() if k in df_f.columns})
        flujo = df_f.to_dict(orient="records")

    # ── Comprar ───────────────────────────────────────────────────────────────
    comprar = []
    hoja_comp = _hoja(["🛒 Comprar ONC", "Comprar ONC", "Comprar"])
    if hoja_comp:
        df_c = xl.parse(hoja_comp).fillna("")
        for c in df_c.select_dtypes(include=["datetime64"]).columns:
            df_c[c] = df_c[c].dt.strftime("%Y-%m-%d")
        if "fuente_comprar" in df_c.columns:  # excluir el CSV histórico 2020 guardado como fallback
            df_c = df_c[~df_c["fuente_comprar"].astype(str).str.contains("histórico", na=False)]
        renames_c = {"organismo_contratante": "organismo", "tipo_procedimiento": "tipo"}
        df_c = df_c.rename(columns={k: v for k, v in renames_c.items() if k in df_c.columns})
        comprar = df_c.to_dict(orient="records")

    # ── TGN ───────────────────────────────────────────────────────────────────
    tgn = []
    hoja_tgn = _hoja(["💰 TGN Pagos", "TGN Pagos", "TGN"])
    if hoja_tgn:
        df_t = xl.parse(hoja_tgn).fillna("")
        for c in df_t.select_dtypes(include=["datetime64"]).columns:
            df_t[c] = df_t[c].dt.strftime("%Y-%m-%d")
        renames_t = {"organismo_tgn": "organismo", "monto_pagado": "monto", "jurisdiccion": "jurisdiccion"}
        df_t = df_t.rename(columns={k: v for k, v in renames_t.items() if k in df_t.columns})
        tgn = df_t.to_dict(orient="records")

    totales = {
        "flujo":   len(flujo),
        "comprar": len(comprar),
        "tgn":     len(tgn),
        "archivo": os.path.basename(archivo),
        "timestamp": datetime.now().isoformat(),
    }

    return {
        "sin_datos": len(flujo) == 0 and len(comprar) == 0 and len(tgn) == 0,
        "flujo":    flujo,
        "comprar":  comprar,
        "tgn":      tgn,
        "totales":  totales,
    }


# ─────────────────────────────────────────────────────────────────────────────
# API — STATS Y REFRESH
# ─────────────────────────────────────────────────────────────────────────────

# Clave propia para ver las estadísticas de uso. Así se puede compartir el acceso a
# las estadísticas sin entregar REFRESH_TOKEN (que además dispara el scraping).
# Se configura en Railway → Variables → STATS_KEY. ADMIN_KEY y REFRESH_TOKEN
# siguen funcionando, para no romper accesos existentes.
STATS_KEY = os.getenv("STATS_KEY", "")


def _clave_stats_valida(clave: str | None) -> bool:
    import secrets as _s
    if not clave:
        return False
    validas = [k for k in (STATS_KEY, ADMIN_KEY, REFRESH_TOKEN) if k]
    return any(_s.compare_digest(clave.encode(), k.encode()) for k in validas)


@app.get("/api/stats")
def stats(x_refresh_token: str = Header(None), x_stats_key: str = Header(None)):
    if not (_clave_stats_valida(x_stats_key) or _clave_stats_valida(x_refresh_token)):
        raise HTTPException(status_code=401, detail="Clave inválida")
    return get_stats()


@app.post("/api/refresh")
def refresh(x_refresh_token: str = Header(None)):
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")
    _invalidar_cache()
    try:
        from diario import main as run_diario
        df = run_diario()
        _invalidar_cache()
        return {
            "status":    "ok",
            "registros": len(df) if df is not None else 0,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _token_valido(t):
    return t in (REFRESH_TOKEN, ADMIN_KEY)


# ── MEACI: Cruce Internacional de CUIT ───────────────────────────────────────
@app.get("/api/cruce-cuit")
async def cruce_cuit(cuit: str):
    """Consulta si un CUIT está sancionado internacionalmente (vía MEACI)."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{MEACI_URL}/api/empresas/cuit/{cuit}")
            if r.status_code == 200:
                return {"cuit": cuit, "sancionado": True, "fuente": "MEACI", "empresa": r.json()}
            if r.status_code == 404:
                return {"cuit": cuit, "sancionado": False, "fuente": "MEACI", "detalle": "sin coincidencias"}
            return {"cuit": cuit, "sancionado": False, "fuente": "MEACI", "error": f"MEACI respondió {r.status_code}"}
    except Exception as e:
        return {"cuit": cuit, "sancionado": False, "fuente": "MEACI", "error": str(e)}

@app.get("/api/cruce-cuits-bulk")
async def cruce_cuits_bulk(cuits: str):
    """Consulta múltiples CUITs separados por coma. Retorna solo los sancionados."""
    lista = [c.strip() for c in cuits.split(",") if c.strip()][:50]
    alertas = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{MEACI_URL}/api/cruce-cuits-bulk", params={"cuits": ",".join(lista)})
            if r.status_code == 200:
                alertas = r.json().get("alertas", {})
    except Exception:
        pass
    return {"alertas": alertas, "total_consultados": len(lista), "total_alertas": len(alertas)}

@app.post("/api/reload")
def reload_alias(x_refresh_token: str = Header(None)):
    """Alias de /api/refresh — compatibilidad con workflows anteriores."""
    if not _token_valido(x_refresh_token):
        raise HTTPException(status_code=401, detail="Token inválido")
    _invalidar_cache()
    return {"status": "ok (reload alias)", "timestamp": datetime.now().isoformat()}


# ─────────────────────────────────────────────────────────────────────────────
# API — AGENTIC AI (explicaciones narrativas vía Claude)
# ─────────────────────────────────────────────────────────────────────────────

class PerfilOrganismo(BaseModel):
    organismo: str = ""
    total_contratos: int = 0
    monto_total: float = 0
    proveedores_unicos: int = 0
    hhi: float = 0
    hhi_interpretacion: str = ""
    red_flags: dict = {}
    top_proveedores: list = []


class PerfilProveedor(BaseModel):
    cuit: str = ""
    nombre: str = ""
    total_contratos: int = 0
    monto_adjudicado: float = 0
    monto_cobrado_tgn: float = 0
    organismos_unicos: int = 0
    red_flags: dict = {}
    top_organismos: list = []


class AlertaMonitor(BaseModel):
    tipo: str  # "fragmentacion" | "unico" | "hhi" | "fantasmas"
    fila: dict = {}


# Límite de uso de la IA (cada consulta tiene costo): 3 por visitante (IP) por día
# y un tope diario total. Solo cuenta si la IA respondió.
import hashlib as _hashlib
import threading as _threading
from datetime import timezone as _tz
IA_LIMITE_POR_IP = int(os.getenv("IA_LIMITE_POR_IP", "3"))
IA_LIMITE_GLOBAL = int(os.getenv("IA_LIMITE_GLOBAL_DIA", "30"))
_ia_usos = {"dia": None, "por_ip": {}, "total": 0}
_ia_lock = _threading.Lock()


def _ia_clave(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    ip = (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or "?"
    return _hashlib.sha256(ip.encode()).hexdigest()[:16]


def _ia_con_limite(request: Request, fn):
    from agentic_ai import ia_disponible
    if not ia_disponible():
        return fn()
    hoy = datetime.now(_tz.utc).date()
    clave = _ia_clave(request)
    with _ia_lock:
        if _ia_usos["dia"] != hoy:
            _ia_usos.update({"dia": hoy, "por_ip": {}, "total": 0})
        usados = _ia_usos["por_ip"].get(clave, 0)
        if usados >= IA_LIMITE_POR_IP or _ia_usos["total"] >= IA_LIMITE_GLOBAL:
            return JSONResponse(status_code=429, content={
                "disponible": False,
                "motivo": f"Alcanzaste el límite de {IA_LIMITE_POR_IP} consultas con IA por día. Probá mañana.",
            })
        _ia_usos["por_ip"][clave] = usados + 1
        _ia_usos["total"] += 1
    resultado = fn()
    if not resultado.get("disponible"):
        with _ia_lock:
            _ia_usos["por_ip"][clave] = max(_ia_usos["por_ip"].get(clave, 1) - 1, 0)
            _ia_usos["total"] = max(_ia_usos["total"] - 1, 0)
    else:
        resultado["usos_restantes_hoy"] = IA_LIMITE_POR_IP - usados - 1
    return resultado


@app.get("/api/ia/status")
def ia_status():
    from agentic_ai import ia_disponible
    return {"disponible": ia_disponible(), "limite_por_dia": IA_LIMITE_POR_IP}


@app.post("/api/ia/explicar-organismo")
def ia_explicar_organismo(perfil: PerfilOrganismo, request: Request):
    from agentic_ai import explicar_perfil_organismo
    return _ia_con_limite(request, lambda: explicar_perfil_organismo(perfil.model_dump()))


@app.post("/api/ia/explicar-proveedor")
def ia_explicar_proveedor(perfil: PerfilProveedor, request: Request):
    from agentic_ai import explicar_perfil_proveedor
    return _ia_con_limite(request, lambda: explicar_perfil_proveedor(perfil.model_dump()))


@app.post("/api/ia/explicar-alerta")
def ia_explicar_alerta(alerta: AlertaMonitor, request: Request):
    from agentic_ai import explicar_alerta_monitor
    if alerta.tipo == "fantasmas":
        return {"disponible": False, "motivo": "La alerta de 'fantasmas' está desactivada (TGN no informa CUIT)."}
    return _ia_con_limite(request, lambda: explicar_alerta_monitor(alerta.tipo, alerta.fila))
