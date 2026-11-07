"""Tests del grafo organismo ↔ proveedor (/api/grafo)."""
import pandas as pd
from fastapi.testclient import TestClient

import main


def _df():
    filas = [
        # organismo, CUIT, proveedor, monto, tipo
        ("EJÉRCITO ARGENTINO", "30-71434715-9", "TUX S.A", "$100.000,00", "Licitación Pública 1/26"),
        ("EJÉRCITO ARGENTINO", "30-71434715-9", "TUX S.A", "$300.000,00", "Contratación Directa 2/26"),
        ("EJÉRCITO ARGENTINO", "30-11111111-1", "OTRA S.A", "$100.000,00", "Licitación Privada 3/26"),
        ("POLICÍA FEDERAL ARGENTINA", "30714347159", "favor de la firma TUX S.A", "", "Licitación Pública 4/26"),
        ("POLICÍA FEDERAL ARGENTINA", "", "SIN CUIT S.A", "$50,00", "Licitación Pública 5/26"),
    ]
    return pd.DataFrame([{
        "organismo_base": o, "cuit_proveedor": c, "proveedor_adjudicado": p,
        "monto_adjudicado_bora": m, "tipo_proceso_bora": t, "nivel_riesgo_licit": "Bajo",
        "fecha": pd.Timestamp("2026-10-01"),
    } for o, c, p, m, t in filas])


def test_nodos_aristas_y_sin_cuit():
    g = main._grafo_organismo_proveedor(_df())
    r = g["resumen"]
    assert (r["organismos"], r["proveedores"], r["aristas"]) == (2, 2, 3)
    assert r["sin_cuit"] == 1 and r["avisos"] == 4
    # El mismo CUIT con y sin guiones es un solo proveedor, compartido por dos organismos
    tux = next(n for n in g["nodos"] if n["id"] == "p:30714347159")
    assert tux["organismos"] == 2 and tux["avisos"] == 3 and tux["directas"] == 1
    assert tux["label"] == "TUX S.A"
    assert r["compartidos"] == 1 and r["top_compartidos"][0]["organismos"] == 2


def test_concentracion_por_monto_o_por_avisos():
    g = main._grafo_organismo_proveedor(_df())
    ej = next(n for n in g["nodos"] if n["id"] == "o:EJÉRCITO ARGENTINO")
    assert ej["concentracion_base"] == "monto" and ej["concentracion"] == 80.0  # 400.000 de 500.000
    pfa = next(n for n in g["nodos"] if n["id"] == "o:POLICÍA FEDERAL ARGENTINA")
    assert pfa["concentracion_base"] == "avisos" and pfa["concentracion"] == 100.0  # sin montos


def test_filtros_min_avisos_y_solo_compartidos():
    assert main._grafo_organismo_proveedor(_df(), min_avisos=2)["resumen"]["aristas"] == 1
    g = main._grafo_organismo_proveedor(_df(), solo_compartidos=True)
    assert {n["id"] for n in g["nodos"] if n["tipo"] == "proveedor"} == {"p:30714347159"}
    assert g["resumen"]["aristas"] == 2


def test_df_vacio():
    assert main._grafo_organismo_proveedor(pd.DataFrame())["nodos"] == []


def test_nombre_para_grafo():
    assert main._nombre_para_grafo("favor de la firma GELEC S") == "GELEC S"
    assert main._nombre_para_grafo("FERNANDO PEREZ CUIL NRO 20-37") == "FERNANDO PEREZ"
    assert main._nombre_para_grafo("SERVICIOS S.A. (") == "SERVICIOS S.A."
    assert main._nombre_para_grafo("CUITLAHUAC SA") == "CUITLAHUAC SA"


def test_endpoint_con_los_reportes():
    c = TestClient(main.app)
    r = c.get("/api/grafo")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["resumen"]["aristas"] == len(d["aristas"]) > 0
    ids = {n["id"] for n in d["nodos"]}
    assert all(a["from"] in ids and a["to"] in ids for a in d["aristas"])
    assert c.get("/api/grafo", params={"organismo": "ANSES"}).json()["resumen"]["organismos"] <= 1
