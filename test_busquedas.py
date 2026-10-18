"""Tests de búsquedas, perfiles, sin CUIT y clave de estadísticas (sobre los reportes de data/)."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import diario
import main

c = TestClient(main.app)


def _total(**params):
    r = c.get("/api/contratos", params={"limit": 1, **params})
    assert r.status_code == 200, r.text
    return r.json()["total"]


def _df():
    return main._cargar_df()


# ── Fechas ────────────────────────────────────────────────────────────────────
def test_todos_los_contratos_tienen_fecha():
    df = _df()
    assert df["fecha"].notna().mean() > 0.95  # antes: 39% (junio en adelante quedaba sin fecha)


def test_filtro_por_fecha_incluye_junio_en_adelante():
    assert _total(desde="2026-06-01") > 0
    assert _total(desde="2026-02-01") == _total()


# ── Contratos ─────────────────────────────────────────────────────────────────
def test_organismo_sin_acentos_ni_mayusculas():
    assert _total(organismo="ejercito") == _total(organismo="EJÉRCITO") > 0


def test_organismo_por_sigla():
    assert _total(organismo="ANSES") > 0


def test_caracteres_especiales_no_rompen():
    assert _total(organismo="(") == 0
    assert _total(organismo="S.A.") < _total()  # el punto ya no es comodín


def test_cuit_con_y_sin_guiones():
    cuit = next(x for x in _df()["cuit_proveedor"] if str(x).strip())
    digitos = "".join(ch for ch in cuit if ch.isdigit())
    assert _total(cuit=cuit) == _total(cuit=digitos) > 0


def test_filtro_sin_cuit_cuenta_todos_los_avisos_sin_cuit():
    vacios = int((_df()["cuit_proveedor"].astype(str).str.strip() == "").sum())
    assert _total(alerta="SIN CUIT") == vacios


# ── Organismos ────────────────────────────────────────────────────────────────
def test_nombre_base_sin_sufijos():
    assert main._organismo_base("SERVICIO PENITENCIARIO FEDERAL Solicitudes  de Provisión") == "SERVICIO PENITENCIARIO FEDERAL"
    assert main._organismo_base("NOTA ACLARATORIA - SERVICIO PENITENCIARIO FEDERAL") == "SERVICIO PENITENCIARIO FEDERAL"
    assert main._organismo_base("ARCA - DIRECCIÓN REGIONAL NORESTE Adjudicaciones Mayo/2026") == "ARCA - DIRECCIÓN REGIONAL NORESTE"


def test_perfil_por_sigla_y_sin_acentos():
    r = c.get("/api/organismo", params={"nombre": "ANSES"})
    assert r.status_code == 200 and r.json()["total_contratos"] > 0
    assert c.get("/api/organismo", params={"nombre": "ejercito argentino"}).json()["organismo"] == "EJÉRCITO ARGENTINO"


def test_perfil_exacto_no_suma_las_dependencias():
    exacto = c.get("/api/organismo", params={"nombre": "AGENCIA DE RECAUDACIÓN Y CONTROL ADUANERO"}).json()
    juntos = c.get("/api/organismo", params={"nombre": "AGENCIA DE RECAUDACIÓN Y CONTROL ADUANERO", "agrupar": True}).json()
    assert exacto["organismos_incluidos"] == ["AGENCIA DE RECAUDACIÓN Y CONTROL ADUANERO"]
    assert juntos["total_contratos"] > exacto["total_contratos"]


def test_busqueda_ambigua_ofrece_opciones():
    d = c.get("/api/organismo", params={"nombre": "aduanera"}).json()
    assert d["ambiguo"] is True and len(d["coincidencias"]) > 1


def test_no_encontrado_sugiere():
    r = c.get("/api/organismo", params={"nombre": "zzzz inexistente"})
    assert r.status_code == 404


def test_lista_y_ruta_anterior_siguen_funcionando():
    lista = c.get("/api/organismos/lista").json()["data"]
    assert lista
    assert c.get("/api/organismos/" + lista[0]["organismo"]).status_code == 200
    con_barra = [o["organismo"] for o in lista if "/" in o["organismo"]]
    for nombre in con_barra:  # por parámetro funcionan aunque tengan "/"
        assert c.get("/api/organismo", params={"nombre": nombre}).status_code == 200


# ── Proveedores ───────────────────────────────────────────────────────────────
def test_todos_los_perfiles_de_proveedor_responden():
    provs = c.get("/api/proveedores", params={"top": 100}).json()["data"]
    assert provs
    for p in provs:
        r = c.get("/api/proveedores/" + p["cuit"])
        assert r.status_code == 200, (p["cuit"], r.text[:200])


def test_busqueda_de_proveedor_por_cuit_sin_guiones():
    p = c.get("/api/proveedores", params={"top": 1}).json()["data"][0]
    digitos = "".join(ch for ch in p["cuit"] if ch.isdigit())
    assert any(x["cuit"] == p["cuit"] for x in c.get("/api/proveedores", params={"busqueda": digitos}).json()["data"])


# ── Sin CUIT ──────────────────────────────────────────────────────────────────
def test_sin_cuit():
    d = c.get("/api/sin-cuit").json()
    assert d["total"] > 0 and d["data"][0]["link_bora"].startswith("https://")
    assert d["por_organismo"]


# ── Extracción ────────────────────────────────────────────────────────────────
def test_cuit_sin_guiones():
    assert diario._normalizar_cuit("CUIT 20180531778 domicilio") == "20-18053177-8"


# ── Correcciones por aviso ────────────────────────────────────────────────────
def test_correcciones_solo_completan_vacios(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CORRECCIONES_DIR", str(tmp_path))
    pd.DataFrame([{"link_bora": "a", "cuit_proveedor": "30-71434715-9"},
                  {"link_bora": "b", "cuit_proveedor": "30-71620292-1"}]).to_csv(tmp_path / "x.csv", index=False)
    df = pd.DataFrame({"link_bora": ["a", "b"], "cuit_proveedor": ["", "20-11111111-2"],
                       "proveedor_adjudicado": ["", ""], "monto_adjudicado_bora": ["", ""]})
    out = main._aplicar_correcciones(df)
    assert list(out["cuit_proveedor"]) == ["30-71434715-9", "20-11111111-2"]


# ── Estadísticas ──────────────────────────────────────────────────────────────
def test_clave_de_estadisticas(monkeypatch):
    monkeypatch.setattr(main, "STATS_KEY", "clave-nueva")
    monkeypatch.setattr(main, "get_stats", lambda: {"total_visitas": 1})
    assert c.get("/api/stats", headers={"X-Stats-Key": "clave-nueva"}).status_code == 200
    assert c.get("/api/stats", headers={"X-Stats-Key": "otra"}).status_code == 401
    assert c.get("/api/stats").status_code == 401
