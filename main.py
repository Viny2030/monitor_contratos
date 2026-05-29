"""
main.py — FastAPI Monitor de Contratos v3
Ph.D. Monteverde (2020) — Algoritmos contra la Corrupción

Endpoints:
  GET  /              → dashboard HTML
  GET  /api/status    → estado del servicio + último reporte
  GET  /api/contratos → datos del último reporte (con filtros opcionales)
  GET  /api/resumen   → KPIs ejecutivos
  POST /api/refresh   → dispara scraping manual (requiere REFRESH_TOKEN)
  GET  /api/stats     → estadísticas de uso (requiere REFRESH_TOKEN)

Variables de entorno:
  DATABASE_URL    → PostgreSQL (tracking visitas/donaciones)
  REFRESH_TOKEN   → token para endpoints protegidos
  TGN_TOKEN       → token API Presupuesto Abierto
"""

import glob
import os
from contextlib import asynccontextmanager
from datetime import datetime

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import Request

from db import init_db, registrar_visita, get_stats

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR      = "/app/data" if os.path.exists("/app") else "data"
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "dev-token")

os.makedirs(DATA_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Railway sirve solo la API.
    El scraping diario lo maneja GitHub Actions (scraping_diario.yml).
    """
    init_db()
    print("✅ Monitor Contratos v3 arrancando — modo API puro")
    yield
    print("Monitor Contratos v3 apagando")


# ─────────────────────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────────────────────

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
templates = Jinja2Templates(directory="templates")

# Cache en memoria (se invalida con /api/refresh)
_cache: dict = {"df": None, "timestamp": None}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _buscar_xlsx() -> list[str]:
    """Lista todos los reportes ordenados por fecha (más reciente primero)."""
    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(
        glob.glob(patron, recursive=True),
        key=lambda x: os.path.getmtime(x),
        reverse=True,
    )
    return archivos


def _cargar_ultimo_reporte() -> pd.DataFrame:
    """Carga el reporte más reciente en cache."""
    if _cache["df"] is not None:
        return _cache["df"]

    archivos = _buscar_xlsx()
    if not archivos:
        return pd.DataFrame()

    hojas_preferidas = ["🚨 Flujo Completo", "🔗 Flujo Cruzado", "Sheet1"]
    try:
        xl   = pd.ExcelFile(archivos[0])
        hoja = next((h for h in hojas_preferidas if h in xl.sheet_names), xl.sheet_names[0])
        df   = xl.parse(hoja).fillna("").astype(str)
        _cache["df"]        = df
        _cache["timestamp"] = datetime.now().isoformat()
        return df
    except Exception as e:
        print(f"Error cargando reporte: {e}")
        return pd.DataFrame()


def _invalidar_cache():
    _cache["df"]        = None
    _cache["timestamp"] = None


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=templates.TemplateResponse.__class__)
async def dashboard(request: Request):
    """Sirve el dashboard HTML."""
    registrar_visita("dashboard")
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/status")
def status():
    """Estado del servicio y metadatos del último reporte disponible."""
    archivos = _buscar_xlsx()
    ultimo_archivo = None
    ultimo_fecha   = None
    total_contratos = 0

    if archivos:
        ultimo_archivo = os.path.basename(archivos[0])
        ultimo_fecha   = datetime.fromtimestamp(
            os.path.getmtime(archivos[0])
        ).isoformat()
        df = _cargar_ultimo_reporte()
        total_contratos = len(df)

    return {
        "servicio":        "Monitor Contratos v3 — Ph.D. Monteverde",
        "version":         "3.0.0",
        "status":          "activo",
        "ultimo_reporte":  ultimo_archivo,
        "ultima_carga":    ultimo_fecha,
        "total_contratos": total_contratos,
        "reportes_en_disco": len(archivos),
        "cache_activo":    _cache["df"] is not None,
        "cache_timestamp": _cache["timestamp"],
        "timestamp":       datetime.now().isoformat(),
    }


@app.get("/api/resumen")
def resumen():
    """KPIs ejecutivos del último reporte."""
    registrar_visita("resumen")
    df = _cargar_ultimo_reporte()

    if df.empty:
        return {
            "total": 0,
            "mensaje": "Sin datos — ejecutar scraping primero",
        }

    # Conteos por nivel de riesgo
    alto  = int((df.get("nivel_riesgo_licit", pd.Series()) == "Alto").sum())
    medio = int((df.get("nivel_riesgo_licit", pd.Series()) == "Medio").sum())

    # Conteos por alerta de flujo
    flujo_completo = int(
        df.get("alerta", pd.Series("")).str.contains("FLUJO COMPLETO", na=False).sum()
    )
    cobraron_tgn = int(
        (df.get("cobro_en_tgn", pd.Series()) == "✅ SÍ").sum()
    )

    # Distribución por escenario Monteverde
    escenarios = {}
    if "tipo_decision" in df.columns:
        escenarios = df["tipo_decision"].value_counts().to_dict()

    return {
        "total":               len(df),
        "riesgo_alto":         alto,
        "riesgo_medio":        medio,
        "flujo_completo":      flujo_completo,
        "cobraron_tgn":        cobraron_tgn,
        "escenarios_monteverde": escenarios,
        "timestamp":           datetime.now().isoformat(),
    }


@app.get("/api/contratos")
def contratos(
    alerta:         str | None = Query(None, description="Filtrar por tipo de alerta"),
    nivel_riesgo:   str | None = Query(None, description="Alto / Medio / Bajo"),
    organismo:      str | None = Query(None, description="Filtro parcial por organismo"),
    escenario:      str | None = Query(None, description="Escenario Monteverde"),
    limit:          int        = Query(200,  description="Máximo de registros", le=1000),
    offset:         int        = Query(0,    description="Paginación"),
):
    """
    Retorna los contratos del último reporte con filtros opcionales.
    Paginable con limit/offset.
    """
    registrar_visita("contratos")
    df = _cargar_ultimo_reporte()

    if df.empty:
        return {"total": 0, "data": [], "mensaje": "Sin datos"}

    # Filtros
    if alerta and "alerta" in df.columns:
        df = df[df["alerta"].str.contains(alerta, case=False, na=False)]

    if nivel_riesgo and "nivel_riesgo_licit" in df.columns:
        df = df[df["nivel_riesgo_licit"].str.upper() == nivel_riesgo.upper()]

    if organismo and "organismo_contratante" in df.columns:
        df = df[df["organismo_contratante"].str.contains(organismo, case=False, na=False)]

    if escenario and "tipo_decision" in df.columns:
        df = df[df["tipo_decision"].str.contains(escenario, case=False, na=False)]

    total = len(df)
    df_pag = df.iloc[offset: offset + limit]

    return {
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "data":   df_pag.to_dict(orient="records"),
    }


@app.post("/api/refresh")
def refresh(x_refresh_token: str = Header(None)):
    """
    Dispara el ciclo de scraping manualmente.
    Normalmente GitHub Actions lo ejecuta — este endpoint es para emergencias.
    Requiere header X-Refresh-Token.
    """
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    _invalidar_cache()

    try:
        from diario import main as run_diario
        df = run_diario()
        _invalidar_cache()  # forzar recarga después del scraping
        return {
            "status":    "ok",
            "mensaje":   "Ciclo completado",
            "registros": len(df) if df is not None else 0,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats")
def stats(x_refresh_token: str = Header(None)):
    """
    Estadísticas de uso (visitas, donaciones).
    Requiere header X-Refresh-Token.
    """
    if x_refresh_token != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Token inválido")

    return get_stats()
