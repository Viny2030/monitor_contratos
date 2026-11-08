"""Qué se puede afirmar a partir del cruce con TGN.

Presupuesto Abierto no informa el CUIT del beneficiario. Una coincidencia por nombre
de organismo dice que ese organismo tuvo pagos en el año, no que el proveedor adjudicado
haya cobrado. Sólo una coincidencia por CUIT permite decir "cobró".

Los reportes guardados tienen `cobro_en_tgn = SÍ` por organismo o sin método, y a veces
beneficiario y monto "cobrado" que en realidad son del organismo. No se modifican los
Excel: este módulo corrige lo que publica el sitio al leerlos.
"""
import pandas as pd

SI = "✅ SÍ"
NO = "❌ NO"
ETAPA_ORGANISMO = "💰 ADJUDICADO + ORGANISMO CON PAGOS TGN"
ALERTA_ORGANISMO = "🔶 BORA + TGN (por organismo)"


def _si(valor) -> bool:
    return "SÍ" in str(valor or "")


def por_cuit(fila: dict) -> bool:
    return str(fila.get("metodo_cruce_tgn") or "").strip().upper() == "CUIT"


def hubo_pagos(fila: dict) -> bool:
    """El organismo tuvo pagos en TGN, según cualquiera de los dos campos."""
    return _si(fila.get("cobro_en_tgn")) or _si(fila.get("organismo_con_pagos_tgn"))


def normalizar_fila(fila: dict) -> dict:
    """Devuelve una copia en la que "cobró" sólo queda si el cruce fue por CUIT."""
    if not any(c in fila for c in ("cobro_en_tgn", "etapa", "alerta")):
        return fila
    r = dict(fila)
    cuit = por_cuit(r)

    if "cobro_en_tgn" in r:
        r["organismo_con_pagos_tgn"] = SI if hubo_pagos(r) else NO
        if not cuit:
            r["cobro_en_tgn"] = NO

    if not cuit:
        if "COBRÓ" in str(r.get("etapa") or ""):
            r["etapa"] = ETAPA_ORGANISMO
        if "cobró" in str(r.get("alerta") or ""):
            r["alerta"] = ALERTA_ORGANISMO
        # Son datos del organismo: no van con nombre de "cobrado" al lado de un proveedor.
        if str(r.get("monto_cobrado_tgn") or "").strip() and not str(r.get("monto_pagado_tgn") or "").strip():
            r["monto_pagado_tgn"] = r["monto_cobrado_tgn"]
        for campo in ("monto_cobrado_tgn", "beneficiario_tgn"):
            if campo in r:
                r[campo] = ""
    return r


def normalizar_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    if not any(c in df.columns for c in ("cobro_en_tgn", "etapa", "alerta")):
        return df
    return pd.DataFrame(
        [normalizar_fila(f) for f in df.to_dict(orient="records")],
        index=df.index,
    )
