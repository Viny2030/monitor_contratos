"""Una coincidencia por organismo no puede publicarse como que el proveedor cobró."""
import os
import sys

import pandas as pd
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analisis_concentracion as ac  # noqa: E402
import cruce_comprar as cc  # noqa: E402
import main  # noqa: E402
from cobro_tgn import ALERTA_ORGANISMO, NO, SI, normalizar_df, normalizar_fila  # noqa: E402


def test_fila_vieja_por_organismo_o_sin_metodo_no_dice_cobro():
    for metodo in ("organismo", "", None):
        r = normalizar_fila({
            "cobro_en_tgn": SI, "metodo_cruce_tgn": metodo, "alerta": "🔶 BORA + TGN (cobró)",
            "beneficiario_tgn": "ORGANISMO X", "monto_cobrado_tgn": 12.5,
        })
        assert r["cobro_en_tgn"] == NO
        assert r["organismo_con_pagos_tgn"] == SI
        assert r["alerta"] == ALERTA_ORGANISMO
        assert r["beneficiario_tgn"] == "" and r["monto_cobrado_tgn"] == ""
        assert r["monto_pagado_tgn"] == 12.5


def test_monto_pagado_existente_no_se_pisa():
    r = normalizar_fila({"cobro_en_tgn": SI, "monto_cobrado_tgn": 1, "monto_pagado_tgn": 2})
    assert r["monto_pagado_tgn"] == 2


def test_coincidencia_por_cuit_se_conserva():
    f = {"cobro_en_tgn": SI, "metodo_cruce_tgn": "CUIT", "alerta": "🔶 BORA + TGN (cobró)",
         "beneficiario_tgn": "X SA", "monto_cobrado_tgn": 3}
    r = normalizar_fila(f)
    assert r["cobro_en_tgn"] == SI and r["organismo_con_pagos_tgn"] == SI
    assert r["alerta"] == f["alerta"] and r["beneficiario_tgn"] == "X SA" and r["monto_cobrado_tgn"] == 3


def test_flujo_completo_y_filas_sin_cruce_no_cambian():
    r = normalizar_fila({"cobro_en_tgn": SI, "alerta": "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN"})
    assert r["alerta"].startswith("🚨") and r["organismo_con_pagos_tgn"] == SI
    r = normalizar_fila({"cobro_en_tgn": NO, "alerta": "✅ ADJUDICADO (sin cruce aún)"})
    assert r["organismo_con_pagos_tgn"] == NO
    otra = {"organismo": "X"}
    assert normalizar_fila(otra) == otra
    df = pd.DataFrame([otra])
    assert normalizar_df(df).equals(df)


def test_alerta_recalculada_conserva_el_cruce_con_reportes_nuevos():
    """El cruce con COMPR.AR recalcula la alerta: no puede perder el dato de TGN."""
    base = {"cuit_proveedor": "30-1", "cobro_en_tgn": NO, "organismo_con_pagos_tgn": SI,
            "cancelada": "False", "metodo_cruce_tgn": "organismo"}
    assert cc.alerta({**base, "en_comprar": NO}) == ALERTA_ORGANISMO
    assert cc.alerta({**base, "en_comprar": cc.SI}).startswith("🚨 FLUJO COMPLETO")
    assert cc.alerta({"cuit_proveedor": "30-1", "cobro_en_tgn": NO, "cancelada": "False",
                      "en_comprar": NO}).startswith("✅ ADJUDICADO")


def test_sin_cruce_por_cuit_no_se_acusa_a_nadie_de_fantasma():
    df = pd.DataFrame([{
        "cuit_proveedor": "30-71434715-9", "proveedor_adjudicado": "X SA",
        "organismo_contratante": "ORG", "monto_adjudicado_bora": "$ 1.000,00",
        "cobro_en_tgn": NO, "organismo_con_pagos_tgn": SI, "metodo_cruce_tgn": "organismo",
        "fecha_publicacion": "2026-11-06",
    }])
    assert ac.detectar_fantasmas(df).empty


def _datos():
    return pd.DataFrame([
        {"organismo_contratante": "ORG", "organismo_base": "ORG", "cuit_proveedor": "30-1",
         "monto_adjudicado_bora": "", "cobro_en_tgn": NO, "organismo_con_pagos_tgn": SI,
         "metodo_cruce_tgn": "organismo", "alerta": ALERTA_ORGANISMO},
        {"organismo_contratante": "ORG", "organismo_base": "ORG", "cuit_proveedor": "30-2",
         "monto_adjudicado_bora": "", "cobro_en_tgn": NO, "organismo_con_pagos_tgn": NO,
         "metodo_cruce_tgn": "", "alerta": "✅ ADJUDICADO (sin cruce aún)"},
    ])


def test_resumen_separa_organismos_con_pagos_de_cobros(monkeypatch):
    monkeypatch.setattr(main, "_cargar_df", _datos)
    d = TestClient(main.app).get("/api/resumen").json()
    assert d["con_pagos_organismo_tgn"] == 1
    assert d["cobraron_tgn"] == 0


def test_pagina_usa_la_clave_nueva():
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "index.html")
    with open(ruta, encoding="utf-8") as f:
        pagina = f.read()
    assert "res.cobraron_tgn" not in pagina
    assert "res.con_pagos_organismo_tgn" in pagina


def test_el_prompt_de_ia_no_afirma_montos_cobrados():
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentic_ai.py")
    with open(ruta, encoding="utf-8") as f:
        codigo = f.read()
    assert "efectivamente cobrado" not in codigo
