"""
diario.py — Scraping diario con APIs oficiales
Monitor de Contratos v3 — Metodología Monteverde (2020)

Fuentes:
  BORA     → scraping HTML/PDF boletinoficial.gob.ar (sin API oficial)
  Comprar  → API datos abiertos infra.datos.gob.ar (CSV oficial ONC)
  TGN      → API oficial presupuestoabierto.gob.ar/api/v1 (Bearer token)

Variables de entorno requeridas:
  TGN_TOKEN   → token Bearer de Presupuesto Abierto
                (registrarse en presupuestoabierto.gob.ar/sici/api-pac)

Uso:
  python diario.py
"""

import io
import os
import re
import time
import logging
from datetime import datetime, date

import requests
import pandas as pd
from bs4 import BeautifulSoup

from analisis import aplicar_matriz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

HEADERS_HTML = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9",
    "Connection": "keep-alive",
}

TIMEOUT = 60
REINTENTOS = 3
ESPERA_ENTRE_REINTENTOS = 15
PAUSA_ENTRE_AVISOS = 1          # segundos entre requests a BORA
UMBRAL_LICITACION = 10_000_000  # ARS — debajo = contratación directa

# URLs ────────────────────────────────────────────────────────────────────────
BORA_SECCION3  = "https://www.boletinoficial.gob.ar/seccion/tercera"
BORA_PDF_TMPL  = "https://www.boletinoficial.gob.ar/pdf/aviso/tercera/{aviso_id}/{fecha_raw}"
BORA_HTML_TMPL = "https://www.boletinoficial.gob.ar/detalleAviso/tercera/{aviso_id}/{fecha_raw}"

# API interna Comprar.gob.ar — datos actuales (2021-hoy)
# Endpoint XHR reverse-engineered del portal
COMPRAR_API_URL = (
    "https://comprar.gob.ar/Consultas.aspx/ObtenerProcesosCompraGridSeleccion"
)
COMPRAR_LISTA_URL = "https://comprar.gob.ar/Compras.aspx?qs=W1HXHGHtH10="

# Fallback: CSV histórico datos.gob.ar (solo hasta 2020)
COMPRAR_ADJ_TMPL = (
    "https://infra.datos.gob.ar/catalog/jgm/dataset/4/"
    "distribution/4.{resource_id}/download/adjudicaciones-{anio}.csv"
)
COMPRAR_RESOURCE_IDS = {
    2020: 20,
    2019: 18,
    2018: 14,
}

# API Presupuesto Abierto TGN
TGN_API_URL    = "https://www.presupuestoabierto.gob.ar/api/v1/credito"
TGN_TOKEN      = os.getenv("TGN_TOKEN", "")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS GENERALES
# ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, *, verify_ssl: bool = False, **kwargs) -> requests.Response | None:
    """GET con reintentos y backoff."""
    for intento in range(1, REINTENTOS + 1):
        try:
            log.debug(f"GET [{intento}/{REINTENTOS}] {url[:90]}")
            r = requests.get(
                url, headers=HEADERS_HTML,
                timeout=TIMEOUT, verify=verify_ssl, **kwargs
            )
            r.raise_for_status()
            return r
        except Exception as e:
            log.warning(f"  ⚠️ Intento {intento} fallido: {e}")
            if intento < REINTENTOS:
                time.sleep(ESPERA_ENTRE_REINTENTOS)
    return None


def _normalizar_cuit(texto: str) -> str:
    """Extrae y normaliza CUIT/CUIL de un texto libre."""
    # Formato XX-XXXXXXXX-X
    m = re.search(r'\b(\d{2})-(\d{7,8})-(\d{1})\b', str(texto))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Sin guiones (11 dígitos)
    m = re.search(r'\b(20|23|24|27|30|33|34)\d{9}\b', str(texto))
    if m:
        raw = m.group(0)
        return f"{raw[:2]}-{raw[2:-1]}-{raw[-1]}"
    return ""


def _normalizar_monto(texto: str) -> float:
    """Convierte string de monto argentino a float."""
    if not texto or pd.isna(texto):
        return 0.0
    try:
        limpio = re.sub(r"[^\d,]", "", str(texto)).replace(",", ".")
        partes = limpio.split(".")
        if len(partes) > 2:
            limpio = "".join(partes[:-1]) + "." + partes[-1]
        return float(limpio) if limpio else 0.0
    except Exception:
        return 0.0


def _directorio_mes(d: date) -> str:
    carpeta = os.path.join(DATA_DIR, d.strftime("%Y-%m"))
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def _normalizar_organismo(nombre: str) -> str:
    """Normalización básica para cruce por organismo."""
    stop = {
        "NACIONAL", "GENERAL", "ARGENTINA", "PUBLICA", "ADMINISTRACION",
        "DIRECCION", "SECRETARIA", "MINISTERIO", "AGENCIA", "INSTITUTO",
        "FEDERAL", "REPUBLICA", "ESTADO", "SERVICIO", "OFICINA", "SOCIAL",
        "DE", "LA", "EL", "LOS", "LAS", "DEL", "Y",
    }
    n = re.sub(r"[^A-ZÁÉÍÓÚÑ\s]", "", nombre.upper())
    n = re.sub(r'\s+', ' ', n).strip()
    palabras = [p for p in n.split() if len(p) > 3 and p not in stop]
    return " ".join(palabras)


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACCIÓN 1 — BORA (HTML + PDF, sin API oficial)
# ─────────────────────────────────────────────────────────────────────────────

def _texto_aviso_bora(aviso_id: str, fecha_pub: str) -> str:
    """
    Obtiene el texto completo de un aviso BORA.
    BORA renderiza el cuerpo del aviso con JavaScript — el HTML estático
    solo trae el shell de la página. La única fuente confiable es el PDF.
    Estrategia: PDF primero, HTML como último recurso.
    """
    fecha_raw = fecha_pub.replace("-", "")

    # 1) PDF — fuente confiable con texto real del aviso
    url_pdf = BORA_PDF_TMPL.format(aviso_id=aviso_id, fecha_raw=fecha_raw)
    r = _get(url_pdf)
    if r:
        content_type = r.headers.get("Content-Type", "").lower()
        if "pdf" in content_type or r.content[:4] == b"%PDF":
            try:
                from pdfminer.high_level import extract_text as pdf_extract
                texto = pdf_extract(io.BytesIO(r.content))
                if texto and len(texto) > 50:
                    log.debug(f"  PDF OK aviso {aviso_id}: {len(texto)} chars")
                    return texto
            except Exception as e:
                log.debug(f"  PDF parse error aviso {aviso_id}: {e}")

    # 2) HTML — solo útil si BORA cambia a SSR en el futuro
    # Por ahora el body real viene vacío (JS dinámico), pero lo intentamos
    # buscando keywords concretos que confirmen contenido real
    url_html = BORA_HTML_TMPL.format(aviso_id=aviso_id, fecha_raw=fecha_raw)
    r = _get(url_html)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        # Buscar divs con contenido real (keywords del dominio)
        texto_pagina = soup.get_text(separator=" ", strip=True)
        keywords_reales = ["CUIT", "ADJUDIC", "PROVEEDOR", "MONTO", "IMPORTE",
                           "ADJUDICATARIO", "CONTRATO", "LICITACION"]
        if any(kw in texto_pagina.upper() for kw in keywords_reales):
            # Filtrar el header/nav — quedarse solo con lo que viene después
            # de la fecha de edición
            idx = texto_pagina.find("Edición del")
            if idx > 0:
                texto_pagina = texto_pagina[idx:]
            if len(texto_pagina) > 100:
                return texto_pagina

    log.debug(f"  Sin texto para aviso {aviso_id}")
    return ""


def _extraer_proveedor(texto: str) -> str:
    patrones = [
        r'PROVEEDOR ADJUDICADO[:\s]+([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)(?:\s*[,.]?\s*CUIT|\s*$)',
        r'ADJUDICATARIO[:\s]+([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)(?:\s*[,.]?\s*CUIT|\s*$)',
        r'adjudica[dó]\s+(?:la\s+firma\s+|a\s+la\s+firma\s+|a\s+)([A-ZÁÉÍÓÚÑ][^\n\r]{3,80}?)(?:\s*CUIT|\s*[,.])',
        r'firma\s+([A-ZÁÉÍÓÚÑ][^\n\r]{3,60}?)\s*[,.]?\s*(?:CUIT|C\.U\.I\.T)',
    ]
    for patron in patrones:
        m = re.search(patron, texto, re.IGNORECASE)
        if m:
            res = m.group(1).strip().rstrip(".,- ")
            if len(res) > 3:
                return res
    return ""


def _extraer_monto_bora(texto: str) -> str:
    m = re.search(
        r'(?:MONTO TOTAL ADJUDICADO|TOTAL ADJUDICADO|IMPORTE ADJUDICADO|MONTO ADJUDICADO)'
        r'[^\$\d]*\$?\s*([\d\.,]+)',
        texto, re.IGNORECASE
    )
    return "$" + m.group(1).strip() if m else ""


def extraer_bora(hoy: date | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Extrae el índice de Sección 3ra de BORA y el detalle de adjudicaciones.
    Retorna (df_adjudicaciones, df_licitaciones).
    """
    if hoy is None:
        hoy = date.today()

    log.info("📰 BORA Sección 3ra — índice")
    r = _get(BORA_SECCION3)
    if not r:
        log.error("BORA: no se pudo conectar")
        return pd.DataFrame(), pd.DataFrame()

    soup = BeautifulSoup(r.text, "html.parser")
    indice = []
    categoria_actual = ""

    for elem in soup.find_all(["h5", "a"]):
        if elem.name == "h5":
            categoria_actual = elem.get_text(strip=True)
        elif elem.name == "a" and "/detalleAviso/tercera/" in elem.get("href", ""):
            href = elem["href"]
            partes = href.strip("/").split("/")
            aviso_id  = partes[-2] if len(partes) >= 2 else ""
            fecha_raw = partes[-1] if len(partes) >= 1 else ""
            fecha_pub = (
                f"{fecha_raw[:4]}-{fecha_raw[4:6]}-{fecha_raw[6:]}"
                if len(fecha_raw) == 8 else fecha_raw
            )
            lineas = [l.strip() for l in elem.get_text().split("\n") if l.strip()]
            tipo_proceso = ""
            lineas_org = []
            for linea in lineas:
                if any(
                    p.lower() in linea.lower()
                    for p in ["Licitación", "Contratación", "Concurso",
                               "Adjudicación", "Subasta", "Compulsa", "Obra Pública"]
                ):
                    tipo_proceso = linea
                else:
                    lineas_org.append(linea)

            organismo = re.sub(r'\s*-\s*$', '', " ".join(lineas_org)).strip()
            es_adj = "ADJUDICACION" in categoria_actual.upper()

            indice.append({
                "fecha_publicacion": fecha_pub,
                "organismo": organismo,
                "tipo_proceso": tipo_proceso,
                "categoria": categoria_actual,
                "aviso_id": aviso_id,
                "es_adjudicacion": es_adj,
                "link_bora": "https://www.boletinoficial.gob.ar" + href,
                "fuente": "BORA Sección 3ra",
            })

    adj_raw   = [r for r in indice if r["es_adjudicacion"]]
    licit_raw = [r for r in indice if not r["es_adjudicacion"]]
    log.info(f"  Índice: {len(adj_raw)} adjudicaciones, {len(licit_raw)} licitaciones")

    # Detalle de adjudicaciones (CUIT, proveedor, monto)
    log.info("  Extrayendo detalle de adjudicaciones...")
    adj_detalle = []
    for row in adj_raw:
        time.sleep(PAUSA_ENTRE_AVISOS)
        texto = _texto_aviso_bora(row["aviso_id"], row["fecha_publicacion"])
        cuit      = _normalizar_cuit(texto)
        proveedor = _extraer_proveedor(texto)
        monto     = _extraer_monto_bora(texto)

        adj_detalle.append({
            "fecha_extraccion":      hoy.isoformat(),
            "fecha_publicacion":     row["fecha_publicacion"],
            "organismo_contratante": row["organismo"],
            "tipo_proceso":          row["tipo_proceso"],
            "aviso_id":              row["aviso_id"],
            "link_bora":             row["link_bora"],
            "proveedor_adjudicado":  proveedor,
            "cuit_proveedor":        cuit,
            "monto_adjudicado_bora": monto,
            "texto_muestra":         texto[:300] if texto else "SIN TEXTO",
            "fuente":                "BORA Adjudicaciones",
        })
        estado = f"✅ {cuit}" if cuit else "⚠️ sin CUIT"
        log.info(f"    {estado} | {proveedor[:45] or '—'} | {monto}")

    con_cuit = sum(1 for d in adj_detalle if d["cuit_proveedor"])
    log.info(f"  ✅ Adjudicaciones procesadas: {len(adj_detalle)} ({con_cuit} con CUIT)")

    df_adj  = pd.DataFrame(adj_detalle)
    df_lic  = pd.DataFrame(licit_raw)
    return df_adj, df_lic


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACCIÓN 2 — COMPRAR / datos.gob.ar (API oficial ONC)
# ─────────────────────────────────────────────────────────────────────────────

def extraer_comprar(anio: int | None = None) -> pd.DataFrame:
    """
    Extrae procesos de Comprar.gob.ar scrapeando el listado HTML del portal.
    Es la única fuente actual con datos 2021-hoy (datos.gob.ar solo llega a 2020).

    Fallback: CSV histórico de datos.gob.ar para años <= 2020.
    """
    if anio is None:
        anio = date.today().year

    log.info(f"🛒 Comprar.gob.ar — procesos recientes")

    r = _get(COMPRAR_LISTA_URL)
    if not r:
        log.error("  Comprar: no se pudo conectar al portal")
        return _extraer_comprar_csv_historico(anio)

    soup = BeautifulSoup(r.text, "lxml")

    # La tabla tiene id fijo en el portal
    tabla = soup.find("table", {"id": "ctl00_CPH1_GridListaPliegosAperturaProxima"})
    if not tabla:
        # Intentar cualquier tabla con suficientes columnas
        tablas = soup.find_all("table")
        tabla = next(
            (t for t in tablas if len(t.find_all("tr")) > 3),
            None
        )

    if not tabla:
        log.warning("  Comprar: tabla no encontrada en HTML")
        return _extraer_comprar_csv_historico(anio)

    filas = []
    headers = []

    # Extraer headers
    thead = tabla.find("thead") or tabla
    for th in thead.find_all("th"):
        headers.append(th.get_text(strip=True))

    # Extraer filas
    for tr in tabla.find_all("tr")[1:]:
        cols = tr.find_all("td")
        if len(cols) < 3:
            continue

        # Los links son javascript:__doPostBack — construir URL real
        # desde el nro_proceso (formato: XX/YY-NNNN-TTTNN)
        nro = cols[0].get_text(strip=True)
        link_real = (
            f"https://comprar.gob.ar/Compras.aspx?qs={nro}"
            if nro else ""
        )

        fila = {
            "nro_proceso":           cols[0].get_text(strip=True) if len(cols) > 0 else "",
            "nombre_proceso":        cols[1].get_text(strip=True) if len(cols) > 1 else "",
            "tipo_procedimiento":    cols[2].get_text(strip=True) if len(cols) > 2 else "",
            "fecha_apertura":        cols[3].get_text(strip=True) if len(cols) > 3 else "",
            "estado":                cols[4].get_text(strip=True) if len(cols) > 4 else "",
            "organismo_contratante": cols[5].get_text(strip=True) if len(cols) > 5 else "",
            "link_comprar":          link_real,
            "fuente_comprar":        "Comprar.gob.ar portal",
        }
        filas.append(fila)

    if not filas:
        log.warning("  Comprar: 0 filas parseadas")
        return _extraer_comprar_csv_historico(anio)

    df = pd.DataFrame(filas)
    log.info(f"  ✅ {len(df):,} procesos extraídos del portal")
    return df


def _extraer_comprar_csv_historico(anio: int) -> pd.DataFrame:
    """
    Fallback: descarga CSV de adjudicaciones históricas desde datos.gob.ar.
    Solo disponible para años 2018-2020.
    """
    rid = COMPRAR_RESOURCE_IDS.get(anio)
    if not rid:
        # Usar el más reciente disponible (2020)
        anio, rid = 2020, COMPRAR_RESOURCE_IDS[2020]
        log.warning(f"  Comprar fallback histórico: usando {anio}")

    url = COMPRAR_ADJ_TMPL.format(resource_id=rid, anio=anio)
    log.info(f"  Comprar CSV histórico: {url}")
    r = _get(url)
    if not r:
        log.error("  Comprar: fallback CSV también falló")
        return pd.DataFrame()

    try:
        df = pd.read_csv(io.StringIO(r.text), on_bad_lines="skip")
    except Exception as e:
        log.error(f"  Comprar CSV parse error: {e}")
        return pd.DataFrame()

    col_map = {
        "Número Procedimiento":  "nro_proceso",
        "Descripcion SAF":       "organismo_contratante",
        "Descripción Proveedor": "proveedor_adjudicado",
        "CUIT":                  "cuit_proveedor",
        "Monto":                 "monto_adjudicado_comprar",
        "Tipo de Procedimiento": "tipo_procedimiento",
        "Modalidad":             "modalidad",
        "Fecha de Adjudicación": "fecha_adjudicacion",
        "Rubros":                "rubros",
        "Descripcion UOC":       "unidad_operativa",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    if "cuit_proveedor" in df.columns:
        df["cuit_proveedor"] = df["cuit_proveedor"].apply(
            lambda x: _normalizar_cuit(str(x))
        )

    df["fuente_comprar"] = f"datos.gob.ar histórico {anio}"
    log.info(f"  ✅ {len(df):,} registros históricos cargados ({anio})")
    return df


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACCIÓN 3 — TGN / Presupuesto Abierto (API oficial)
# ─────────────────────────────────────────────────────────────────────────────

def extraer_tgn(anio: int | None = None) -> pd.DataFrame:
    """
    Consulta la API oficial de Presupuesto Abierto.
    Endpoint: POST /api/v1/credito
    Requiere TGN_TOKEN (Bearer) — registrar en presupuestoabierto.gob.ar/sici/api-pac

    Cruce posible: por organismo/jurisdicción (la API no expone CUIT beneficiario).
    """
    if anio is None:
        anio = date.today().year

    if not TGN_TOKEN:
        log.warning("⚠️ TGN_TOKEN no configurado — omitiendo TGN")
        log.warning("   Registrate en presupuestoabierto.gob.ar/sici/api-pac")
        return pd.DataFrame()

    log.info(f"💰 Presupuesto Abierto TGN — crédito ejecutado {anio}")

    # Endpoint REST interno — nivel beneficiario expone CUIT
    # Documentado en: presupuestoabierto.gob.ar/sici/rest-api/credito/ejecutado
    TGN_REST_URL = (
        "https://www.presupuestoabierto.gob.ar/sici/rest-api/"
        "credito/ejecutado"
    )

    params = {
        "anio":  anio,
        "nivel": "beneficiario",  # expone cuit + descripcion beneficiario
    }

    headers_api = {
        "Authorization": f"Bearer {TGN_TOKEN}",
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    # Intento 1: REST interno con nivel=beneficiario
    df = pd.DataFrame()
    try:
        r = requests.get(
            TGN_REST_URL,
            headers=headers_api,
            params=params,
            timeout=TIMEOUT,
            verify=False,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            df = pd.DataFrame(data)
            log.info(f"  REST beneficiario OK: {len(df):,} registros")
        elif isinstance(data, dict) and "data" in data:
            df = pd.DataFrame(data["data"])
            log.info(f"  REST beneficiario OK: {len(df):,} registros")
    except Exception as e:
        log.warning(f"  REST nivel=beneficiario falló: {e}")

    # Intento 2: API oficial v1 con columns beneficiario
    if df.empty:
        log.info("  Fallback: API v1 con columnas de beneficiario")
        # Según la documentación oficial filters es un objeto, no array
        # Ejemplo oficial: {"title": "...", "columns": [...], "filters": {...}}
        payload = {
            "columns": [
                "ejercicio_presupuestario",
                "jurisdiccion_desc",
                "entidad_desc",
                "credito_pagado",
                "credito_devengado",
            ],
        }
        # Intentar con filtro de año como objeto simple
        payload_filtrado = {**payload, "filters": {"ejercicio_presupuestario": anio}}

        for intento_payload in [payload_filtrado, payload]:
            try:
                r = requests.post(
                    TGN_API_URL,
                    headers=headers_api,
                    json=intento_payload,
                    params={"format": "csv"},
                    timeout=TIMEOUT,
                    verify=False,
                )
                r.raise_for_status()
                df = pd.read_csv(io.StringIO(r.text), on_bad_lines="skip")
                if not df.empty:
                    log.info(f"  API v1 OK: {len(df):,} registros")
                    break
            except Exception as e:
                body = getattr(e.response, 'text', '')[:500] if hasattr(e, 'response') else ''
                log.warning(f"  intento falló: {e} | {body[:200]}")
                df = pd.DataFrame()
                continue

        if df.empty:
            log.error("  API v1 todos los intentos fallaron")
            return pd.DataFrame()

    if df.empty:
        log.warning(f"  TGN: sin datos para {anio}")
        return pd.DataFrame()

    # Normalizar columnas — distintos endpoints devuelven distintos nombres
    col_map = {
        # REST interno
        "beneficiario":              "proveedor_tgn",
        "beneficiario_desc":         "proveedor_tgn",
        "cuit":                      "cuit_tgn",
        "beneficiario_id":           "cuit_tgn",
        "monto":                     "monto_pagado",
        "importe_pagado":            "monto_pagado",
        # API v1
        "ejercicio_presupuestario":  "anio",
        "jurisdiccion_desc":         "jurisdiccion",
        "entidad_desc":              "organismo_tgn",
        "credito_pagado":            "monto_pagado",
        "credito_devengado":         "monto_devengado",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    # Normalizar CUIT si existe
    if "cuit_tgn" in df.columns:
        df["cuit_tgn"] = df["cuit_tgn"].apply(
            lambda x: _normalizar_cuit(str(x))
        )

    for col in ["monto_pagado", "monto_devengado"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Clave de cruce por organismo (fallback cuando no hay CUIT)
    org_col = next(
        (c for c in ["organismo_tgn", "jurisdiccion"] if c in df.columns), None
    )
    df["organismo_norm"] = (
        df[org_col].apply(_normalizar_organismo) if org_col
        else pd.Series("", index=df.index)
    )

    df["fuente_tgn"] = f"Presupuesto Abierto TGN {anio}"
    log.info(f"  ✅ {len(df):,} registros TGN")

    # Log de cobertura CUIT
    if "cuit_tgn" in df.columns:
        con_cuit = df["cuit_tgn"].astype(bool).sum()
        log.info(f"  CUIT beneficiario: {con_cuit:,} / {len(df):,} registros")

    return df


# ─────────────────────────────────────────────────────────────────────────────
# CRUCE: BORA ←→ Comprar ←→ TGN
# ─────────────────────────────────────────────────────────────────────────────

def cruzar_fuentes(
    df_adj:     pd.DataFrame,
    df_comprar: pd.DataFrame,
    df_tgn:     pd.DataFrame,
) -> pd.DataFrame:
    """
    Cruza las tres fuentes:
      1. BORA adjudicaciones  (fecha, organismo, CUIT, proveedor, monto)
      2. Comprar datos.gob.ar (CUIT, organismo, monto, tipo_procedimiento)
      3. TGN presupuesto      (organismo, monto pagado)

    Cruce BORA ↔ Comprar: por CUIT (exacto) o por organismo (fuzzy)
    Cruce resultado ↔ TGN: por organismo normalizado
    """
    if df_adj.empty:
        log.warning("Sin adjudicaciones BORA para cruzar")
        return pd.DataFrame()

    # Índice Comprar por CUIT
    comprar_por_cuit: dict[str, list] = {}
    comprar_por_org:  list[dict]      = []
    if not df_comprar.empty:
        for _, row in df_comprar.iterrows():
            cuit = str(row.get("cuit_proveedor", "")).strip()
            if cuit:
                comprar_por_cuit.setdefault(cuit, []).append(row.to_dict())
            comprar_por_org.append(row.to_dict())

    # Índice TGN — por CUIT (exacto) y por organismo (fuzzy)
    tgn_por_cuit: dict[str, dict] = {}
    tgn_por_org:  dict[str, dict] = {}
    if not df_tgn.empty:
        for _, row in df_tgn.iterrows():
            # Índice por CUIT
            cuit = str(row.get("cuit_tgn", "")).strip()
            if cuit and cuit != "nan":
                tgn_por_cuit[cuit] = row.to_dict()
            # Índice por organismo (fallback)
            key = str(row.get("organismo_norm", "")).strip()
            if key:
                if key in tgn_por_org:
                    tgn_por_org[key]["monto_pagado"] = (
                        tgn_por_org[key].get("monto_pagado", 0)
                        + row.get("monto_pagado", 0)
                    )
                else:
                    tgn_por_org[key] = row.to_dict()

    resultados = []
    for _, adj in df_adj.iterrows():
        cuit_adj = str(adj.get("cuit_proveedor", "")).strip()
        org_adj  = str(adj.get("organismo_contratante", "")).strip()
        org_norm = _normalizar_organismo(org_adj)

        # ── Cruce con Comprar ───────────────────────────────────────────────
        match_comprar = []

        # 1) Por CUIT exacto (más confiable)
        if cuit_adj and cuit_adj in comprar_por_cuit:
            match_comprar = comprar_por_cuit[cuit_adj]

        # 2) Por organismo (2+ palabras en común)
        if not match_comprar and org_norm:
            palabras_org = set(org_norm.split())
            for c in comprar_por_org:
                org_c = _normalizar_organismo(str(c.get("organismo_contratante", "")))
                coincidencias = len(palabras_org & set(org_c.split()))
                if coincidencias >= 2:
                    match_comprar.append(c)

        cm = match_comprar[0] if match_comprar else {}

        # ── Cruce con TGN ───────────────────────────────────────────────────
        # 1) Por CUIT exacto (más confiable — solo si el endpoint devolvió CUITs)
        tgn_match = tgn_por_cuit.get(cuit_adj, {})
        # 2) Fallback por organismo normalizado
        if not tgn_match:
            tgn_match = tgn_por_org.get(org_norm, {})

        # ── Etapa de trazabilidad ───────────────────────────────────────────
        en_comprar = bool(match_comprar)
        en_tgn     = bool(tgn_match)
        tiene_cuit = bool(cuit_adj)

        if tiene_cuit and en_comprar and en_tgn:
            alerta = "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN"
        elif tiene_cuit and en_tgn:
            alerta = "🔶 BORA + TGN (cobró)"
        elif tiene_cuit and en_comprar:
            alerta = "🔷 BORA + COMPRAR"
        elif tiene_cuit:
            alerta = "✅ ADJUDICADO (sin cruce aún)"
        else:
            alerta = "⚠️ SIN CUIT EXTRAÍDO"

        resultados.append({
            # ── Identificación ──────────────────────────────────────────────
            "fecha_extraccion":       adj.get("fecha_extraccion"),
            "fecha_publicacion":      adj.get("fecha_publicacion"),
            # ── BORA ────────────────────────────────────────────────────────
            "organismo_contratante":  org_adj,
            "tipo_proceso_bora":      adj.get("tipo_proceso", ""),
            "link_bora":              adj.get("link_bora", ""),
            "proveedor_adjudicado":   adj.get("proveedor_adjudicado", ""),
            "cuit_proveedor":         cuit_adj,
            "monto_adjudicado_bora":  adj.get("monto_adjudicado_bora", ""),
            # ── Comprar ─────────────────────────────────────────────────────
            "en_comprar":             "✅ SÍ" if en_comprar else "❌ NO",
            "nro_proceso_comprar":    cm.get("nro_proceso", ""),
            "tipo_procedimiento":     cm.get("tipo_procedimiento", ""),
            "modalidad":              cm.get("modalidad", ""),
            "monto_comprar":          cm.get("monto_adjudicado_comprar", ""),
            "rubros":                 cm.get("rubros", ""),
            "unidad_operativa":       cm.get("unidad_operativa", ""),
            # ── TGN ─────────────────────────────────────────────────────────
            "cobro_en_tgn":           "✅ SÍ" if en_tgn else "❌ NO",
            "organismo_tgn":          tgn_match.get("organismo_tgn", ""),
            "unidad_ejecutora_tgn":   tgn_match.get("unidad_ejecutora", ""),
            "monto_pagado_tgn":       tgn_match.get("monto_pagado", ""),
            "monto_devengado_tgn":    tgn_match.get("monto_devengado", ""),
            # ── Trazabilidad ────────────────────────────────────────────────
            "alerta":                 alerta,
        })

    df = pd.DataFrame(resultados)

    # Ordenar por prioridad de alerta
    orden = {
        "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN": 0,
        "🔶 BORA + TGN (cobró)":               1,
        "🔷 BORA + COMPRAR":                   2,
        "✅ ADJUDICADO (sin cruce aún)":        3,
        "⚠️ SIN CUIT EXTRAÍDO":                4,
    }
    df["_orden"] = df["alerta"].map(orden).fillna(9)
    df = df.sort_values("_orden").drop(columns=["_orden"]).reset_index(drop=True)

    log.info(f"  Cruce completado: {len(df)} registros")
    for alerta, count in df["alerta"].value_counts().items():
        log.info(f"    {alerta}: {count}")

    return df


# ─────────────────────────────────────────────────────────────────────────────
# INDICADORES DE RIESGO
# ─────────────────────────────────────────────────────────────────────────────

def _agregar_riesgo(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega indicadores de riesgo licitatorio por fila.
    Independiente de la Matriz Monteverde (que clasifica por escenario).
    """
    indicadores_col = []
    score_col       = []

    for _, fila in df.iterrows():
        flags = []
        score = 0

        monto = _normalizar_monto(fila.get("monto_adjudicado_bora", ""))
        tipo  = str(fila.get("tipo_procedimiento", "")).lower()
        detalle = str(fila.get("tipo_proceso_bora", "")).lower()

        # Contratación directa sin justificación de monto
        if "directa" in tipo or "directa" in detalle:
            flags.append("⚠️ Contratación directa")
            score += 3

        # Monto bajo umbral licitación pública
        if 0 < monto < UMBRAL_LICITACION:
            flags.append("⚠️ Monto bajo umbral licitación")
            score += 2

        # Redeterminación de precios
        if "redeterminacion" in detalle or "redeterminación" in detalle:
            flags.append("⚠️ Redeterminación de precios")
            score += 2

        # Sin CUIT extraído
        if not str(fila.get("cuit_proveedor", "")).strip():
            flags.append("⚠️ Sin CUIT")
            score += 1

        # Adjudicado pero sin cobro TGN
        if fila.get("cobro_en_tgn") == "❌ NO" and fila.get("cuit_proveedor"):
            flags.append("⚠️ Sin cobro registrado en TGN")
            score += 1

        indicadores_col.append(" | ".join(flags) if flags else "✅ Sin alertas")
        score_col.append(score)

    df = df.copy()
    df["indicadores_riesgo"]  = indicadores_col
    df["score_riesgo_licit"]  = score_col
    df["nivel_riesgo_licit"]  = df["score_riesgo_licit"].apply(
        lambda s: "Alto" if s >= 5 else ("Medio" if s >= 2 else "Bajo")
    )
    return df


# ─────────────────────────────────────────────────────────────────────────────
# GUARDAR EXCEL
# ─────────────────────────────────────────────────────────────────────────────

def guardar_excels(
    df_cruce:   pd.DataFrame,
    df_adj:     pd.DataFrame,
    df_licit:   pd.DataFrame,
    df_comprar: pd.DataFrame,
    df_tgn:     pd.DataFrame,
    hoy:        date | None = None,
) -> tuple[str, str]:
    """
    Genera dos Excel en data/YYYY-MM/:

    reporte_YYYY-MM-DD.xlsx       — operativo completo (5 hojas)
    flujo_licitaciones_YYYY-MM-DD.xlsx — trazabilidad BORA→TGN (4 hojas)
    """
    if hoy is None:
        hoy = date.today()

    carpeta = _directorio_mes(hoy)
    fecha_str = hoy.strftime("%Y-%m-%d")

    # ── Excel 1: Reporte operativo ────────────────────────────────────────────
    archivo1 = os.path.join(carpeta, f"reporte_{fecha_str}.xlsx")
    with pd.ExcelWriter(archivo1, engine="openpyxl") as writer:
        hojas = 0

        if not df_cruce.empty:
            df_cruce.to_excel(writer, sheet_name="🚨 Flujo Completo", index=False)
            hojas += 1

        if not df_adj.empty:
            df_adj.to_excel(writer, sheet_name="🏆 Adjudicaciones BORA", index=False)
            hojas += 1

        if not df_licit.empty:
            df_licit.to_excel(writer, sheet_name="📰 Licitaciones BORA", index=False)
            hojas += 1

        if not df_comprar.empty:
            df_comprar.to_excel(writer, sheet_name="🛒 Comprar ONC", index=False)
            hojas += 1

        if not df_tgn.empty:
            df_tgn.to_excel(writer, sheet_name="💰 TGN Pagos", index=False)
            hojas += 1

        # Hoja de alertas consolidada
        if not df_cruce.empty:
            cols_alerta = [c for c in [
                "fecha_publicacion", "organismo_contratante",
                "cuit_proveedor", "proveedor_adjudicado",
                "monto_adjudicado_bora", "tipo_procedimiento",
                "indicadores_riesgo", "score_riesgo_licit", "nivel_riesgo_licit",
                "tipo_decision", "transferencia", "indice_fenomeno",
                "cobro_en_tgn", "monto_pagado_tgn", "alerta", "link_bora",
            ] if c in df_cruce.columns]
            df_cruce[cols_alerta].sort_values(
                "score_riesgo_licit", ascending=False
            ).to_excel(writer, sheet_name="⚠️ Red Flags", index=False)
            hojas += 1

        # Guardia: al menos una hoja visible
        if hojas == 0:
            pd.DataFrame({
                "estado": ["Sin datos — scrapers no retornaron resultados"],
                "fecha":  [datetime.now().isoformat()],
            }).to_excel(writer, sheet_name="Sin Datos", index=False)

    log.info(f"  ✅ {archivo1}")

    # ── Excel 2: Flujo licitaciones ───────────────────────────────────────────
    archivo2 = os.path.join(carpeta, f"flujo_licitaciones_{fecha_str}.xlsx")
    with pd.ExcelWriter(archivo2, engine="openpyxl") as writer:
        hojas = 0

        if not df_cruce.empty:
            df_cruce.to_excel(writer, sheet_name="🔗 Flujo Cruzado", index=False)
            hojas += 1

        if not df_cruce.empty:
            df_cobros = df_cruce[df_cruce.get("cobro_en_tgn", pd.Series()) == "✅ SÍ"]
            if not df_cobros.empty:
                df_cobros.to_excel(writer, sheet_name="💰 Cobraron en TGN", index=False)
                hojas += 1

        if not df_comprar.empty:
            df_comprar.to_excel(writer, sheet_name="⏳ Procesos ONC", index=False)
            hojas += 1

        if not df_cruce.empty and "nivel_riesgo_licit" in df_cruce.columns:
            df_alto = df_cruce[df_cruce["nivel_riesgo_licit"].isin(["Alto", "Medio"])]
            if not df_alto.empty:
                df_alto.sort_values(
                    "score_riesgo_licit", ascending=False
                ).to_excel(writer, sheet_name="⚠️ Alertas Riesgo", index=False)
                hojas += 1

        if hojas == 0:
            pd.DataFrame({
                "estado": ["Sin datos"],
                "fecha":  [datetime.now().isoformat()],
            }).to_excel(writer, sheet_name="Sin Datos", index=False)

    log.info(f"  ✅ {archivo2}")
    return archivo1, archivo2


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    hoy = date.today()

    # Guardia fin de semana
    if hoy.weekday() >= 5:
        dia = "sábado" if hoy.weekday() == 5 else "domingo"
        log.info(f"⏭️ Hoy es {dia} — BORA no publica. Script finalizado.")
        return

    log.info("=" * 60)
    log.info(f"🚀 INICIO PROCESO DIARIO: {hoy.isoformat()}")
    log.info("=" * 60)

    # 1. Extracción
    df_adj, df_licit = extraer_bora(hoy)
    df_comprar       = extraer_comprar(hoy.year)
    df_tgn           = extraer_tgn(hoy.year)

    # 2. Clasificación Monteverde (Matriz XAI)
    if not df_adj.empty:
        df_adj = aplicar_matriz(df_adj, col_texto="texto_muestra")

    # 3. Cruce BORA → Comprar → TGN
    df_cruce = cruzar_fuentes(df_adj, df_comprar, df_tgn)

    # 4. Indicadores de riesgo licitatorio
    if not df_cruce.empty:
        df_cruce = _agregar_riesgo(df_cruce)

    # 5. Guardar Excel
    a1, a2 = guardar_excels(df_cruce, df_adj, df_licit, df_comprar, df_tgn, hoy)

    # 6. Resumen
    log.info("\n" + "=" * 60)
    log.info("📊 RESUMEN FINAL")
    log.info("=" * 60)
    log.info(f"  BORA adjudicaciones : {len(df_adj)}")
    log.info(f"  BORA licitaciones   : {len(df_licit)}")
    log.info(f"  Comprar ONC         : {len(df_comprar)}")
    log.info(f"  TGN beneficiarios   : {len(df_tgn)}")
    log.info(f"  Flujo cruzado       : {len(df_cruce)}")
    if not df_cruce.empty and "nivel_riesgo_licit" in df_cruce.columns:
        alto  = (df_cruce["nivel_riesgo_licit"] == "Alto").sum()
        medio = (df_cruce["nivel_riesgo_licit"] == "Medio").sum()
        log.info(f"  🔴 Riesgo Alto      : {alto}")
        log.info(f"  🟡 Riesgo Medio     : {medio}")
    log.info(f"  📁 Reporte          : {a1}")
    log.info(f"  📁 Flujo            : {a2}")

    return df_cruce


if __name__ == "__main__":
    main()