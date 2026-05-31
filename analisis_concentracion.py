"""
analisis_concentracion.py — Análisis de Concentración y Fragmentación
Monitor de Fenómenos Corruptivos — Ph.D. Monteverde (2020)

Detecta patrones sistémicos que van más allá del contrato individual:

  1. Fragmentación de contratos
     Un organismo divide una compra grande en varios contratos pequeños
     para evitar el umbral de licitación pública.

  2. Proveedor único por organismo
     Un organismo adjudica siempre al mismo CUIT — captura del contratista.

  3. Concentración temporal (ráfaga)
     Múltiples adjudicaciones al mismo proveedor en una ventana corta
     de tiempo — patrón de adjudicación acelerada.

  4. Distribución del gasto (HHI por organismo)
     Qué tan concentrado está el gasto de cada organismo en pocos proveedores.

  5. Proveedor fantasma
     CUIT adjudicado que nunca aparece en TGN — ganó pero no cobró,
     patrón posible de empresa pantalla o error de datos.

Uso:
    python analisis_concentracion.py                    # análisis completo
    python analisis_concentracion.py --fragmentacion    # solo fragmentación
    python analisis_concentracion.py --proveedor-unico  # solo proveedor único
    python analisis_concentracion.py --rafaga           # solo ráfagas
    python analisis_concentracion.py --hhi              # solo concentración HHI
    python analisis_concentracion.py --fantasmas        # CUITs sin cobro TGN
    python analisis_concentracion.py --exportar         # genera Excel completo
"""

import os
import re
import sys
import glob
import argparse
import unicodedata
from datetime import datetime, timedelta

import pandas as pd


# ─────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────
DATA_DIR = os.path.join(os.getcwd(), "data")

# Umbral de licitación pública en ARS
# Adjudicaciones cercanas a este monto activan la alerta de fragmentación
UMBRAL_LICITACION = 10_000_000
TOLERANCIA_UMBRAL = 0.15   # ±15%

# Ventana temporal para detectar ráfagas (días)
VENTANA_RAFAGA_DIAS = 7

# Mínimo de adjudicaciones para considerar proveedor único
MIN_ADJ_PROVEEDOR_UNICO = 3

# HHI: índice de concentración de Herfindahl-Hirschman
HHI_ALTO   = 2500   # mercado altamente concentrado
HHI_MEDIO  = 1500   # concentración moderada


# ─────────────────────────────────────────
# CARGA DE DATOS
# ─────────────────────────────────────────
def cargar_historico():
    patron   = os.path.join(DATA_DIR, "**", "reporte_*.xlsx")
    archivos = sorted(glob.glob(patron, recursive=True))

    if not archivos:
        print(f"⚠️  No se encontraron archivos en {DATA_DIR}")
        print("    Ejecutá primero: python diario.py")
        return pd.DataFrame()

    dfs = []
    for archivo in archivos:
        try:
            xl = pd.ExcelFile(archivo, engine="openpyxl")
            for sheet in ["🚨 Flujo Completo", "🔗 Flujo Cruzado"]:
                if sheet in xl.sheet_names:
                    df = pd.read_excel(xl, sheet_name=sheet, engine="openpyxl")
                    df["_archivo"] = os.path.basename(archivo)
                    dfs.append(df)
                    break
        except Exception as e:
            print(f"  ⚠️  {archivo}: {e}")

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)

    # Normalizar fechas
    for col in ["fecha", "fecha_extraccion", "fecha_publicacion"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    print(f"  ✅ {len(df)} registros históricos ({len(archivos)} archivos)\n")
    return df


def parsear_monto(valor):
    if pd.isna(valor) or not str(valor).strip():
        return 0.0
    try:
        limpio = re.sub(r"[^\d,]", "", str(valor)).replace(",", ".")
        partes = limpio.split(".")
        if len(partes) > 2:
            limpio = "".join(partes[:-1]) + "." + partes[-1]
        return float(limpio)
    except Exception:
        return 0.0


def normalizar(texto):
    if not isinstance(texto, str):
        return ""
    t = texto.upper().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


def preparar_df(df):
    """Agrega columnas numéricas y normalizadas para el análisis."""
    col_monto = next(
        (c for c in ["monto_adjudicado_bora", "monto_adjudicado"] if c in df.columns), None
    )
    col_tgn = next(
        (c for c in ["monto_cobrado_tgn", "monto_pagado"] if c in df.columns), None
    )
    col_org = next(
        (c for c in ["organismo_contratante", "organismo"] if c in df.columns), None
    )
    col_cuit = next(
        (c for c in ["cuit_proveedor", "cuit"] if c in df.columns), None
    )
    col_fecha = next(
        (c for c in ["fecha", "fecha_publicacion", "fecha_extraccion"] if c in df.columns), None
    )

    df = df.copy()
    df["_monto"]    = df[col_monto].apply(parsear_monto) if col_monto else 0.0
    df["_tgn"]      = df[col_tgn].apply(parsear_monto)   if col_tgn   else 0.0
    df["_org_norm"] = df[col_org].apply(normalizar)       if col_org   else ""
    df["_cuit"]     = df[col_cuit].astype(str).str.strip() if col_cuit else ""
    df["_fecha"]    = pd.to_datetime(df[col_fecha], errors="coerce") if col_fecha else pd.NaT

    return df, col_org, col_cuit, col_fecha


# ─────────────────────────────────────────
# 1. FRAGMENTACIÓN DE CONTRATOS
# Detecta organismos que dividen compras en montos
# cercanos al umbral de licitación pública
# ─────────────────────────────────────────
def detectar_fragmentacion(df, exportar_df=False):
    print("━" * 60)
    print("🔪 ANÁLISIS DE FRAGMENTACIÓN DE CONTRATOS")
    print(f"   Umbral licitación: ${UMBRAL_LICITACION:,.0f} ARS (±{int(TOLERANCIA_UMBRAL*100)}%)")
    print("━" * 60)

    df, col_org, col_cuit, col_fecha = preparar_df(df)

    limite_inf = UMBRAL_LICITACION * (1 - TOLERANCIA_UMBRAL)
    limite_sup = UMBRAL_LICITACION * (1 + TOLERANCIA_UMBRAL)

    # Contratos cerca del umbral
    df_cerca = df[
        (df["_monto"] >= limite_inf) & (df["_monto"] <= limite_sup) & (df["_monto"] > 0)
    ].copy()

    if df_cerca.empty:
        print("  ✅ No se detectaron contratos cerca del umbral\n")
        return pd.DataFrame()

    print(f"\n  {len(df_cerca)} contratos cerca del umbral (${limite_inf:,.0f} — ${limite_sup:,.0f})\n")

    # Agrupar por organismo: cuántos contratos cerca del umbral tiene
    if col_org:
        grp = (
            df_cerca.groupby(col_org)
            .agg(
                contratos_cerca=("_monto", "count"),
                monto_total=("_monto", "sum"),
                cuits_distintos=(col_cuit, "nunique") if col_cuit else ("_monto", "count"),
            )
            .reset_index()
            .sort_values("contratos_cerca", ascending=False)
        )

        # Alerta si hay 2+ contratos cerca del umbral en el mismo organismo
        df_alerta = grp[grp["contratos_cerca"] >= 2].copy()
        df_alerta["nivel_alerta"] = df_alerta["contratos_cerca"].apply(
            lambda x: "🔴 Crítico" if x >= 5 else ("🟡 Moderado" if x >= 3 else "🟠 Leve")
        )

        print(f"  {'Organismo':<55}  {'Contratos':>9}  {'Monto Total':>18}  {'Alerta'}")
        print("  " + "-" * 105)
        for _, row in df_alerta.iterrows():
            print(f"  {str(row[col_org])[:55]:<55}  "
                  f"{int(row['contratos_cerca']):>9}  "
                  f"${row['monto_total']:>17,.0f}  "
                  f"{row['nivel_alerta']}")

        if df_alerta.empty:
            print("  ✅ Ningún organismo con patrón de fragmentación sistemática")
        else:
            print(f"\n  ⚠️  {len(df_alerta)} organismos con posible fragmentación")

        print()
        return df_alerta if exportar_df else pd.DataFrame()

    return pd.DataFrame()


# ─────────────────────────────────────────
# 2. PROVEEDOR ÚNICO POR ORGANISMO
# Un organismo adjudica siempre (o casi siempre)
# al mismo CUIT — señal de captura del contratista
# ─────────────────────────────────────────
def detectar_proveedor_unico(df, exportar_df=False):
    print("━" * 60)
    print("🔒 ANÁLISIS DE PROVEEDOR ÚNICO POR ORGANISMO")
    print(f"   Mínimo de adjudicaciones para analizar: {MIN_ADJ_PROVEEDOR_UNICO}")
    print("━" * 60)

    df, col_org, col_cuit, col_fecha = preparar_df(df)

    if not col_org or not col_cuit:
        print("  ❌ Faltan columnas de organismo o CUIT\n")
        return pd.DataFrame()

    df_valido = df[df["_cuit"].str.strip() != ""].copy()

    # Por organismo: total de adjudicaciones y CUITs distintos
    grp = (
        df_valido.groupby(col_org)
        .agg(
            total_adj=("_cuit", "count"),
            cuits_distintos=("_cuit", "nunique"),
            monto_total=("_monto", "sum"),
        )
        .reset_index()
    )

    # Solo organismos con suficientes adjudicaciones
    grp = grp[grp["total_adj"] >= MIN_ADJ_PROVEEDOR_UNICO].copy()
    grp["pct_concentracion"] = (1 / grp["cuits_distintos"]) * 100

    # Organismos con un solo proveedor o muy pocos
    df_unico = grp[grp["cuits_distintos"] == 1].copy()
    df_muy_concentrado = grp[
        (grp["cuits_distintos"] > 1) & (grp["pct_concentracion"] >= 60)
    ].copy()

    print(f"\n  🔴 PROVEEDOR ÚNICO (1 solo CUIT en todas las adjudicaciones)")
    print(f"  {'Organismo':<55}  {'Adj':>5}  {'Monto Total':>18}")
    print("  " + "-" * 85)
    if df_unico.empty:
        print("  ✅ No se detectaron organismos con proveedor único absoluto")
    else:
        for _, row in df_unico.sort_values("total_adj", ascending=False).iterrows():
            # Identificar el CUIT dominante
            cuit_dom = (
                df_valido[df_valido[col_org] == row[col_org]]["_cuit"]
                .value_counts().index[0]
            )
            print(f"  {str(row[col_org])[:55]:<55}  "
                  f"{int(row['total_adj']):>5}  "
                  f"${row['monto_total']:>17,.0f}  "
                  f"→ CUIT: {cuit_dom}")

    print(f"\n  🟡 ALTA CONCENTRACIÓN (≥60% en un solo proveedor)")
    print(f"  {'Organismo':<55}  {'Adj':>5}  {'CUITs':>5}  {'% Conc.':>8}  {'Monto Total':>18}")
    print("  " + "-" * 100)
    if df_muy_concentrado.empty:
        print("  ✅ No se detectaron organismos con alta concentración")
    else:
        for _, row in df_muy_concentrado.sort_values("pct_concentracion", ascending=False).iterrows():
            print(f"  {str(row[col_org])[:55]:<55}  "
                  f"{int(row['total_adj']):>5}  "
                  f"{int(row['cuits_distintos']):>5}  "
                  f"{row['pct_concentracion']:>7.1f}%  "
                  f"${row['monto_total']:>17,.0f}")

    total_alertas = len(df_unico) + len(df_muy_concentrado)
    print(f"\n  ⚠️  {total_alertas} organismos con patrón de proveedor único o concentración alta\n")

    resultado = pd.concat([df_unico, df_muy_concentrado], ignore_index=True) if exportar_df else pd.DataFrame()
    return resultado


# ─────────────────────────────────────────
# 3. CONCENTRACIÓN TEMPORAL — RÁFAGAS
# Múltiples adjudicaciones al mismo CUIT
# en una ventana corta de tiempo
# ─────────────────────────────────────────
def detectar_rafaga(df, exportar_df=False):
    print("━" * 60)
    print("⚡ ANÁLISIS DE RÁFAGAS DE ADJUDICACIÓN")
    print(f"   Ventana temporal: {VENTANA_RAFAGA_DIAS} días")
    print("━" * 60)

    df, col_org, col_cuit, col_fecha = preparar_df(df)

    if not col_cuit or not col_fecha:
        print("  ❌ Faltan columnas de CUIT o fecha\n")
        return pd.DataFrame()

    df_valido = df[
        df["_cuit"].str.strip().ne("") & df["_fecha"].notna()
    ].copy()

    df_valido = df_valido.sort_values(["_cuit", "_fecha"])

    rafagas = []

    for cuit, grupo in df_valido.groupby("_cuit"):
        if len(grupo) < 2:
            continue

        fechas = sorted(grupo["_fecha"].tolist())

        # Ventana deslizante
        for i, fecha_ini in enumerate(fechas):
            fecha_fin = fecha_ini + timedelta(days=VENTANA_RAFAGA_DIAS)
            en_ventana = grupo[
                (grupo["_fecha"] >= fecha_ini) & (grupo["_fecha"] <= fecha_fin)
            ]
            if len(en_ventana) >= 3:
                monto_ventana = en_ventana["_monto"].sum()
                orgs = en_ventana[col_org].nunique() if col_org else 1
                nombre_prov = ""
                for col_n in ["proveedor_adjudicado", "proveedor_nombre"]:
                    if col_n in en_ventana.columns:
                        nombres = en_ventana[col_n].dropna()
                        if not nombres.empty:
                            nombre_prov = nombres.iloc[0][:40]
                            break
                rafagas.append({
                    "cuit":            cuit,
                    "nombre":          nombre_prov,
                    "fecha_inicio":    fecha_ini.date(),
                    "fecha_fin":       en_ventana["_fecha"].max().date(),
                    "adj_en_ventana":  len(en_ventana),
                    "monto_total":     monto_ventana,
                    "organismos":      orgs,
                    "nivel_alerta":    (
                        "🔴 Crítico" if len(en_ventana) >= 7
                        else "🟡 Moderado" if len(en_ventana) >= 5
                        else "🟠 Leve"
                    ),
                })
            # Evitar duplicados: avanzar al siguiente grupo sin solapamiento
            break

    if not rafagas:
        print("  ✅ No se detectaron ráfagas de adjudicación\n")
        return pd.DataFrame()

    df_rafagas = (
        pd.DataFrame(rafagas)
        .sort_values("adj_en_ventana", ascending=False)
        .drop_duplicates(subset=["cuit"])
        .reset_index(drop=True)
    )

    print(f"\n  {len(df_rafagas)} proveedores con ráfaga de adjudicaciones\n")
    print(f"  {'CUIT':<20}  {'Nombre':<40}  {'Período':<22}  "
          f"{'Adj':>4}  {'Orgs':>5}  {'Monto':>18}  Alerta")
    print("  " + "-" * 125)
    for _, row in df_rafagas.iterrows():
        periodo = f"{row['fecha_inicio']} → {row['fecha_fin']}"
        print(f"  {str(row['cuit']):<20}  {str(row['nombre']):<40}  "
              f"{periodo:<22}  "
              f"{int(row['adj_en_ventana']):>4}  "
              f"{int(row['organismos']):>5}  "
              f"${row['monto_total']:>17,.0f}  "
              f"{row['nivel_alerta']}")

    print()
    return df_rafagas if exportar_df else pd.DataFrame()


# ─────────────────────────────────────────
# 4. CONCENTRACIÓN HHI POR ORGANISMO
# Índice de Herfindahl-Hirschman adaptado
# Mide si el gasto de cada organismo está
# concentrado en pocos proveedores
# ─────────────────────────────────────────
def analisis_hhi(df, top_n=15, exportar_df=False):
    print("━" * 60)
    print("📐 ÍNDICE DE CONCENTRACIÓN HHI POR ORGANISMO")
    print(f"   HHI > {HHI_ALTO}: alta concentración (riesgo de captura)")
    print(f"   HHI {HHI_MEDIO}–{HHI_ALTO}: concentración moderada")
    print(f"   HHI < {HHI_MEDIO}: mercado competitivo")
    print("━" * 60)

    df, col_org, col_cuit, col_fecha = preparar_df(df)

    if not col_org or not col_cuit:
        print("  ❌ Faltan columnas requeridas\n")
        return pd.DataFrame()

    df_valido = df[df["_monto"] > 0].copy()
    resultados = []

    for org, grupo in df_valido.groupby(col_org):
        if len(grupo) < 2:
            continue

        total = grupo["_monto"].sum()
        if total == 0:
            continue

        # Ignorar filas con CUIT vacío o nulo antes de agrupar
        grupo_cuit = grupo[grupo["_cuit"].str.strip().ne("") & grupo["_cuit"].notna()]
        if grupo_cuit.empty:
            continue

        por_cuit = grupo_cuit.groupby("_cuit")["_monto"].sum()
        if por_cuit.empty:
            continue

        participaciones = por_cuit / total
        hhi = round((participaciones ** 2).sum() * 10_000, 1)

        cuit_top = por_cuit.idxmax()
        pct_top  = participaciones[cuit_top] * 100

        nivel = (
            "🔴 Alta concentración" if hhi > HHI_ALTO
            else "🟡 Moderada" if hhi > HHI_MEDIO
            else "🟢 Competitivo"
        )

        resultados.append({
            "organismo":        org,
            "hhi":              hhi,
            "nivel":            nivel,
            "total_adj":        len(grupo),
            "cuits_distintos":  grupo["_cuit"].nunique(),
            "monto_total":      total,
            "cuit_top":         cuit_top,
            "pct_cuit_top":     round(pct_top, 1),
        })

    if not resultados:
        print("  ✅ Sin datos suficientes para calcular HHI\n")
        return pd.DataFrame()

    df_hhi = (
        pd.DataFrame(resultados)
        .sort_values("hhi", ascending=False)
        .reset_index(drop=True)
    )

    print(f"\n  TOP {top_n} ORGANISMOS POR ÍNDICE HHI\n")
    print(f"  {'Organismo':<50}  {'HHI':>6}  {'Adj':>5}  {'CUITs':>5}  "
          f"{'% Top CUIT':>10}  {'Nivel'}")
    print("  " + "-" * 110)
    for _, row in df_hhi.head(top_n).iterrows():
        print(f"  {str(row['organismo'])[:50]:<50}  "
              f"{row['hhi']:>6.0f}  "
              f"{int(row['total_adj']):>5}  "
              f"{int(row['cuits_distintos']):>5}  "
              f"{row['pct_cuit_top']:>9.1f}%  "
              f"{row['nivel']}")

    alta = (df_hhi["hhi"] > HHI_ALTO).sum()
    print(f"\n  🔴 Organismos con alta concentración (HHI>{HHI_ALTO}): {alta}")
    print()
    return df_hhi if exportar_df else pd.DataFrame()


# ─────────────────────────────────────────
# 5. PROVEEDORES FANTASMA
# CUITs adjudicados que no aparecen en TGN
# Ganaron contratos pero no cobraron
# Puede indicar: empresa pantalla, datos incompletos,
# o cobro diferido fuera del período analizado
# ─────────────────────────────────────────
def detectar_fantasmas(df, exportar_df=False):
    print("━" * 60)
    print("👻 PROVEEDORES FANTASMA (adjudicados sin cobro en TGN)")
    print("━" * 60)

    df, col_org, col_cuit, col_fecha = preparar_df(df)

    col_tgn = next(
        (c for c in ["cobro_en_tgn", "monto_cobrado_tgn"] if c in df.columns), None
    )
    col_prov = next(
        (c for c in ["proveedor_adjudicado", "proveedor_nombre"] if c in df.columns), None
    )

    if not col_cuit or not col_tgn:
        print("  ❌ Faltan columnas de CUIT o TGN\n")
        return pd.DataFrame()

    df_valido = df[df["_cuit"].str.strip().ne("") & df["_monto"].gt(0)].copy()

    # Determinar quién nunca cobró
    if df["cobro_en_tgn"].dtype == object if "cobro_en_tgn" in df.columns else False:
        cobro_mask = df_valido["cobro_en_tgn"].isin(["✅ SÍ", "True", True])
    else:
        cobro_mask = df_valido["_tgn"].gt(0)

    cuits_cobraron = set(df_valido[cobro_mask]["_cuit"].unique())
    df_sin_cobro   = df_valido[~df_valido["_cuit"].isin(cuits_cobraron)].copy()

    if df_sin_cobro.empty:
        print("  ✅ Todos los proveedores adjudicados aparecen en TGN\n")
        return pd.DataFrame()

    grp = (
        df_sin_cobro.groupby("_cuit")
        .agg(
            contratos=("_monto", "count"),
            monto_total=("_monto", "sum"),
            **({"nombre": pd.NamedAgg(column=col_prov, aggfunc="first")} if col_prov else {}),
            **({"organismo": pd.NamedAgg(column=col_org, aggfunc="first")} if col_org else {}),
        )
        .reset_index()
        .sort_values("monto_total", ascending=False)
    )

    print(f"\n  {len(grp)} proveedores adjudicados sin registro de cobro en TGN\n")
    print(f"  {'CUIT':<20}  {'Nombre':<40}  {'Contratos':>9}  {'Monto Total':>18}")
    print("  " + "-" * 95)
    for _, row in grp.head(20).iterrows():
        nombre = str(row.get("nombre", ""))[:40] if "nombre" in grp.columns else ""
        print(f"  {str(row['_cuit']):<20}  {nombre:<40}  "
              f"{int(row['contratos']):>9}  "
              f"${row['monto_total']:>17,.0f}")

    if len(grp) > 20:
        print(f"  ... y {len(grp) - 20} más")

    print(f"\n  ⚠️  Nota: puede indicar cobro diferido, datos incompletos o empresa pantalla")
    print()
    return grp if exportar_df else pd.DataFrame()


# ─────────────────────────────────────────
# EXPORTAR REPORTE COMPLETO
# ─────────────────────────────────────────
def exportar_excel(df_frag, df_unico, df_rafaga, df_hhi, df_fantasmas):
    carpeta = os.path.join(DATA_DIR, "analisis")
    os.makedirs(carpeta, exist_ok=True)
    hoy     = datetime.now().strftime("%Y-%m-%d")
    archivo = os.path.join(carpeta, f"concentracion_{hoy}.xlsx")

    with pd.ExcelWriter(archivo, engine="openpyxl") as writer:
        resumen = pd.DataFrame([{
            "generado_en":               datetime.now().strftime("%Y-%m-%d %H:%M"),
            "organismos_fragmentacion":  len(df_frag),
            "organismos_prov_unico":     len(df_unico),
            "proveedores_rafaga":        len(df_rafaga),
            "organismos_hhi_alto":       (df_hhi["hhi"] > HHI_ALTO).sum() if not df_hhi.empty else 0,
            "proveedores_fantasma":      len(df_fantasmas),
            "umbral_licitacion_ars":     UMBRAL_LICITACION,
            "ventana_rafaga_dias":       VENTANA_RAFAGA_DIAS,
        }])
        resumen.to_excel(writer, sheet_name="📋 Resumen", index=False)

        if not df_frag.empty:
            df_frag.to_excel(writer, sheet_name="🔪 Fragmentación", index=False)
        if not df_unico.empty:
            df_unico.to_excel(writer, sheet_name="🔒 Proveedor Único", index=False)
        if not df_rafaga.empty:
            df_rafaga.to_excel(writer, sheet_name="⚡ Ráfagas", index=False)
        if not df_hhi.empty:
            df_hhi.to_excel(writer, sheet_name="📐 HHI Concentración", index=False)
        if not df_fantasmas.empty:
            df_fantasmas.to_excel(writer, sheet_name="👻 Fantasmas", index=False)

    print(f"  💾 Reporte exportado: {archivo}\n")
    return archivo


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    # ── Guardia fin de semana y feriados argentinos ───────────────────────────
    hoy = datetime.now()

    try:
        import holidays
        feriados_ar = holidays.Argentina(years=hoy.year)
        es_feriado = hoy.date() in feriados_ar
        nombre_feriado = feriados_ar.get(hoy.date(), "")
    except ImportError:
        es_feriado = False
        nombre_feriado = ""

    if hoy.weekday() >= 5:
        dia = "sábado" if hoy.weekday() == 5 else "domingo"
        print(f"⏭️  Hoy es {dia} {hoy.strftime('%Y-%m-%d')} — no se ejecuta análisis en fin de semana.")
        print("   Script finalizado sin ejecutar análisis.")
        exit(0)

    if es_feriado:
        print(f"⏭️  Hoy es feriado nacional: '{nombre_feriado}' ({hoy.strftime('%Y-%m-%d')}) — no se ejecuta análisis.")
        print("   Script finalizado sin ejecutar análisis.")
        exit(0)
    # ──────────────────────────────────────────────────────────────────────────

    parser = argparse.ArgumentParser(
        description="Análisis de Concentración — Monitor de Fenómenos Corruptivos"
    )
    parser.add_argument("--fragmentacion",   action="store_true")
    parser.add_argument("--proveedor-unico", action="store_true")
    parser.add_argument("--rafaga",          action="store_true")
    parser.add_argument("--hhi",             action="store_true")
    parser.add_argument("--fantasmas",       action="store_true")
    parser.add_argument("--exportar",        action="store_true", help="Genera Excel con todos los análisis")
    parser.add_argument("--top",             type=int, default=15, help="Top N para HHI (default: 15)")
    args = parser.parse_args()

    print("\n🔍 ANÁLISIS DE CONCENTRACIÓN Y FRAGMENTACIÓN")
    print(f"   Teoría: Ph.D. Vicente Humberto Monteverde (2020)")
    print(f"   Fecha:  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    print("🔄 Cargando datos históricos...")
    df = cargar_historico()
    if df.empty:
        sys.exit(1)

    # Si no se especifica ningún análisis, correr todos
    todos = not any([
        args.fragmentacion, args.proveedor_unico,
        args.rafaga, args.hhi, args.fantasmas
    ])

    exportar = args.exportar or todos

    df_frag     = pd.DataFrame()
    df_unico    = pd.DataFrame()
    df_rafaga   = pd.DataFrame()
    df_hhi      = pd.DataFrame()
    df_fantasmas = pd.DataFrame()

    if todos or args.fragmentacion:
        df_frag = detectar_fragmentacion(df, exportar_df=exportar)

    if todos or args.proveedor_unico:
        df_unico = detectar_proveedor_unico(df, exportar_df=exportar)

    if todos or args.rafaga:
        df_rafaga = detectar_rafaga(df, exportar_df=exportar)

    if todos or args.hhi:
        df_hhi = analisis_hhi(df, top_n=args.top, exportar_df=exportar)

    if todos or args.fantasmas:
        df_fantasmas = detectar_fantasmas(df, exportar_df=exportar)

    if exportar:
        exportar_excel(df_frag, df_unico, df_rafaga, df_hhi, df_fantasmas)

    # Resumen final
    print("=" * 60)
    print("📊 RESUMEN DE ALERTAS DE CONCENTRACIÓN")
    print("=" * 60)
    print(f"  🔪 Fragmentación (organismos):   {len(df_frag)}")
    print(f"  🔒 Proveedor único (organismos): {len(df_unico)}")
    print(f"  ⚡ Ráfagas (proveedores):        {len(df_rafaga)}")
    hhi_alto = (df_hhi["hhi"] > HHI_ALTO).sum() if not df_hhi.empty else 0
    print(f"  📐 HHI alto (organismos):        {hhi_alto}")
    print(f"  👻 Fantasmas (proveedores):      {len(df_fantasmas)}")
    print()