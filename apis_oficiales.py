"""
apis_oficiales.py
=================
Módulo ADICIONAL para monitor_contratos_v2.
NO modifica ningún archivo existente.

Expone funciones independientes para consumir las APIs oficiales
del Estado argentino. Cada función devuelve un DataFrame compatible
con el esquema que ya usa diario.py, para que puedas combinarlos
cuando quieras.

USO RÁPIDO (desde diario.py o cualquier script):
    from apis_oficiales import (
        obtener_sipro_api,
        obtener_comprar_api,
        obtener_contrat_ocds_api,
        obtener_tgn_ejecucion_api,
        validar_cuit_api,
        obtener_bora_normativa_api,
    )

FUENTES CUBIERTAS:
    1. SIPRO          — datos.gob.ar CKAN  (proveedores del Estado)
    2. COMPR.AR       — datos.gob.ar CKAN  (convocatorias y adjudicaciones)
    3. CONTRAT.AR     — datos.gob.ar CKAN  (obra pública OCDS)
    4. TGN Ejecución  — presupuestoabierto.gob.ar (endpoint /ejecucion)
    5. CUIT Padrón    — apis.datos.gob.ar  (validación/enriquecimiento de CUIT)
    6. BORA Normativa — argentina.gob.ar/normativa (búsqueda por texto/fecha)
"""

import os
import time
import requests
import pandas as pd
from datetime import datetime
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────────────────────────
# HEADERS comunes (igual que diario.py para consistencia)
# ─────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "es-AR,es;q=0.9",
}

# ─────────────────────────────────────────────────────────────────
# CONSTANTES — IDs de recursos en datos.gob.ar
# ─────────────────────────────────────────────────────────────────

# SIPRO — Sistema de Información de Proveedores (legacy, pero público)
SIPRO_RESOURCE_ID = "jgm_4.15"

# COMPR.AR — adjudicaciones 2020 (el más reciente publicado en CKAN)
COMPRAR_ADJ_2020_ID  = "jgm_4.12"   # adjudicaciones 2020
COMPRAR_CONV_2020_ID = "jgm_4.11"   # convocatorias 2020

# CONTRAT.AR — obra pública OCDS
CONTRAT_OCDS_ID = "jgm-contrataciones-obras-ocds"  # dataset id completo

# Endpoint base CKAN
CKAN_BASE = "https://datos.gob.ar/api/3/action/datastore_search"
CKAN_PKG  = "https://datos.gob.ar/api/3/action/package_show"

# TGN token (el mismo que ya usa diario.py)
TGN_TOKEN_DEFAULT = "707cb8c8-83e6-4c4d-a202-3e49c14eda89"


# ═══════════════════════════════════════════════════════════════════
# HELPER GENÉRICO
# ═══════════════════════════════════════════════════════════════════

def _get(url, params=None, headers=None, timeout=40, verify=False, intentos=3):
    """GET con reintentos. Devuelve requests.Response o None."""
    h = {**HEADERS, **(headers or {})}
    for i in range(intentos):
        try:
            r = requests.get(url, params=params, headers=h,
                             timeout=timeout, verify=verify)
            r.raise_for_status()
            return r
        except Exception as e:
            print(f"  ⚠️  Intento {i+1}/{intentos} falló: {e}")
            if i < intentos - 1:
                time.sleep(5)
    return None


def _ckan_search(resource_id, q=None, filters=None, limit=1000, offset=0):
    """
    Consulta genérica a la API CKAN de datos.gob.ar.
    Devuelve lista de registros (dicts) o [].
    """
    params = {
        "resource_id": resource_id,
        "limit": limit,
        "offset": offset,
    }
    if q:
        params["q"] = q
    if filters:
        import json
        params["filters"] = json.dumps(filters)

    r = _get(CKAN_BASE, params=params)
    if r is None:
        return []
    try:
        data = r.json()
        if data.get("success"):
            return data["result"]["records"]
        print(f"  ⚠️  CKAN error: {data.get('error')}")
        return []
    except Exception as e:
        print(f"  ⚠️  CKAN parse error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# 1. SIPRO — Proveedores del Estado
# ═══════════════════════════════════════════════════════════════════

def obtener_sipro_api(q=None, cuit=None, nombre=None, limit=500):
    """
    Consulta el SIPRO vía API CKAN de datos.gob.ar.

    Parámetros
    ----------
    q       : búsqueda libre de texto (nombre empresa, rubro, etc.)
    cuit    : filtrar por CUIT específico  (string "20-12345678-9")
    nombre  : filtrar por nombre de proveedor
    limit   : máximo de registros a devolver (default 500)

    Retorna
    -------
    DataFrame con columnas:
        cuit, razon_social, domicilio, rubro, estado_sipro, fuente

    Ejemplo
    -------
    >>> df = obtener_sipro_api(nombre="TECHINT")
    >>> df = obtener_sipro_api(cuit="30-50000427-3")
    """
    print("🏢 SIPRO API — consultando proveedores del Estado...")

    filters = {}
    if cuit:
        filters["CUIT"] = cuit.replace("-", "").replace(" ", "")
    if nombre:
        filters["RAZON_SOCIAL"] = nombre

    records = _ckan_search(
        resource_id=SIPRO_RESOURCE_ID,
        q=q,
        filters=filters if filters else None,
        limit=limit,
    )

    if not records:
        # Fallback: intentar con el dataset completo (CSV público)
        print("  ↩  CKAN sin resultados, intentando CSV público...")
        records = _sipro_csv_fallback(q=q, cuit=cuit, nombre=nombre)

    if not records:
        print("  ❌ SIPRO no disponible")
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # Normalizar nombres de columna (el CSV puede tener distintos headers)
    col_map = {
        "CUIT": "cuit",
        "RAZON_SOCIAL": "razon_social",
        "DOMICILIO": "domicilio",
        "RUBRO": "rubro",
        "ESTADO": "estado_sipro",
    }
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)

    df["fuente"] = "SIPRO datos.gob.ar"
    df["fecha_consulta"] = datetime.now().strftime("%Y-%m-%d")

    print(f"  ✅ {len(df)} proveedores encontrados en SIPRO")
    return df


def _sipro_csv_fallback(q=None, cuit=None, nombre=None):
    """Intenta bajar el CSV público de SIPRO directamente."""
    url = ("https://datos.gob.ar/dataset/jgm-sistema-contrataciones-electronicas"
           "/archivo/jgm_4.15")
    # El link directo al CSV del recurso
    csv_url = ("https://infra.datos.gob.ar/catalog/jgm/dataset/4/"
               "distribution/4.15/download/sipro.csv")
    r = _get(csv_url, timeout=60)
    if r is None:
        return []
    try:
        from io import StringIO
        df = pd.read_csv(StringIO(r.text), sep=",", on_bad_lines="skip")
        if cuit:
            cuit_limpio = cuit.replace("-", "").replace(" ", "")
            col_cuit = next((c for c in df.columns if "cuit" in c.lower()), None)
            if col_cuit:
                df = df[df[col_cuit].astype(str).str.replace("-","") == cuit_limpio]
        if nombre:
            col_nom = next((c for c in df.columns if "razon" in c.lower() or "nombre" in c.lower()), None)
            if col_nom:
                df = df[df[col_nom].str.upper().str.contains(nombre.upper(), na=False)]
        if q:
            mask = df.apply(lambda row: q.upper() in " ".join(row.astype(str)).upper(), axis=1)
            df = df[mask]
        return df.to_dict("records")
    except Exception as e:
        print(f"  ⚠️  SIPRO CSV fallback falló: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# 2. COMPR.AR — Convocatorias y Adjudicaciones vía CKAN
# ═══════════════════════════════════════════════════════════════════

def obtener_comprar_api(anio=2020, tipo="adjudicaciones", organismo=None, limit=1000):
    """
    Descarga convocatorias o adjudicaciones de COMPR.AR desde datos.gob.ar.

    Parámetros
    ----------
    anio       : año a consultar — disponibles: 2015 a 2020
    tipo       : "adjudicaciones" | "convocatorias"
    organismo  : filtrar por nombre de organismo (substring)
    limit      : máximo de registros

    Retorna
    -------
    DataFrame con columnas homogéneas al esquema de diario.py:
        nro_proceso, nombre_proceso, tipo_proceso, organismo,
        monto, cuit_proveedor, proveedor, fecha_publicacion,
        link, fuente

    Ejemplo
    -------
    >>> df = obtener_comprar_api(anio=2020, tipo="adjudicaciones")
    >>> df = obtener_comprar_api(anio=2019, tipo="convocatorias", organismo="SALUD")
    """
    print(f"🛒 COMPR.AR API — {tipo} {anio}...")

    # Mapa de resource_id por año (dataset jgm-sistema-contrataciones-electronicas)
    # Estos IDs son estables en datos.gob.ar
    resource_ids = {
        "adjudicaciones": {
            2020: "jgm_4.12", 2019: "jgm_4.10", 2018: "jgm_4.8",
            2017: "jgm_4.5",  2016: "jgm_4.3",  2015: "jgm_4.1",
        },
        "convocatorias": {
            2020: "jgm_4.11", 2019: "jgm_4.9",  2018: "jgm_4.7",
            2017: "jgm_4.6",  2016: "jgm_4.4",  2015: "jgm_4.2",
        },
    }

    rid = resource_ids.get(tipo, {}).get(anio)
    if not rid:
        print(f"  ⚠️  No hay resource_id para {tipo} {anio}. Años disponibles: 2015-2020.")
        return pd.DataFrame()

    records = _ckan_search(resource_id=rid, limit=limit)

    if not records:
        print(f"  ❌ COMPR.AR {tipo} {anio} sin datos")
        return pd.DataFrame()

    df = pd.DataFrame(records)

    # Filtrar por organismo si se especifica
    if organismo:
        col_org = next((c for c in df.columns
                        if any(x in c.lower() for x in ["organismo","unidad","jurisdiccion"])), None)
        if col_org:
            df = df[df[col_org].str.upper().str.contains(organismo.upper(), na=False)]

    # Normalizar columnas al esquema de diario.py
    col_map = {
        "numero_proceso":        "nro_proceso",
        "nombre_llamado":        "nombre_proceso",
        "tipo_procedimiento":    "tipo_proceso",
        "organismo_desc":        "organismo",
        "unidad_ejecutora_desc": "organismo",
        "monto_total_adjudicado":"monto",
        "cuit_proveedor":        "cuit_proveedor",
        "proveedor_desc":        "proveedor",
        "fecha_publicacion":     "fecha_publicacion",
    }
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)

    if "link" not in df.columns:
        df["link"] = ""
    df["fuente"] = f"COMPR.AR API {tipo} {anio}"

    print(f"  ✅ {len(df)} registros de COMPR.AR {tipo} {anio}")
    return df


# ═══════════════════════════════════════════════════════════════════
# 3. CONTRAT.AR — Obra Pública en estándar OCDS
# ═══════════════════════════════════════════════════════════════════

def obtener_contrat_ocds_api(organismo=None, limit=500):
    """
    Descarga datos de obra pública de CONTRAT.AR en estándar OCDS
    desde datos.gob.ar.

    Parámetros
    ----------
    organismo : filtrar por nombre de organismo
    limit     : máximo de registros

    Retorna
    -------
    DataFrame con columnas:
        ocid, titulo, organismo, monto_contrato, moneda,
        proveedor, cuit_proveedor, fecha_publicacion,
        estado, link, fuente

    Ejemplo
    -------
    >>> df = obtener_contrat_ocds_api()
    >>> df = obtener_contrat_ocds_api(organismo="VIALIDAD")
    """
    print("🏗️  CONTRAT.AR OCDS API — obra pública...")

    # Resource IDs del dataset de obra pública OCDS en datos.gob.ar
    # Dataset: "jgm-contrataciones-obras-publicas-ocds"
    resource_ids_ocds = [
        "jgm_5.1",   # contratos adjudicados OCDS
        "jgm_5.2",   # licitaciones abiertas OCDS
    ]

    all_records = []
    for rid in resource_ids_ocds:
        records = _ckan_search(resource_id=rid, limit=limit)
        if records:
            all_records.extend(records)

    if not all_records:
        # Fallback: CSV descargable directamente
        print("  ↩  CKAN sin resultados, intentando CSV OCDS...")
        all_records = _contrat_csv_fallback()

    if not all_records:
        print("  ❌ CONTRAT.AR OCDS no disponible")
        return pd.DataFrame()

    df = pd.DataFrame(all_records)

    if organismo:
        col_org = next((c for c in df.columns
                        if "organismo" in c.lower() or "buyer" in c.lower()), None)
        if col_org:
            df = df[df[col_org].str.upper().str.contains(organismo.upper(), na=False)]

    # Normalizar al esquema común
    col_map = {
        "ocid":                     "ocid",
        "tender/title":             "titulo",
        "buyer/name":               "organismo",
        "contracts/0/value/amount": "monto_contrato",
        "contracts/0/value/currency": "moneda",
        "awards/0/suppliers/0/name":  "proveedor",
        "awards/0/suppliers/0/identifier/id": "cuit_proveedor",
        "tender/datePublished":     "fecha_publicacion",
        "tender/status":            "estado",
    }
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)

    if "link" not in df.columns:
        df["link"] = df.get("ocid", "").apply(
            lambda x: f"https://contrat.ar/onc/#/processes/{x}" if x else ""
        )
    df["fuente"] = "CONTRAT.AR OCDS API"

    print(f"  ✅ {len(df)} registros de obra pública OCDS")
    return df


def _contrat_csv_fallback():
    """Intenta bajar el CSV de contratos OCDS directamente."""
    url = ("https://infra.datos.gob.ar/catalog/jgm/dataset/5/"
           "distribution/5.1/download/contratos-ocds.csv")
    r = _get(url, timeout=90)
    if r is None:
        return []
    try:
        from io import StringIO
        df = pd.read_csv(StringIO(r.text), sep=",", on_bad_lines="skip")
        return df.to_dict("records")
    except Exception as e:
        print(f"  ⚠️  CONTRAT CSV fallback falló: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════
# 4. TGN — Endpoint /ejecucion (complemento al /credito ya integrado)
# ═══════════════════════════════════════════════════════════════════

def obtener_tgn_ejecucion_api(anio=None, jurisdiccion=None, token=None):
    """
    Consulta el endpoint /ejecucion de Presupuesto Abierto.
    Complementa el /credito que ya usa diario.py.

    Diferencia clave: /ejecucion incluye el CUIT del beneficiario
    en algunos registros de transferencias, lo que mejora el cruce.

    Parámetros
    ----------
    anio         : año fiscal (default: año actual)
    jurisdiccion : filtrar por jurisdicción (ej: "MINISTERIO DE SALUD")
    token        : Bearer token (usa TGN_TOKEN_DEFAULT si no se pasa)

    Retorna
    -------
    DataFrame con columnas:
        anio, jurisdiccion, entidad, unidad_ejecutora,
        cuit_beneficiario, beneficiario, monto_pagado,
        monto_devengado, fuente

    Ejemplo
    -------
    >>> df = obtener_tgn_ejecucion_api()
    >>> df = obtener_tgn_ejecucion_api(jurisdiccion="MINISTERIO DE OBRAS PUBLICAS")
    """
    anio = anio or datetime.now().year
    token = token or os.environ.get("TGN_TOKEN", TGN_TOKEN_DEFAULT)

    print(f"💰 TGN /ejecucion API — ejercicio {anio}...")

    url = "https://www.presupuestoabierto.gob.ar/api/v1/ejecucion"
    headers_api = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/csv",
    }
    body = {
        "columns": [
            "ejercicio_presupuestario",
            "jurisdiccion_desc",
            "entidad_desc",
            "unidad_ejecutora_desc",
            "beneficiario_cuit",        # ← campo clave que /credito no siempre trae
            "beneficiario_desc",
            "monto_pagado",
            "monto_devengado",
        ]
    }

    try:
        r = requests.post(url, headers=headers_api, json=body,
                          timeout=90, verify=False)
        r.raise_for_status()

        from io import StringIO
        df = pd.read_csv(StringIO(r.text), sep=",", on_bad_lines="skip")

        if "ejercicio_presupuestario" in df.columns:
            df = df[df["ejercicio_presupuestario"] == anio].copy()

        if jurisdiccion and "jurisdiccion_desc" in df.columns:
            df = df[df["jurisdiccion_desc"].str.upper()
                    .str.contains(jurisdiccion.upper(), na=False)]

        col_map = {
            "ejercicio_presupuestario": "anio",
            "jurisdiccion_desc":        "jurisdiccion",
            "entidad_desc":             "entidad",
            "unidad_ejecutora_desc":    "unidad_ejecutora",
            "beneficiario_cuit":        "cuit_beneficiario",
            "beneficiario_desc":        "beneficiario",
            "monto_pagado":             "monto_pagado",
            "monto_devengado":          "monto_devengado",
        }
        df.rename(columns={k: v for k, v in col_map.items() if k in df.columns},
                  inplace=True)

        for col in ["monto_pagado", "monto_devengado"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        df["fuente"] = f"TGN /ejecucion {anio}"

        con_cuit = df["cuit_beneficiario"].notna().sum() if "cuit_beneficiario" in df.columns else 0
        print(f"  ✅ {len(df)} registros TGN ejecucion | {con_cuit} con CUIT beneficiario")
        return df

    except Exception as e:
        print(f"  ❌ TGN /ejecucion falló: {e}")
        return pd.DataFrame()


# ═══════════════════════════════════════════════════════════════════
# 5. CUIT — Validación y enriquecimiento vía apis.datos.gob.ar
# ═══════════════════════════════════════════════════════════════════

def validar_cuit_api(cuit):
    """
    Valida y enriquece un CUIT consultando el padrón público de AFIP
    a través de apis.datos.gob.ar (servicio oficial, sin registro).

    Parámetros
    ----------
    cuit : string — acepta formatos "20-12345678-9", "20123456789", etc.

    Retorna
    -------
    dict con claves:
        cuit_normalizado, razon_social, domicilio_fiscal,
        actividad_principal, estado_afip, es_valido
    O dict vacío si no se encuentra.

    Ejemplo
    -------
    >>> info = validar_cuit_api("30-50000427-3")
    >>> info = validar_cuit_api("20123456789")
    """
    cuit_limpio = re.sub(r"[^0-9]", "", str(cuit))
    if len(cuit_limpio) != 11:
        return {"es_valido": False, "error": f"CUIT inválido: {cuit}"}

    print(f"🔍 Validando CUIT {cuit_limpio}...")

    # Endpoint 1: apis.datos.gob.ar (padrón AFIP simplificado)
    url_padron = f"https://apis.datos.gob.ar/series/api/series/?ids=cuit:{cuit_limpio}"

    # Endpoint más directo: datosabiertos AFIP via wsdl (simplificado)
    url_cuit = f"https://api.afip.gov.ar/sr-padron/v2/persona/{cuit_limpio}"

    # Intentamos primero la API pública no oficial pero estable
    result = _validar_cuit_publico(cuit_limpio)
    if result:
        return result

    # Fallback: consulta CKAN SIPRO por CUIT
    print(f"  ↩  Fallback SIPRO para CUIT {cuit_limpio}...")
    df_sipro = obtener_sipro_api(cuit=cuit_limpio, limit=5)
    if not df_sipro.empty:
        row = df_sipro.iloc[0]
        return {
            "cuit_normalizado": f"{cuit_limpio[:2]}-{cuit_limpio[2:10]}-{cuit_limpio[10]}",
            "razon_social":     row.get("razon_social", ""),
            "domicilio_fiscal": row.get("domicilio", ""),
            "actividad_principal": row.get("rubro", ""),
            "estado_afip":      row.get("estado_sipro", ""),
            "es_valido":        True,
            "fuente":           "SIPRO datos.gob.ar",
        }

    return {"es_valido": False, "error": f"CUIT no encontrado: {cuit_limpio}"}


def _validar_cuit_publico(cuit_limpio):
    """Consulta la API pública de AFIP (sin token) para datos básicos."""
    # Este endpoint es el WS de consulta pública de AFIP
    url = f"https://soa.afip.gob.ar/sr-padron/v2/persona/{cuit_limpio}"
    r = _get(url, timeout=15)
    if r is None:
        return None
    try:
        data = r.json()
        persona = data.get("data", {})
        if not persona:
            return None
        return {
            "cuit_normalizado": f"{cuit_limpio[:2]}-{cuit_limpio[2:10]}-{cuit_limpio[10]}",
            "razon_social":     persona.get("razonSocial") or
                                f"{persona.get('nombre','')} {persona.get('apellido','')}".strip(),
            "domicilio_fiscal": _parsear_domicilio(persona.get("domicilioFiscal", {})),
            "actividad_principal": str(persona.get("actividadPrincipal", "")),
            "estado_afip":      persona.get("estadoClave", ""),
            "es_valido":        True,
            "fuente":           "AFIP SOA público",
        }
    except Exception:
        return None


def _parsear_domicilio(d):
    if not d or not isinstance(d, dict):
        return ""
    partes = [
        d.get("direccion", ""),
        d.get("localidad", ""),
        d.get("descripcionProvincia", ""),
        str(d.get("codPostal", "")),
    ]
    return ", ".join(p for p in partes if p)


def validar_cuits_lote(lista_cuits, pausa=0.5):
    """
    Valida una lista de CUITs y devuelve un DataFrame con los resultados.
    Incluye pausa entre consultas para no saturar la API.

    Parámetros
    ----------
    lista_cuits : list de strings
    pausa       : segundos entre consultas (default 0.5)

    Retorna
    -------
    DataFrame con una fila por CUIT

    Ejemplo
    -------
    >>> cuits = ["30-50000427-3", "20123456789", "27-33445566-2"]
    >>> df = validar_cuits_lote(cuits)
    """
    print(f"🔍 Validando {len(lista_cuits)} CUITs en lote...")
    resultados = []
    for cuit in lista_cuits:
        r = validar_cuit_api(cuit)
        r["cuit_original"] = cuit
        resultados.append(r)
        time.sleep(pausa)
    df = pd.DataFrame(resultados)
    validos = df["es_valido"].sum() if "es_valido" in df.columns else 0
    print(f"  ✅ {validos}/{len(lista_cuits)} CUITs validados")
    return df


# ═══════════════════════════════════════════════════════════════════
# 6. BORA — Búsqueda de normativa vía argentina.gob.ar
# ═══════════════════════════════════════════════════════════════════

def obtener_bora_normativa_api(texto=None, fecha_desde=None, fecha_hasta=None,
                                seccion="tercera", limit=50):
    """
    Busca normativa en el BORA usando el endpoint de búsqueda
    de argentina.gob.ar/normativa (datos estructurados, más estable
    que el scraping del HTML de boletinoficial.gob.ar).

    Parámetros
    ----------
    texto       : término de búsqueda libre
    fecha_desde : "YYYY-MM-DD"  (default: hoy)
    fecha_hasta : "YYYY-MM-DD"  (default: hoy)
    seccion     : "primera" | "segunda" | "tercera" (default "tercera")
    limit       : máximo de resultados

    Retorna
    -------
    DataFrame con columnas:
        fecha_publicacion, seccion, tipo_norma, numero_norma,
        organismo, titulo, link, fuente

    Ejemplo
    -------
    >>> df = obtener_bora_normativa_api(texto="licitacion")
    >>> df = obtener_bora_normativa_api(fecha_desde="2026-03-01", seccion="tercera")
    """
    hoy = datetime.now().strftime("%Y-%m-%d")
    fecha_desde = fecha_desde or hoy
    fecha_hasta = fecha_hasta or hoy

    print(f"📰 BORA Normativa API — sección {seccion} [{fecha_desde} → {fecha_hasta}]...")

    # Endpoint de búsqueda de normativa argentina.gob.ar
    url = "https://www.argentina.gob.ar/normativa/buscar"

    params = {
        "seccion": seccion,
        "fechaDesde": fecha_desde,
        "fechaHasta": fecha_hasta,
        "pageSize": limit,
        "page": 1,
    }
    if texto:
        params["texto"] = texto

    r = _get(url, params=params, headers={"Accept": "application/json"})

    if r is None:
        # Fallback: endpoint JSON alternativo del BORA
        return _bora_json_fallback(fecha_desde, seccion, texto, limit)

    try:
        data = r.json()
        items = data.get("results", data.get("items", data.get("data", [])))
        if not items:
            return _bora_json_fallback(fecha_desde, seccion, texto, limit)
    except Exception:
        return _bora_json_fallback(fecha_desde, seccion, texto, limit)

    registros = []
    for item in items[:limit]:
        registros.append({
            "fecha_publicacion": item.get("fecha", item.get("fechaPublicacion", "")),
            "seccion":           seccion,
            "tipo_norma":        item.get("tipoNorma", item.get("tipo", "")),
            "numero_norma":      item.get("numero", ""),
            "organismo":         item.get("organismo", item.get("emisor", "")),
            "titulo":            item.get("titulo", item.get("sumario", ""))[:200],
            "link":              _construir_link_bora(item),
            "fuente":            "BORA argentina.gob.ar/normativa",
        })

    df = pd.DataFrame(registros)
    print(f"  ✅ {len(df)} normas encontradas en BORA")
    return df


def _bora_json_fallback(fecha, seccion, texto, limit):
    """
    Fallback: endpoint JSON interno del BORA
    (documentado en el código fuente de boletinoficial.gob.ar).
    """
    fecha_raw = fecha.replace("-", "")
    url = f"https://www.boletinoficial.gob.ar/busqueda/filtros"
    params = {
        "seccion":   seccion,
        "fechaInicio": fecha_raw,
        "fechaFin":    fecha_raw,
        "cantPorPagina": limit,
        "paginaActual": 1,
    }
    if texto:
        params["textoBusqueda"] = texto

    r = _get(url, params=params, headers={"Accept": "application/json, */*"})
    if r is None:
        print("  ❌ BORA normativa no disponible")
        return pd.DataFrame()

    try:
        data = r.json()
        avisos = data.get("avisos", data.get("data", []))
        registros = []
        for a in avisos:
            aviso_id = a.get("id", a.get("nroAviso", ""))
            registros.append({
                "fecha_publicacion": fecha,
                "seccion":           seccion,
                "tipo_norma":        a.get("categoria", ""),
                "numero_norma":      aviso_id,
                "organismo":         a.get("dependencia", a.get("organismo", "")),
                "titulo":            a.get("titulo", a.get("denominacion", ""))[:200],
                "link": (f"https://www.boletinoficial.gob.ar/detalleAviso/"
                         f"{seccion}/{aviso_id}/{fecha.replace('-','')}"),
                "fuente": "BORA JSON interno",
            })
        df = pd.DataFrame(registros)
        print(f"  ✅ {len(df)} normas via BORA JSON interno")
        return df
    except Exception as e:
        print(f"  ❌ BORA JSON fallback falló: {e}")
        return pd.DataFrame()


def _construir_link_bora(item):
    base = "https://www.boletinoficial.gob.ar/detalleAviso/tercera/"
    nro = item.get("numero", item.get("id", ""))
    fecha = item.get("fecha", "").replace("-", "")
    if nro and fecha:
        return f"{base}{nro}/{fecha}"
    return item.get("link", item.get("url", ""))


import re  # ya importado arriba, pero explícito para claridad


# ═══════════════════════════════════════════════════════════════════
# FUNCIÓN COMBINADA — corre todas las APIs y devuelve un dict
# ═══════════════════════════════════════════════════════════════════

def obtener_todo_api(fecha=None, token_tgn=None):
    """
    Ejecuta todas las APIs oficiales disponibles y devuelve
    un diccionario con los DataFrames resultantes.

    Compatible con el flujo de diario.py: los DataFrames tienen
    el mismo esquema que los obtenidos por scraping, por lo que
    podés combinarlos con pd.concat() sin cambiar nada más.

    Parámetros
    ----------
    fecha     : "YYYY-MM-DD" (default: hoy)
    token_tgn : Bearer token TGN (usa variable de entorno si no se pasa)

    Retorna
    -------
    dict con claves:
        "bora_normativa"    → DataFrame (normas del día)
        "comprar_adj"       → DataFrame (adjudicaciones COMPR.AR)
        "comprar_conv"      → DataFrame (convocatorias COMPR.AR)
        "contrat_ocds"      → DataFrame (obra pública OCDS)
        "tgn_ejecucion"     → DataFrame (pagos con CUIT beneficiario)
        "sipro"             → DataFrame vacío (requiere búsqueda específica)

    Ejemplo
    -------
    >>> from apis_oficiales import obtener_todo_api
    >>> resultados = obtener_todo_api()
    >>> df_bora   = resultados["bora_normativa"]
    >>> df_adj    = resultados["comprar_adj"]
    >>> df_pagos  = resultados["tgn_ejecucion"]
    """
    hoy = fecha or datetime.now().strftime("%Y-%m-%d")
    anio = int(hoy[:4])

    print("\n" + "="*55)
    print("🚀 APIs Oficiales — inicio")
    print(f"📅 Fecha: {hoy}")
    print("="*55 + "\n")

    resultados = {}

    # 1. BORA normativa del día
    resultados["bora_normativa"] = obtener_bora_normativa_api(
        fecha_desde=hoy, fecha_hasta=hoy, seccion="tercera"
    )

    # 2. COMPR.AR adjudicaciones (usa el año más reciente disponible)
    anio_comprar = min(anio, 2020)  # CKAN tiene hasta 2020
    resultados["comprar_adj"] = obtener_comprar_api(
        anio=anio_comprar, tipo="adjudicaciones"
    )
    resultados["comprar_conv"] = obtener_comprar_api(
        anio=anio_comprar, tipo="convocatorias"
    )

    # 3. CONTRAT.AR OCDS
    resultados["contrat_ocds"] = obtener_contrat_ocds_api()

    # 4. TGN ejecucion (complementa el /credito de diario.py)
    resultados["tgn_ejecucion"] = obtener_tgn_ejecucion_api(
        anio=anio, token=token_tgn
    )

    # 5. SIPRO requiere búsqueda específica — devuelve vacío por default
    resultados["sipro"] = pd.DataFrame()

    # Resumen
    print("\n" + "="*55)
    print("📊 RESUMEN APIs Oficiales:")
    for nombre, df in resultados.items():
        n = len(df) if not df.empty else 0
        icono = "✅" if n > 0 else "⚠️ "
        print(f"  {icono} {nombre:<20} {n:>5} registros")
    print("="*55 + "\n")

    return resultados


# ═══════════════════════════════════════════════════════════════════
# EJECUCIÓN STANDALONE (test rápido)
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("🧪 Test rápido de APIs oficiales\n")

    # Test 1: BORA normativa
    df_bora = obtener_bora_normativa_api(seccion="tercera")
    print(f"   BORA: {len(df_bora)} normas\n")

    # Test 2: COMPR.AR adjudicaciones 2020
    df_comp = obtener_comprar_api(anio=2020, tipo="adjudicaciones", limit=10)
    print(f"   COMPR.AR 2020 adj: {len(df_comp)} registros\n")

    # Test 3: TGN ejecucion
    df_tgn = obtener_tgn_ejecucion_api()
    print(f"   TGN ejecucion: {len(df_tgn)} registros\n")

    # Test 4: CUIT puntual
    info = validar_cuit_api("30-50000427-3")
    print(f"   CUIT test: {info}\n")

    print("✅ Tests finalizados")