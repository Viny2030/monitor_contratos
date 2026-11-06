"""Cruce BORA → COMPR.AR 2016-2026 (cruce_comprar.py) y su aplicación en el sitio (sin red)."""
import pandas as pd
import pytest

import cruce_comprar as cc

FILAS = [
    # mismo proceso que "Licitación Pública 208/2026" del Ejército (SAF 374), dos renglones
    ("84/16-0208-LPU26", "374 - Estado Mayor General del Ejercito", "2026", "10/03/2026", "30-11111111-1", "Tux SA", "100000.50", "Peso Argentino"),
    ("84/16-0208-LPU26", "374 - Estado Mayor General del Ejercito", "2026", "10/03/2026", "30-11111111-1", "Tux SA", "200000.00", "Peso Argentino"),
    # mismo número y tipo pero OTRO organismo: no debe cruzar con el Ejército
    ("37/1-0208-LPU26", "375 - Gendarmeria Nacional", "2026", "15/03/2026", "30-22222222-2", "Otra SA", "5.00", "Peso Argentino"),
    # proceso de la Policía Federal (SAF 326, alias)
    ("30-0002-LPU26", "326 - Super Intendencia de la Administracion de la Policia Federal", "2026", "09/07/2026", "30-33333333-3", "Skop SA", "300.00", "Peso Argentino"),
]


@pytest.fixture
def indice():
    df = pd.DataFrame(FILAS, columns=["Numero_Proceso", "Descripcion_SAF", "Ejercicio", "Fecha_de_Adjudicacion",
                                      "CUIT", "Descripcion_Proveedor", "Monto", "Moneda"])
    df["Descripcion_UOC"] = "UOC"
    df["Tipo_de_Procedimiento"] = "Licitacion Pública"
    df["Modalidad"] = "Sin Modalidad"
    df["Rubros"] = "INFORMATICA;"
    return cc.IndiceComprar(df)


def test_tipo_numero():
    assert cc.tipo_numero("Licitación Pública 208/2026") == ("LPU", 208, "26")
    assert cc.tipo_numero("Licitación Privada (Modo IV) 10/2025") == ("LPR", 10, "25")
    assert cc.tipo_numero("Licitación Pública") is None


def test_cruza_el_mismo_proceso_del_mismo_organismo(indice):
    r = indice.cruzar("Licitación Pública 208/2026", "EJÉRCITO ARGENTINO", "2026-06-01")
    assert r["en_comprar"] == cc.SI
    assert r["nro_proceso_comprar"] == "84/16-0208-LPU26"
    assert r["monto_comprar"] == 300000.5            # suma de los renglones
    assert r["cuit_comprar"] == "30-11111111-1" and r["proveedor_comprar"] == "Tux SA"


def test_alias_por_saf_y_sufijos_del_bora(indice):
    r = indice.cruzar("Licitación Pública 2/2026", "POLICÍA FEDERAL ARGENTINA Solicitudes de Provisión", "2026-06-01")
    assert r["en_comprar"] == cc.SI and r["nro_proceso_comprar"] == "30-0002-LPU26"


def test_estados_fuera_pendiente_y_no(indice):
    assert indice.cruzar("Licitación Pública 1/2026", "BANCO DE LA NACIÓN ARGENTINA", "2026-06-01")["en_comprar"] == cc.FUERA
    pend = indice.cruzar("Licitación Pública 999/2026", "EJÉRCITO ARGENTINO", "2026-09-15")["en_comprar"]
    assert pend == cc.PENDIENTE.format("09/07/2026")
    assert indice.cruzar("Licitación Pública 999/2026", "EJÉRCITO ARGENTINO", "2026-05-15")["en_comprar"] == cc.NO
    assert indice.cruzar("Licitación Pública", "EJÉRCITO ARGENTINO", "2026-05-15")["en_comprar"] == cc.NO


def test_alerta_con_el_cruce_nuevo():
    base = {"cuit_proveedor": "30-1", "cobro_en_tgn": "❌ NO", "cancelada": "False"}
    assert cc.alerta({**base, "en_comprar": cc.SI}) == "🔷 BORA + COMPRAR"
    assert cc.alerta({**base, "en_comprar": cc.FUERA}) == "✅ ADJUDICADO (sin cruce aún)"
    assert cc.alerta({**base, "en_comprar": cc.SI, "cobro_en_tgn": "✅ SÍ"}) == "🚨 FLUJO COMPLETO: BORA→COMPRAR→TGN"
    assert cc.alerta({"cuit_proveedor": "", "en_comprar": cc.SI}) == "⚠️ SIN CUIT EXTRAÍDO"


def test_el_sitio_aplica_el_cruce_y_completa_solo_lo_vacio(tmp_path, monkeypatch):
    import main
    csv = tmp_path / "comprar.csv"
    pd.DataFrame([
        {"link_bora": "a", "en_comprar": cc.SI, "nro_proceso_comprar": "84/16-0208-LPU26", "monto_comprar": "300000.5",
         "proveedor_comprar": "Tux SA", "cuit_comprar": "30-11111111-1"},
        {"link_bora": "b", "en_comprar": cc.SI, "monto_comprar": "999.0", "proveedor_comprar": "X SA", "cuit_comprar": "30-9"},
        {"link_bora": "c", "en_comprar": cc.FUERA},
    ]).to_csv(csv, index=False)
    monkeypatch.setattr(main, "CRUCE_COMPRAR_CSV", str(csv))
    df = pd.DataFrame([
        {"link_bora": "a", "en_comprar": "❌ NO", "cuit_proveedor": "", "proveedor_adjudicado": "", "monto_adjudicado_bora": "", "cobro_en_tgn": "❌ NO", "alerta": "⚠️ SIN CUIT EXTRAÍDO"},
        {"link_bora": "b", "en_comprar": "❌ NO", "cuit_proveedor": "30-1", "proveedor_adjudicado": "Y SA", "monto_adjudicado_bora": "$5.000.000,00", "cobro_en_tgn": "❌ NO", "alerta": ""},
        {"link_bora": "c", "en_comprar": "✅ SÍ", "cuit_proveedor": "", "proveedor_adjudicado": "", "monto_adjudicado_bora": "$66", "cobro_en_tgn": "❌ NO", "alerta": ""},
    ])
    out = main._aplicar_cruce_comprar(main._limpiar_montos_bora(df)).set_index("link_bora")
    assert out.loc["a", "cuit_proveedor"] == "30-11111111-1" and out.loc["a", "monto_adjudicado_bora"] == "300000.5"
    assert out.loc["a", "monto_fuente"] == "COMPR.AR" and out.loc["a", "alerta"] == "🔷 BORA + COMPRAR"
    # lo que ya trajo el BORA no se pisa
    assert out.loc["b", "cuit_proveedor"] == "30-1" and out.loc["b", "monto_adjudicado_bora"] == "$5.000.000,00"
    assert out.loc["b", "monto_fuente"] == "BORA"
    # el cruce viejo "✅ SÍ" se corrige y el "$66" cortado se descarta
    assert out.loc["c", "en_comprar"] == cc.FUERA and out.loc["c", "monto_adjudicado_bora"] == ""
