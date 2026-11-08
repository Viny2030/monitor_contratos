"""
cruce_comprar.py — cruce de los avisos del BORA con las adjudicaciones de COMPR.AR
================================================================================
Antes el cruce comparaba cada aviso con lo que el portal COMPR.AR mostraba ese
día (unos 12 procesos abiertos, no adjudicaciones) y, sin CUIT, aceptaba
cualquier proceso del mismo organismo con dos palabras en común. Resultado:
22 de 838 avisos "en COMPR.AR" y sin garantía de que fuera el mismo proceso.

Ahora se cruza contra el archivo de adjudicaciones 2016-2026 de datos.gob.ar,
por el mismo proceso:

    "Licitación Pública 208/2026" + EJÉRCITO ARGENTINO
        → 84/16-0208-LPU26 (SAF 374 - Estado Mayor General del Ejército)

es decir, tipo + número + año del aviso, y el SAF del organismo. En la prueba
del 06/11/2026 el proveedor coincidió en 63 de 63 casos verificables.

Cada aviso queda en uno de cuatro estados (columna en_comprar):

    ✅ SÍ                              mismo proceso encontrado en COMPR.AR
    ➖ FUERA DE COMPR.AR                el organismo no compra por COMPR.AR
    ⏳ COMPR.AR publicado hasta dd/mm   aviso posterior al último dato publicado
    ❌ NO                               organismo en COMPR.AR, proceso no encontrado

El archivo se baja con ETag (solo cuando cambia) a ~/.cache/monitor_contratos/comprar/.
"""

from __future__ import annotations

import io
import json
import os
import re
import unicodedata
from datetime import date, datetime

import pandas as pd
import requests

COMPRAR_URL = os.getenv(
    "COMPRAR_ADJ_URL",
    "https://infra.datos.gob.ar/catalog/jgm/dataset/4/distribution/4.22/download/Adjudicaciones.csv",
)
RAIZ = os.path.dirname(os.path.abspath(__file__))
# Fuera del checkout: actions/checkout borra los archivos ignorados en cada corrida,
# y el CSV pesa ~88 MB. En la carpeta del usuario el ETag evita bajarlo de nuevo.
CACHE_DIR = os.getenv("COMPRAR_CACHE_DIR", os.path.join(os.path.expanduser("~"), ".cache", "monitor_contratos", "comprar"))
EJERCICIOS = ("2024", "2025", "2026", "2027")
HEADERS = {"User-Agent": "MonitorContratos/1.0 (github.com/Viny2030/monitor_contratos)"}

SI, NO, FUERA = "✅ SÍ", "❌ NO", "➖ FUERA DE COMPR.AR"
PENDIENTE = "⏳ COMPR.AR publicado hasta {}"

# ── Organismos: mismas reglas que el Monitor IRI (monitor/connector.py) ──────
_STOP = {"NACIONAL", "ARGENTINA", "ARGENTINO", "DE", "DEL", "LA", "EL", "LOS", "LAS", "Y", "GENERAL"}
_GENERICAS = {"ESTADO", "MAYOR", "MINISTERIO", "SECRETARIA", "COMANDO", "SERVICIO", "REPUBLICA"}
# Lo que el BORA agrega después del nombre ("SERVICIO PENITENCIARIO FEDERAL Solicitudes de Provisión")
_SUFIJOS_BORA = re.compile(
    r"\s+(Solicitud(es)?\s+de\s+Provisi|Listado\s+[ÓO]rdenes|Contrataciones\s+(Directas|Varias)|"
    r"Licitaciones\s+P[úu]blicas).*$", re.IGNORECASE)
# Organismos que en COMPR.AR figuran con el nombre de su servicio administrativo (SAF)
_ALIAS_SAF = {"POLICIA FEDERAL": "326", "SERVICIO PENITENCIARIO FEDERAL": "331"}
_TIPOS = [("licitacion publica", "LPU"), ("licitacion privada", "LPR"), ("contratacion directa", "CDI"),
          ("concurso publico", "CPU"), ("concurso privado", "CPR"), ("subasta", "SPU"), ("compulsa", "CDI")]


def _sin_acentos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(t)) if unicodedata.category(c) != "Mn")


def norm_org(t: str) -> str:
    t = re.sub(r"^\s*\d+\s*-\s*", "", str(t))
    t = re.sub(r"[^A-Z ]", " ", _sin_acentos(t).upper())
    return " ".join(w for w in t.split() if len(w) > 2 and w not in _STOP)


def nombre_base(organismo: str) -> str:
    return re.sub(_SUFIJOS_BORA, "", str(organismo).split(" - ")[0]).strip()


def _match_nombre(nombre: str, candidatos: dict):
    n = set(norm_org(nombre).split())
    if not n:
        return None
    mejor, puntaje = None, 0
    for org, val in candidatos.items():
        o = set(org.split())
        comunes = len(n & o)
        # todas las palabras del más corto están en el otro y lo que sobra es genérico
        if comunes and comunes == min(len(n), len(o)) and ((n | o) - (n & o)) <= _GENERICAS and comunes > puntaje:
            mejor, puntaje = val, comunes
    return mejor


def tipo_numero(tipo_proceso_bora: str):
    """'Licitación Pública 208/2026' → ('LPU', 208, '26'); None si falta algo."""
    t = _sin_acentos(str(tipo_proceso_bora)).lower()
    tipo = next((c for k, c in _TIPOS if k in t), None)
    m = re.search(r"(\d+)\s*/\s*(\d{4})", t)
    if not tipo or not m:
        return None
    return tipo, int(m.group(1)), m.group(2)[-2:]


# ── Descarga con caché ───────────────────────────────────────────────────────
def descargar(url: str = COMPRAR_URL, cache_dir: str = CACHE_DIR, timeout: int = 300) -> str | None:
    """Devuelve la ruta local del CSV; lo baja solo si cambió (ETag)."""
    os.makedirs(cache_dir, exist_ok=True)
    ruta, meta_p = os.path.join(cache_dir, "Adjudicaciones.csv"), os.path.join(cache_dir, "meta.json")
    meta = {}
    if os.path.exists(meta_p) and os.path.exists(ruta):
        try:
            meta = json.load(open(meta_p, encoding="utf-8"))
        except ValueError:
            meta = {}
    h = dict(HEADERS)
    if meta.get("etag"):
        h["If-None-Match"] = meta["etag"]
    try:
        r = requests.get(url, headers=h, timeout=timeout)
    except Exception as e:
        print(f"⚠️ COMPR.AR: no se pudo descargar ({e}); se usa la copia local" if os.path.exists(ruta) else f"⚠️ COMPR.AR: {e}")
        return ruta if os.path.exists(ruta) else None
    if r.status_code == 304:
        return ruta
    if r.status_code != 200 or len(r.content) < 1000:
        print(f"⚠️ COMPR.AR: HTTP {r.status_code}")
        return ruta if os.path.exists(ruta) else None
    with open(ruta, "wb") as f:
        f.write(r.content)
    json.dump({"etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified"),
               "bajado": datetime.now().isoformat(timespec="seconds")}, open(meta_p, "w", encoding="utf-8"))
    return ruta


# ── Índice ───────────────────────────────────────────────────────────────────
class IndiceComprar:
    def __init__(self, df: pd.DataFrame):
        df = df[df["Ejercicio"].astype(str).isin(EJERCICIOS)].copy()
        m = df["Numero_Proceso"].astype(str).str.extract(r"-(\d+)-([A-Z]+)(\d{2})$")
        df["_num"] = pd.to_numeric(m[0], errors="coerce")
        df["_tipo"], df["_yy"] = m[1], m[2]
        df["_saf"] = df["Descripcion_SAF"].astype(str).str.extract(r"^(\d+)")[0]
        df["_monto"] = pd.to_numeric(df["Monto"].astype(str).str.replace(",", ".", regex=False), errors="coerce")
        fechas = pd.to_datetime(df["Fecha_de_Adjudicacion"].astype(str).str[:10], format="%d/%m/%Y", errors="coerce")
        self.corte: date | None = fechas.max().date() if fechas.notna().any() else None
        self.saf_por_nombre = {norm_org(n): s for s, n in
                               df.dropna(subset=["_saf"]).groupby("_saf")["Descripcion_SAF"].first().items()}
        self._grupos = {k: g for k, g in df.dropna(subset=["_num", "_tipo", "_saf"]).groupby(["_tipo", "_num", "_yy", "_saf"])}

    @classmethod
    def desde_csv(cls, ruta: str) -> "IndiceComprar":
        cols = ["Numero_Proceso", "Descripcion_SAF", "Descripcion_UOC", "Tipo_de_Procedimiento", "Modalidad",
                "Apartado_Directa", "Ejercicio", "Fecha_de_Adjudicacion", "Rubros", "CUIT",
                "Descripcion_Proveedor", "Monto", "Moneda"]
        df = pd.read_csv(ruta, dtype=str, encoding="utf-8-sig", usecols=lambda c: c in cols, low_memory=False)
        return cls(df)

    def saf(self, organismo: str):
        base = nombre_base(organismo)
        return _ALIAS_SAF.get(norm_org(base)) or _match_nombre(base, self.saf_por_nombre)

    def cruzar(self, tipo_proceso_bora: str, organismo: str, fecha_publicacion=None) -> dict:
        """Estado del aviso y, si hay match, los datos del proceso en COMPR.AR."""
        saf = self.saf(organismo)
        if not saf:
            return {"en_comprar": FUERA}
        tn = tipo_numero(tipo_proceso_bora)
        g = self._grupos.get((tn[0], float(tn[1]), tn[2], saf)) if tn else None
        if g is None:
            f = pd.to_datetime(fecha_publicacion, errors="coerce")
            if self.corte and pd.notna(f) and f.date() > self.corte:
                return {"en_comprar": PENDIENTE.format(self.corte.strftime("%d/%m/%Y"))}
            return {"en_comprar": NO}
        g = g if g["Numero_Proceso"].nunique() == 1 else g[g["Numero_Proceso"] == g["Numero_Proceso"].iloc[0]]
        en_pesos = g["Moneda"].astype(str).str.contains("Peso", case=False, na=False)
        monto = g.loc[en_pesos, "_monto"].sum() if en_pesos.any() else None
        provs = g.dropna(subset=["CUIT"]).drop_duplicates("CUIT")
        unico = len(provs) == 1
        return {
            "en_comprar": SI,
            "nro_proceso_comprar": g["Numero_Proceso"].iloc[0],
            "tipo_procedimiento": g["Tipo_de_Procedimiento"].iloc[0],
            "modalidad": g["Modalidad"].iloc[0],
            "monto_comprar": round(float(monto), 2) if monto else "",
            "rubros": str(g["Rubros"].iloc[0]).strip("; "),
            "unidad_operativa": g["Descripcion_UOC"].iloc[0],
            "proveedor_comprar": provs["Descripcion_Proveedor"].iloc[0] if unico else "; ".join(provs["Descripcion_Proveedor"].astype(str).head(5)),
            "cuit_comprar": provs["CUIT"].iloc[0] if unico else "",
        }


def alerta(fila: dict) -> str:
    """Misma lógica que diario.cruzar_fuentes, con el en_comprar nuevo."""
    cancelada = str(fila.get("cancelada", "")).strip().lower() in ("true", "1")
    tiene_cuit = bool(str(fila.get("cuit_proveedor", "")).strip())
    en_comprar = fila.get("en_comprar") == SI
    # Reportes nuevos: cobro_en_tgn sólo dice SÍ por CUIT; los pagos del organismo van aparte.
    en_tgn = (str(fila.get("cobro_en_tgn", "")).startswith("✅")
              or str(fila.get("organismo_con_pagos_tgn", "")).startswith("✅"))
    tgn_cuit = str(fila.get("metodo_cruce_tgn", "")) == "CUIT"
    if cancelada:
        return "🚫 CANCELADA / DEJADA SIN EFECTO"
    if tiene_cuit and en_comprar and en_tgn:
        return "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN"
    if tiene_cuit and en_tgn and tgn_cuit:
        return "🔶 BORA + TGN (cobró)"
    if tiene_cuit and en_tgn:
        return "🔶 BORA + TGN (por organismo)"
    if tiene_cuit and en_comprar:
        return "🔷 BORA + COMPRAR"
    if tiene_cuit:
        return "✅ ADJUDICADO (sin cruce aún)"
    return "⚠️ SIN CUIT EXTRAÍDO"
