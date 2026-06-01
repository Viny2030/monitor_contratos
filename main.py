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
import re
import unicodedata
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from db import init_db, registrar_visita, get_stats

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR      = "/app/data" if os.path.exists("/app") else "data"
REFRESH_TOKEN = os.getenv("REFRESH_TOKEN", "dev-token")
ADMIN_KEY     = os.getenv("ADMIN_KEY", REFRESH_TOKEN)   # alias para compatibilidad
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
# ─────────────────────────────────────────────────────────────────────────────
# CACHE Y HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_cache: dict = {"df": None, "timestamp": None}


def _buscar_xlsx() -> list[str]:
    patron = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    return sorted(
        glob.glob(patron, recursive=True),
        key=lambda x: os.path.getmtime(x),
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
                hoja = xl.sheet_names[0]
            df = xl.parse(hoja).fillna("")
            df["_archivo"] = os.path.basename(archivo)
            dfs.append(df)
        except Exception:
            pass
    if not dfs:
        return pd.DataFrame()
    df_total = pd.concat(dfs, ignore_index=True)
    # Normalizar columna fecha
    for col in ["fecha", "fecha_publicacion", "fecha_extraccion"]:
        if col in df_total.columns:
            df_total[col] = pd.to_datetime(df_total[col], errors="coerce")
            break
    _cache["df"]        = df_total
    _cache["timestamp"] = datetime.now().isoformat()
    return df_total


def _invalidar_cache():
    _cache["df"] = None
    _cache["timestamp"] = None


def _col(df, opciones):
    return next((c for c in opciones if c in df.columns), None)


def _parsear_monto(v) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", str(v).replace(",", ".")))
    except Exception:
        return 0.0


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFD", texto.upper())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


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

    if alerta and "alerta" in df.columns:
        df = df[df["alerta"].str.contains(alerta, case=False, na=False)]
    if nivel_riesgo and col_riesgo:
        df = df[df[col_riesgo].str.upper() == nivel_riesgo.upper()]
    if organismo and col_org:
        df = df[df[col_org].str.contains(organismo, case=False, na=False)]
    if escenario and col_escen:
        df = df[df[col_escen].str.contains(escenario, case=False, na=False)]
    if cuit and col_cuit:
        df = df[df[col_cuit].astype(str).str.contains(re.sub(r"[^\d]", "", cuit))]
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

    col_org   = _col(df, ["organismo_contratante", "organismo"])
    col_monto = _col(df, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_cuit  = _col(df, ["cuit_proveedor", "cuit"])
    col_cobro = _col(df, ["cobro_en_tgn"])

    if not col_org:
        return {"data": []}

    df["_monto"] = df[col_monto].apply(_parsear_monto) if col_monto else 0

    grp = df.groupby(col_org).agg(
        adjudicaciones=("_monto", "count"),
        monto_total=("_monto", "sum"),
        proveedores_distintos=(col_cuit, "nunique") if col_cuit else ("_monto", "count"),
        cobraron_tgn=(col_cobro, lambda x: (x == "✅ SÍ").sum()) if col_cobro else ("_monto", "count"),
    ).reset_index().sort_values("monto_total", ascending=False).head(top)

    grp.columns = ["organismo", "adjudicaciones", "monto_total", "proveedores_distintos", "cobraron_tgn"]
    return {"data": grp.fillna(0).to_dict(orient="records")}


@app.get("/api/organismos/{nombre}")
def perfil_organismo(nombre: str):
    registrar_visita("perfil_organismo")
    df = _cargar_df()
    if df.empty:
        raise HTTPException(404, "Sin datos")

    col_org = _col(df, ["organismo_contratante", "organismo"])
    if not col_org:
        raise HTTPException(404, "Columna organismo no encontrada")

    df_org = df[df[col_org].str.contains(nombre, case=False, na=False)].copy()
    if df_org.empty:
        raise HTTPException(404, f"Organismo '{nombre}' no encontrado")

    col_monto  = _col(df_org, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_cuit   = _col(df_org, ["cuit_proveedor", "cuit"])
    col_cobro  = _col(df_org, ["cobro_en_tgn"])
    col_riesgo = _col(df_org, ["nivel_riesgo_licit"])
    col_fecha  = _col(df_org, ["fecha", "fecha_publicacion"])

    df_org["_monto"] = df_org[col_monto].apply(_parsear_monto) if col_monto else 0

    # HHI por proveedor
    hhi = 0.0
    if col_cuit:
        por_cuit = df_org.groupby(col_cuit)["_monto"].sum()
        total_m  = por_cuit.sum()
        if total_m > 0:
            hhi = float(((por_cuit / total_m * 100) ** 2).sum())

    # Top proveedores
    top_provs = []
    if col_cuit:
        col_prov = _col(df_org, ["proveedor_adjudicado", "proveedor_nombre"])
        grp_p = df_org.groupby(col_cuit).agg(
            contratos=("_monto", "count"),
            monto=("_monto", "sum"),
            nombre=(col_prov, "first") if col_prov else ("_monto", "count"),
        ).reset_index().sort_values("monto", ascending=False).head(10)
        top_provs = grp_p.fillna("").to_dict(orient="records")

    # Evolución mensual
    evol = []
    if col_fecha:
        df_org["_mes"] = df_org[col_fecha].dt.to_period("M").astype(str)
        evol = df_org.groupby("_mes")["_monto"].sum().reset_index()
        evol.columns = ["mes", "monto"]
        evol = evol.to_dict(orient="records")

    # Red flags
    flags = {}
    if "indicadores_riesgo" in df_org.columns:
        todos = []
        for f in df_org["indicadores_riesgo"].dropna():
            todos.extend([x.strip() for x in str(f).split("|") if "⚠️" in x])
        from collections import Counter
        flags = dict(Counter(todos).most_common(10))

    # Serializar fechas para tabla
    for c in df_org.select_dtypes(include=["datetime64"]).columns:
        df_org[c] = df_org[c].dt.strftime("%Y-%m-%d")

    return {
        "organismo":           nombre,
        "total_contratos":     len(df_org),
        "monto_total":         round(df_org["_monto"].sum(), 2),
        "proveedores_unicos":  int(df_org[col_cuit].nunique()) if col_cuit else 0,
        "hhi":                 round(hhi, 1),
        "hhi_interpretacion":  "Alta concentración" if hhi >= 2500 else ("Moderada" if hhi >= 1500 else "Competitivo"),
        "top_proveedores":     top_provs,
        "evolucion_mensual":   evol,
        "red_flags":           flags,
        "contratos":           df_org.fillna("").head(200).to_dict(orient="records"),
    }


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
    col_tgn    = _col(df, ["monto_cobrado_tgn", "monto_pagado_tgn"])
    col_riesgo = _col(df, ["score_riesgo_licit", "indice_riesgo_licit"])

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
        mask = grp["cuit"].str.contains(busqueda, case=False, na=False)
        if "nombre" in grp.columns:
            mask |= grp["nombre"].str.contains(busqueda, case=False, na=False)
        grp = grp[mask]

    orden_col = {"monto_tgn": "monto_tgn", "monto_adj": "monto_adj",
                 "riesgo": "riesgo_promedio", "contratos": "contratos"}.get(orden, "monto_tgn")
    if orden_col in grp.columns:
        grp = grp.sort_values(orden_col, ascending=False)

    return {"data": grp.head(top).fillna(0).to_dict(orient="records")}


@app.get("/api/proveedores/{cuit}")
def perfil_proveedor(cuit: str):
    registrar_visita("perfil_proveedor")
    df = _cargar_df()
    if df.empty:
        raise HTTPException(404, "Sin datos")

    col_cuit = _col(df, ["cuit_proveedor", "cuit"])
    if not col_cuit:
        raise HTTPException(404, "Sin columna CUIT")

    cuit_norm = re.sub(r"[^\d]", "", cuit)
    df_prov = df[df[col_cuit].astype(str).apply(
        lambda x: re.sub(r"[^\d]", "", x) == cuit_norm
    )].copy()

    if df_prov.empty:
        raise HTTPException(404, f"CUIT {cuit} no encontrado")

    col_monto = _col(df_prov, ["monto_adjudicado_bora", "monto_adjudicado"])
    col_tgn   = _col(df_prov, ["monto_cobrado_tgn", "monto_pagado_tgn"])
    col_org   = _col(df_prov, ["organismo_contratante", "organismo"])
    col_fecha = _col(df_prov, ["fecha", "fecha_publicacion"])
    col_prov  = _col(df_prov, ["proveedor_adjudicado", "proveedor_nombre"])

    df_prov["_monto"] = df_prov[col_monto].apply(_parsear_monto) if col_monto else 0
    df_prov["_tgn"]   = df_prov[col_tgn].apply(_parsear_monto)   if col_tgn   else 0

    nombre = df_prov[col_prov].dropna().mode().iloc[0] if col_prov and not df_prov[col_prov].dropna().empty else cuit

    # Organismos contratantes
    top_orgs = []
    if col_org:
        top_orgs = df_prov.groupby(col_org)["_monto"].sum().sort_values(ascending=False).head(10).reset_index().to_dict(orient="records")

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

    return {
        "cuit":            cuit,
        "nombre":          nombre,
        "total_contratos": len(df_prov),
        "monto_adjudicado": round(df_prov["_monto"].sum(), 2),
        "monto_cobrado_tgn": round(df_prov["_tgn"].sum(), 2),
        "organismos_unicos": int(df_prov[col_org].nunique()) if col_org else 0,
        "top_organismos":  top_orgs,
        "evolucion_mensual": evol,
        "red_flags":       flags,
        "contratos":       df_prov.fillna("").head(200).to_dict(orient="records"),
    }


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
        UMBRAL = 10_000_000
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
    fantasmas = []
    if col_cuit and col_cobro:
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
    }


# ─────────────────────────────────────────────────────────────────────────────
# API — LICITACIONES/DATOS  (consumido por repo monitor / IRI)
# ─────────────────────────────────────────────────────────────────────────────

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

@app.get("/api/stats")
def stats(x_refresh_token: str = Header(None)):
    if x_refresh_token not in (REFRESH_TOKEN, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Token inválido")
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


@app.post("/api/reload")
def reload_alias(x_refresh_token: str = Header(None)):
    """Alias de /api/refresh — compatibilidad con workflows anteriores."""
    if not _token_valido(x_refresh_token):
        raise HTTPException(status_code=401, detail="Token inválido")
    _invalidar_cache()
    return {"status": "ok (reload alias)", "timestamp": datetime.now().isoformat()}