"""Tests de exportación CSV, tendencias, estado, refresco en segundo plano, límite de IA y alertas."""
import io
import time

import pandas as pd
from fastapi.testclient import TestClient

import main

c = TestClient(main.app)


def _leer_csv(r):
    assert r.status_code == 200, r.text[:200]
    assert r.content.startswith("﻿".encode("utf-8"))  # BOM: Excel respeta los acentos
    return pd.read_csv(io.BytesIO(r.content), sep=";", dtype=str, encoding="utf-8-sig")


def test_csv_de_contratos_respeta_los_filtros():
    total = c.get("/api/contratos", params={"limit": 1, "organismo": "ejercito"}).json()["total"]
    df = _leer_csv(c.get("/api/contratos.csv", params={"organismo": "ejercito"}))
    assert len(df) == total > 0
    assert {"fecha", "cuit_proveedor", "link_bora"} <= set(df.columns)


def test_csv_sin_cuit_organismo_y_proveedor():
    assert len(_leer_csv(c.get("/api/sin-cuit.csv"))) == c.get("/api/sin-cuit").json()["total"]
    assert len(_leer_csv(c.get("/api/organismo.csv", params={"nombre": "ANSES"}))) > 0
    cuit = c.get("/api/proveedores", params={"top": 1}).json()["data"][0]["cuit"]
    assert len(_leer_csv(c.get("/api/proveedor.csv", params={"cuit": cuit}))) > 0
    assert c.get("/api/organismo.csv", params={"nombre": "aduanera"}).status_code == 404  # ambiguo


def test_ranking_por_cantidad():
    d = c.get("/api/organismos", params={"top": 10, "orden": "cantidad"}).json()["data"]
    cantidades = [o["adjudicaciones"] for o in d]
    assert cantidades == sorted(cantidades, reverse=True)


def test_resumen_informa_cuantos_avisos_tienen_monto():
    r = c.get("/api/resumen").json()
    assert 0 < r["con_monto"] < r["total"]


def test_tendencias():
    d = c.get("/api/tendencias").json()
    assert d["mes_actual"] > d["mes_anterior"]
    assert {"suben", "mas_directas", "totales"} <= set(d)


def test_estado():
    e = c.get("/api/estado").json()
    assert e["ultimo_reporte"]["archivo"].startswith("reporte_")
    assert 0 < e["cobertura"]["cuit_pct"] < 100
    assert "dias_habiles_sin_reporte_30d" in e


def test_refresh_en_segundo_plano(monkeypatch):
    import diario
    monkeypatch.setattr(diario, "main", lambda: pd.DataFrame([{"a": 1}]))
    h = {"x-refresh-token": main.REFRESH_TOKEN}
    r = c.post("/api/refresh", headers=h)
    assert r.status_code == 202
    for _ in range(50):
        if not c.get("/api/refresh/estado").json()["corriendo"]:
            break
        time.sleep(0.1)
    assert c.get("/api/refresh/estado").json()["registros"] == 1
    assert c.post("/api/refresh", headers={"x-refresh-token": "otro"}).status_code == 401


def test_clave_de_ia_usa_la_ip_que_agrega_el_proxy():
    class R:
        headers = {"x-forwarded-for": "1.1.1.1, 9.9.9.9"}
        client = None
    class R2:
        headers = {"x-forwarded-for": "2.2.2.2, 9.9.9.9"}
        client = None
    # Cambiar la primera IP (la que escribe el visitante) ya no cambia la clave
    assert main._ia_clave(R()) == main._ia_clave(R2())


def test_limite_de_ia_sin_base_sigue_en_memoria(monkeypatch):
    import agentic_ai
    monkeypatch.setattr(agentic_ai, "ia_disponible", lambda: True)
    monkeypatch.setattr(main, "_ia_usos", {"dia": None, "por_ip": {}, "total": 0})
    llamado = lambda: {"disponible": True, "explicacion": "ok"}
    class R:
        headers = {}
        class client:
            host = "5.5.5.5"
    codigos = []
    for _ in range(main.IA_LIMITE_POR_IP + 1):
        r = main._ia_con_limite(R(), llamado)
        codigos.append(getattr(r, "status_code", 200))
    assert codigos[-1] == 429 and codigos.count(200) == main.IA_LIMITE_POR_IP


def test_alertas_sin_novedades_no_mandan_mail(monkeypatch):
    import sys
    sys.path.insert(0, "tools")
    import alertas_dia
    monkeypatch.setattr(alertas_dia, "_meaci", lambda cuits: {})
    hay, _ = alertas_dia.armar("2026-10-16")
    assert hay is False
    hay, _ = alertas_dia.armar("1999-01-01")  # sin reporte
    assert hay is False
