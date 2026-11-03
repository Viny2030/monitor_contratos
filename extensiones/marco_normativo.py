"""
Marco normativo de las contrataciones públicas nacionales (revisión del 03/11/2026).

Cruza cada aviso del BORA con el régimen legal que le corresponde, para que el
análisis no se quede en el riesgo estadístico:

  * régimen material: bienes y servicios (Dec. Delegado 1023/2001), obra pública
    (Ley 13.064) o concesión (Leyes 17.520 / 23.696), según la categoría y el
    texto del aviso;
  * ámbito subjetivo: el Dec. 1023/2001 rige para la Administración Nacional
    (art. 8, inc. a, Ley 24.156). Empresas y sociedades del Estado, el Poder
    Judicial y el Poder Legislativo contratan con regímenes propios, así que sus
    avisos no se evalúan contra el Dec. 1030/2016;
  * procedimiento: licitación o concurso público o privado, contratación
    directa, subasta, compulsa abreviada;
  * monto en módulos (art. 28, Dec. 1030/2016). Los topes por procedimiento
    (art. 27) solo se controlan si se configuran en NORMATIVA_TOPES_MODULOS,
    porque no se pudo confirmar el texto consolidado vigente tras el Dec. 666/2024.

Cada norma del catálogo lleva su estado de verificación. Las correcciones al
listado de partida están en `nota` (p. ej. el Dec. 1336/2016 creó CONTRAT.AR,
no COMPR.AR; la Ley 27.437 fue derogada en casi todo su articulado por el
DNU 70/2023). Es un indicador de cumplimiento formal, no un dictamen jurídico.

Copia del módulo del Monitor de Licitaciones (repo gob_bo_comprar_tgn), con los
mismos criterios y el mismo catálogo; cambian solo los endpoints, que acá leen
todos los contratos del tablero.

Endpoints:

  GET /api/marco-normativo                       Catálogo de normas y reglas aplicadas
  GET /api/contratos/normativa?organismo=&desde=&hasta=
                                                 Resumen y contratos con observaciones
"""
import json
import os
import re
import unicodedata

from fastapi import APIRouter, HTTPException

VERSION_NORMATIVA = "1.0.0"
REVISADO = "2026-11-03"

# Valor del módulo (M): $40.000 según el Dec. 666/2024, que modificó el art. 28 del
# Dec. 1030/2016. No se encontró una actualización posterior; se puede cambiar sin
# tocar el código con NORMATIVA_VALOR_MODULO.
VALOR_MODULO = float(os.getenv("NORMATIVA_VALOR_MODULO", "40000"))
FUENTE_MODULO = os.getenv("NORMATIVA_FUENTE_MODULO", "Dec. 666/2024 (art. 28, Dec. 1030/2016)")


def _topes() -> dict:
    """{'licitacion_privada': M, 'contratacion_directa': M, ...} o {} si no se configuró."""
    try:
        valor = json.loads(os.getenv("NORMATIVA_TOPES_MODULOS", "") or "{}")
        return {k: float(v) for k, v in valor.items()}
    except (ValueError, TypeError, AttributeError):
        return {}


# estado: "verificada" (texto o fuente oficial consultada el 03/11/2026),
#         "parcialmente derogada", "sin verificar" (no se encontró la norma citada).
NORMAS = [
    # ── Leyes y decretos delegados ───────────────────────────────────────────
    {"id": "dec-1023-2001", "norma": "Decreto Delegado 1023/2001", "tipo": "Decreto delegado",
     "nombre": "Régimen de Contrataciones de la Administración Nacional",
     "materia": "Bienes, servicios, consultorías y locaciones. Principios (transparencia, concurrencia, "
                "igualdad, publicidad, razonabilidad) y procedimientos de selección.",
     "regimen": ["bienes_servicios"], "estado": "verificada",
     "nota": "Rige para la Administración Nacional (art. 8, inc. a, Ley 24.156). Modificado por el Dec. 105/2025."},
    {"id": "ley-27742", "norma": "Ley 27.742", "tipo": "Ley",
     "nombre": "Ley de Bases y Puntos de Partida para la Libertad de los Argentinos",
     "materia": "Reformas sobre contratos administrativos, renegociación y rescisión, obra pública y concesiones.",
     "regimen": ["transversal"], "estado": "verificada", "nota": ""},
    {"id": "ley-13064", "norma": "Ley 13.064", "tipo": "Ley", "nombre": "Régimen de Obras Públicas Nacionales",
     "materia": "Construcción, conservación y reparación de inmuebles del Estado Nacional.",
     "regimen": ["obra_publica"], "estado": "verificada",
     "nota": "El Dec. 105/2025 (BO 18/02/2025) derogó su art. 13 (registro de constructores); "
             "el Dec. 206/2025 fija los montos de la escala del art. 4."},
    {"id": "ley-17520", "norma": "Ley 17.520", "tipo": "Ley", "nombre": "Concesión de obra pública",
     "materia": "Concesiones de obras públicas por peaje o tarifa.", "regimen": ["concesion"],
     "estado": "verificada", "nota": ""},
    {"id": "ley-23696", "norma": "Ley 23.696", "tipo": "Ley", "nombre": "Reforma del Estado",
     "materia": "Privatizaciones y concesiones de servicios públicos.", "regimen": ["concesion"],
     "estado": "verificada", "nota": ""},
    {"id": "ley-24156", "norma": "Ley 24.156", "tipo": "Ley",
     "nombre": "Administración Financiera y Sistemas de Control del Sector Público Nacional",
     "materia": "Presupuesto, tesorería, contabilidad y control (SIGEN y AGN). Su art. 8 define el ámbito "
                "subjetivo del Dec. 1023/2001.",
     "regimen": ["transversal"], "estado": "verificada", "nota": ""},
    {"id": "ley-27401", "norma": "Ley 27.401", "tipo": "Ley",
     "nombre": "Responsabilidad Penal de las Personas Jurídicas",
     "materia": "Programa de integridad exigible a quien contrate con el Estado cuando el contrato, por su "
                "monto, deba ser aprobado por autoridad con rango no menor a ministro (art. 24).",
     "regimen": ["transversal"], "estado": "verificada", "nota": ""},
    {"id": "ley-27437", "norma": "Ley 27.437", "tipo": "Ley",
     "nombre": "Compre Argentino y Desarrollo de Proveedores",
     "materia": "Preferencias para bienes nacionales y MiPyMEs en las compras del Estado.",
     "regimen": ["transversal"], "estado": "parcialmente derogada",
     "nota": "El DNU 70/2023 derogó los arts. 1 a 21 y 24 a 30: las preferencias ya no se aplican. "
             "La Disp. ONC 62/2024 quitó del pliego la declaración jurada de oferta nacional."},
    {"id": "ley-27275", "norma": "Ley 27.275", "tipo": "Ley", "nombre": "Acceso a la Información Pública",
     "materia": "Transparencia activa: publicación de contrataciones y adjudicaciones.",
     "regimen": ["transversal"], "estado": "verificada", "nota": ""},
    # ── Decretos reglamentarios ──────────────────────────────────────────────
    {"id": "dec-1030-2016", "norma": "Decreto 1030/2016", "tipo": "Decreto",
     "nombre": "Reglamento del Régimen de Contrataciones de la Administración Nacional",
     "materia": "Etapas del procedimiento, comisiones evaluadoras, garantías, penalidades y montos por "
                "procedimiento expresados en módulos (arts. 27 y 28).",
     "regimen": ["bienes_servicios"], "estado": "verificada",
     "nota": "El Dec. 666/2024 fijó el módulo en $40.000 y modificó la cantidad de módulos por procedimiento."},
    {"id": "dec-666-2024", "norma": "Decreto 666/2024", "tipo": "Decreto",
     "nombre": "Modificación del Reglamento 1030/2016",
     "materia": "Valor del módulo ($40.000) y montos por procedimiento; ver Comunicación General ONC 19/2024.",
     "regimen": ["bienes_servicios"], "estado": "verificada", "nota": ""},
    {"id": "dec-105-2025", "norma": "Decreto 105/2025", "tipo": "Decreto",
     "nombre": "Modificación de la Ley 13.064 y del Dec. 1023/2001",
     "materia": "Obra pública: deroga el art. 13 de la Ley 13.064 (registro de constructores) y reforma el "
                "registro de proveedores de obras y consultoría.",
     "regimen": ["obra_publica"], "estado": "verificada",
     "nota": "Publicado el 18/02/2025. No se confirmó el alcance sobre la Ley 17.520 que le atribuye el listado de partida."},
    {"id": "dec-1336-2016", "norma": "Decreto 1336/2016", "tipo": "Decreto", "nombre": "Sistema CONTRAT.AR",
     "materia": "Gestión electrónica de obra pública, concesiones de obra pública, servicios públicos y licencias.",
     "regimen": ["obra_publica", "concesion"], "estado": "verificada",
     "nota": "Corrección: creó CONTRAT.AR, no COMPR.AR (COMPR.AR se habilitó con la Disp. ONC 65/2016)."},
    {"id": "dec-1169-2018", "norma": "Decreto 1169/2018", "tipo": "Decreto",
     "nombre": "ONC como órgano rector de obra pública",
     "materia": "Designa a la Oficina Nacional de Contrataciones órgano rector del sistema de contrataciones "
                "de obras públicas y concesiones de obras públicas; complementa al Dec. 1336/2016.",
     "regimen": ["obra_publica", "concesion"], "estado": "verificada", "nota": ""},
    {"id": "dec-202-2017", "norma": "Decreto 202/2017", "tipo": "Decreto",
     "nombre": "Conflictos de intereses en contrataciones",
     "materia": "Declaración jurada de intereses del oferente respecto de autoridades intervinientes.",
     "regimen": ["transversal"], "estado": "verificada", "nota": ""},
    # ── Disposiciones y resoluciones operativas ──────────────────────────────
    {"id": "disp-onc-62-2016", "norma": "Disposición ONC 62/2016", "tipo": "Disposición",
     "nombre": "Manual de Procedimiento del Régimen de Contrataciones",
     "materia": "Paso a paso desde el requerimiento hasta la recepción definitiva y el pago.",
     "regimen": ["bienes_servicios"], "estado": "verificada", "nota": ""},
    {"id": "disp-onc-63-2016", "norma": "Disposición ONC 63/2016", "tipo": "Disposición",
     "nombre": "Pliego Único de Bases y Condiciones Generales (original)",
     "materia": "Pliego general para los procedimientos del Dec. 1023/2001.",
     "regimen": ["bienes_servicios"], "estado": "verificada",
     "nota": "Su art. 1 fue sustituido por la Disp. ONC 62/2024."},
    {"id": "disp-onc-62-2024", "norma": "Disposición ONC 62/2024", "tipo": "Disposición",
     "nombre": "Nuevo Pliego Único de Bases y Condiciones Generales",
     "materia": "Aprueba un nuevo PUBCG, vigente para procedimientos autorizados o convocados desde el 02/09/2024.",
     "regimen": ["bienes_servicios"], "estado": "verificada",
     "nota": "Es un pliego nuevo (BO 31/07/2024), no una modificación menor del de 2016."},
    {"id": "disp-onc-65-2016", "norma": "Disposición ONC 65/2016", "tipo": "Disposición",
     "nombre": "Habilitación de COMPR.AR",
     "materia": "Sistema Electrónico de Contrataciones de la Administración Nacional y su manual de uso.",
     "regimen": ["bienes_servicios"], "estado": "verificada", "nota": ""},
    {"id": "rg-afip-4164-2017", "norma": "Resolución General AFIP 4164/2017", "tipo": "Resolución",
     "nombre": "Habilidad fiscal para contratar",
     "materia": "Verificación del cumplimiento fiscal de los oferentes ante ARCA (ex AFIP).",
     "regimen": ["transversal"], "estado": "verificada", "nota": ""},
    {"id": "res-oa-integridad", "norma": "Resoluciones OA 27/2018 y 9/2022", "tipo": "Resolución",
     "nombre": "Lineamientos de integridad y Registro de Integridad y Transparencia (RITE)",
     "materia": "Criterios de la Oficina Anticorrupción para evaluar programas de integridad (Ley 27.401).",
     "regimen": ["transversal"], "estado": "verificada",
     "nota": "La 'Resolución OA 366/2022' del listado de partida no se encontró; se citan las que sí constan."},
]

REGIMENES = {
    "bienes_servicios": "Bienes y servicios (Dec. Delegado 1023/2001)",
    "obra_publica": "Obra pública (Ley 13.064)",
    "concesion": "Concesión (Leyes 17.520 / 23.696)",
    "venta_estado": "Venta o subasta de bienes del Estado",
}

# Ámbito subjetivo: quién contrata. Se evalúa sobre el nombre del organismo sin acentos.
_REGIMEN_PROPIO = [
    (r"\bS\.?\s?A\.?\s?U\.?\b|SOCIEDAD ANONIMA|\bS\.?\s?E\.?$|\bS\.?\s?E\.?\b|SOCIEDAD DEL ESTADO|"
     r"\bS\.?A\.?M\.?I\.?C\b|EMPRESA ", "Empresa o sociedad del Estado (art. 8, inc. b, Ley 24.156)"),
    (r"PODER JUDICIAL|CONSEJO DE LA MAGISTRATURA|CORTE SUPREMA|MINISTERIO PUBLICO",
     "Poder Judicial o Ministerio Público (régimen propio)"),
    (r"CAMARA DE DIPUTADOS|SENADO DE LA NACION|HONORABLE|CONGRESO DE LA NACION|BIBLIOTECA DEL CONGRESO",
     "Poder Legislativo (régimen propio)"),
    (r"UNIVERSIDAD", "Universidad nacional (régimen propio; verificar uso de COMPR.AR)"),
]

_PROCEDIMIENTOS = [
    ("licitacion_publica", r"LICITACION PUBLICA|CONCURSO PUBLICO"),
    ("licitacion_privada", r"LICITACION PRIVADA|CONCURSO PRIVADO"),
    ("contratacion_directa", r"CONTRATACION DIRECTA|ADJUDICACION SIMPLE|COMPULSA ABREVIADA"),
    ("subasta", r"SUBASTA|REMATE"),
]
NOMBRE_PROCEDIMIENTO = {
    "licitacion_publica": "Licitación o concurso público",
    "licitacion_privada": "Licitación o concurso privado",
    "contratacion_directa": "Contratación directa",
    "subasta": "Subasta",
    "no_identificado": "No identificado",
}


def _normalizar(texto) -> str:
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", texto.upper()).strip()


def parsear_monto(valor) -> float:
    """'$ 25.771.802,52' → 25771802.52. None si no hay un número."""
    texto = re.sub(r"[^\d,.\-]", "", str(valor or ""))
    if not re.search(r"\d", texto):
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif texto.count(".") > 1 or re.search(r"\.\d{3}$", texto):
        texto = texto.replace(".", "")  # 1.234.567 sin decimales
    try:
        return float(texto)
    except ValueError:
        return None


def regimen_propio(organismo) -> str:
    org = _normalizar(organismo)
    for patron, descripcion in _REGIMEN_PROPIO:
        if re.search(patron, org):
            return descripcion
    return ""


def clasificar_regimen(fila: dict) -> str:
    texto = _normalizar(" ".join(str(fila.get(c, "")) for c in
                                 ("categoria", "tipo_proceso", "tipo_proceso_bora", "texto_muestra", "nombre_proceso")))
    if re.search(r"VENTAS Y OFRECIMIENTOS DEL ESTADO|SUBASTA|REMATE", texto):
        return "venta_estado"
    if re.search(r"CONCESION|PEAJE|17\.?520", texto):
        return "concesion"
    if re.search(r"^OBRAS |\bOBRA PUBLICA\b|\bOBRAS? -|13\.?064|CONTRAT\.?AR|PAVIMENT|CONSTRUCCION DE", texto):
        return "obra_publica"
    return "bienes_servicios"


def clasificar_procedimiento(fila: dict) -> str:
    texto = _normalizar(fila.get("tipo_proceso") or fila.get("tipo_proceso_bora") or "")
    for clave, patron in _PROCEDIMIENTOS:
        if re.search(patron, texto):
            return clave
    return "no_identificado"


def _normas(*ids) -> list:
    por_id = {n["id"]: n["norma"] for n in NORMAS}
    return [por_id[i] for i in ids]


def evaluar_aviso(fila: dict) -> dict:
    """Régimen, procedimiento, monto en módulos, controles y alertas formales de un aviso."""
    organismo = fila.get("organismo") or fila.get("organismo_contratante") or ""
    regimen = clasificar_regimen(fila)
    procedimiento = clasificar_procedimiento(fila)
    propio = regimen_propio(organismo)
    monto = parsear_monto(fila.get("monto_adjudicado") or fila.get("monto_adjudicado_bora"))
    modulos = round(monto / VALOR_MODULO, 1) if monto and VALOR_MODULO else None

    if regimen == "obra_publica":
        normas = _normas("ley-13064", "dec-105-2025", "dec-1336-2016", "dec-1169-2018")
        plataforma = "CONTRAT.AR"
    elif regimen == "concesion":
        normas = _normas("ley-17520", "ley-23696", "dec-1336-2016")
        plataforma = "CONTRAT.AR"
    elif regimen == "venta_estado":
        normas = _normas("dec-1023-2001", "dec-1030-2016")
        plataforma = "COMPR.AR / subasta"
    else:
        normas = _normas("dec-1023-2001", "dec-1030-2016", "disp-onc-62-2024", "disp-onc-65-2016")
        plataforma = "COMPR.AR"

    controles = [
        {"control": "Publicidad y transparencia activa", "norma": "Ley 27.275"},
        {"control": "Declaración jurada de intereses del oferente", "norma": "Decreto 202/2017"},
    ]
    if regimen != "venta_estado":
        controles.append({"control": "Habilidad fiscal del oferente (ARCA)",
                          "norma": "Resolución General AFIP 4164/2017"})
        controles.append({"control": "Programa de integridad si el contrato lo aprueba una autoridad de "
                                     "rango no menor a ministro", "norma": "Ley 27.401, art. 24"})

    alertas = []
    if propio:
        alertas.append(f"{propio}: no se evalúa contra el Dec. 1030/2016.")
    if procedimiento == "no_identificado" and regimen != "venta_estado":
        alertas.append("El aviso no indica un procedimiento de selección reconocible.")
    topes = _topes()
    if modulos is not None and not propio and regimen == "bienes_servicios" and procedimiento in topes:
        if modulos > topes[procedimiento]:
            alertas.append(f"Monto de {modulos:,.0f} M: supera el tope configurado de "
                           f"{topes[procedimiento]:,.0f} M para {NOMBRE_PROCEDIMIENTO[procedimiento].lower()}.")
    if procedimiento == "contratacion_directa" and not propio:
        alertas.append("Contratación directa: verificar la causal invocada (art. 25, inc. d, Dec. 1023/2001).")

    return {
        "regimen": regimen, "regimen_nombre": REGIMENES[regimen],
        "regimen_propio": propio,
        "procedimiento": procedimiento, "procedimiento_nombre": NOMBRE_PROCEDIMIENTO[procedimiento],
        "plataforma": plataforma, "normas": normas,
        "monto": monto, "monto_modulos": modulos, "controles": controles, "alertas": alertas,
    }


def resumir(evaluaciones: list) -> dict:
    def contar(clave):
        out = {}
        for e in evaluaciones:
            out[e[clave]] = out.get(e[clave], 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
    return {
        "avisos": len(evaluaciones),
        "por_regimen": contar("regimen_nombre"),
        "por_procedimiento": contar("procedimiento_nombre"),
        "regimen_propio": sum(1 for e in evaluaciones if e["regimen_propio"]),
        "con_alertas": sum(1 for e in evaluaciones if e["alertas"]),
    }


router = APIRouter()

# Contrataciones directas repetidas (mismo organismo y mismo proveedor en el
# período): señal de un posible desdoblamiento para quedar por debajo de los topes.
MIN_DIRECTAS_REPETIDAS = int(os.getenv("NORMATIVA_MIN_DIRECTAS_REPETIDAS", "2"))


def evaluar_contratos(filas: list) -> list:
    """evaluar_aviso() de cada contrato, más la señal de directas repetidas."""
    evaluaciones = []
    for f in filas:
        e = evaluar_aviso(f)
        e.update({
            "fecha": str(f.get("fecha") or "")[:10],
            "organismo": f.get("organismo_contratante") or f.get("organismo") or "",
            "organismo_base": f.get("organismo_base") or "",
            "tipo_proceso": f.get("tipo_proceso_bora") or f.get("tipo_proceso") or "",
            "proveedor": f.get("proveedor_adjudicado") or "",
            "cuit": str(f.get("cuit_proveedor") or "").strip(),
            "link": f.get("link_bora") or f.get("link") or "",
        })
        evaluaciones.append(e)
    grupos = {}
    for e in evaluaciones:
        if e["procedimiento"] == "contratacion_directa" and e["cuit"] and not e["regimen_propio"]:
            grupos.setdefault((e["organismo_base"] or e["organismo"], e["cuit"]), []).append(e)
    for grupo in grupos.values():
        if len(grupo) >= MIN_DIRECTAS_REPETIDAS:
            for e in grupo:
                e["alertas"].append(
                    f"{len(grupo)} contrataciones directas del mismo organismo al mismo proveedor en el "
                    "período: revisar si se desdobló una compra para quedar por debajo de los topes.")
    return evaluaciones


def _prioridad(e: dict) -> int:
    texto = " ".join(e["alertas"])
    if "desdobló" in texto:
        return 0
    if "supera el tope" in texto:
        return 1
    if e["procedimiento"] == "contratacion_directa":
        return 2
    if e["regimen_propio"]:
        return 4
    return 3


@router.get("/api/marco-normativo")
def marco_normativo():
    return {"version": VERSION_NORMATIVA, "revisado": REVISADO,
            "modulo": {"valor": VALOR_MODULO, "fuente": FUENTE_MODULO, "topes_configurados": _topes()},
            "regimenes": REGIMENES, "normas": NORMAS,
            "aviso": "Indicador de cumplimiento formal a partir del texto del aviso; no es un dictamen jurídico."}


@router.get("/api/contratos/normativa")
def normativa_contratos(organismo: str = None, desde: str = None, hasta: str = None, limite: int = 200):
    import main as m
    for valor in (desde, hasta):
        if valor and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valor):
            raise HTTPException(status_code=400, detail="Formato inválido. Use AAAA-MM-DD")
    df = m._cargar_df()
    if df.empty:
        return {"sin_datos": True}
    if "fecha" in df.columns:
        fechas = df["fecha"].astype(str).str[:10]
        if desde:
            df = df[fechas >= desde]
            fechas = fechas[df.index]
        if hasta:
            df = df[fechas <= hasta]
    if organismo:
        clave = _normalizar(organismo)
        col = "organismo_base" if "organismo_base" in df.columns else "organismo_contratante"
        df = df[df[col].astype(str).map(_normalizar).str.contains(re.escape(clave), na=False)]
    evaluaciones = evaluar_contratos(df.fillna("").to_dict(orient="records"))
    con_alertas = sorted((e for e in evaluaciones if e["alertas"]), key=lambda e: e["fecha"], reverse=True)
    # Primero lo que pide revisión (directas repetidas, topes, directas), después lo
    # informativo (entes con régimen propio); dentro de cada grupo, lo más reciente.
    con_alertas.sort(key=_prioridad)
    resumen = resumir(evaluaciones)
    resumen["directas_repetidas"] = sum(1 for e in evaluaciones if any("desdobló" in a for a in e["alertas"]))
    return m._json_seguro({"sin_datos": False, "version": VERSION_NORMATIVA, "resumen": resumen,
                           "contratos": con_alertas[:max(1, min(limite, 1000))],
                           "total_con_alertas": len(con_alertas)})
