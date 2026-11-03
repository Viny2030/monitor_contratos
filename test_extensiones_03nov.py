"""Tests de la carga del dataset, el diagnóstico, el marco normativo y el cruce MEACI (03/11/2026).

Ningún test sale a la red: GitHub, MEACI y el BORA se reemplazan por dobles.
"""
import pytest
from fastapi.testclient import TestClient

import main
from extensiones import dataset, diagnostico, marco_normativo

c = TestClient(main.app)


# ── Dataset ──────────────────────────────────────────────────────────────────
def test_sha_blob_coincide_con_git():
    assert dataset.sha_blob(b"hola\n") == "5c1b14949828006ed75a3e8858957f86a2f7e2eb"  # git hash-object


@pytest.mark.parametrize("ruta,entra", [
    ("data/2026-11/reporte_2026-11-03.xlsx", True),
    ("data/2026-11/flujo_licitaciones_2026-11-03.xlsx", True),
    ("data/analisis/concentracion_2026-11-03.xlsx", True),
    ("data/correcciones/avisos_reextraidos.csv", True),
    ("data/perfiles/perfil_cuit_1.xlsx", False),
    ("data/2026-11/otra_cosa.xlsx", False),
    ("main.py", False),
])
def test_que_archivos_se_sincronizan(ruta, entra):
    assert bool(dataset._RUTA_DATASET.match(ruta)) is entra


def _github(monkeypatch, archivos, rotos=()):
    monkeypatch.setattr(dataset, "listar_publicados", lambda: [
        {"relativa": r, "sha": dataset.sha_blob(b)} for r, b in archivos.items()])
    bajados = []

    def descargar(relativa):
        bajados.append(relativa)
        return b"corrupto" if relativa in rotos else archivos[relativa]
    monkeypatch.setattr(dataset, "_descargar", descargar)
    return bajados


def test_sincroniza_lo_que_falta_respalda_lo_que_cambia_y_no_repite(tmp_path, monkeypatch):
    monkeypatch.setenv("RESPALDOS_DIR", str(tmp_path / "respaldos"))
    data = tmp_path / "data"
    (data / "analisis").mkdir(parents=True)
    (data / "analisis" / "concentracion_2026-11-03.xlsx").write_bytes(b"vieja")
    archivos = {"2026-11/reporte_2026-11-03.xlsx": b"reporte", "analisis/concentracion_2026-11-03.xlsx": b"nueva"}
    bajados = _github(monkeypatch, archivos)
    r = dataset.sincronizar(str(data))
    assert r["nuevos"] == ["2026-11/reporte_2026-11-03.xlsx"]
    assert r["actualizados"] == ["analisis/concentracion_2026-11-03.xlsx"]
    assert r["ultimo_dia_publicado"] == "2026-11-03"
    assert (data / "analisis" / "concentracion_2026-11-03.xlsx").read_bytes() == b"nueva"
    assert any((tmp_path / "respaldos" / "analisis").iterdir())
    assert dataset.sincronizar(str(data))["nuevos"] == [] and len(bajados) == 2


def test_archivo_que_no_coincide_no_se_guarda(tmp_path, monkeypatch):
    _github(monkeypatch, {"2026-11/reporte_2026-11-03.xlsx": b"x"}, rotos={"2026-11/reporte_2026-11-03.xlsx"})
    r = dataset.sincronizar(str(tmp_path))
    assert r["nuevos"] == [] and r["errores"][0]["archivo"] == "2026-11/reporte_2026-11-03.xlsx"
    assert not (tmp_path / "2026-11" / "reporte_2026-11-03.xlsx").exists()


def test_endpoint_actualizar_informa_si_github_falla(monkeypatch):
    dataset._estado.update({"corriendo": False, "ultima": None, "_t": 0.0})

    def falla():
        raise RuntimeError("sin red")
    monkeypatch.setattr(dataset, "listar_publicados", falla)
    r = c.post("/api/datos/actualizar")
    assert r.status_code == 502 and "sin red" in r.json()["detail"]


# ── Diagnóstico ──────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, ctype="application/json"):
        self.status_code, self.headers = status, {"content-type": ctype}


def test_meaci_dado_de_baja_se_detecta(monkeypatch):
    monkeypatch.setattr(diagnostico.requests, "get", lambda *a, **k: _Resp(404, "text/html"))
    assert diagnostico.probar_meaci("https://viejo.example")["alcanzable"] is False
    monkeypatch.setattr(diagnostico.requests, "get", lambda *a, **k: _Resp(200))
    assert diagnostico.probar_meaci("https://meaci.example")["alcanzable"] is True


def test_endpoint_diagnostico(monkeypatch):
    monkeypatch.setattr(diagnostico, "probar_bora", lambda: {"alcanzable": False, "http": None, "segundos": 8.0})
    monkeypatch.setattr(diagnostico, "probar_meaci", lambda url: {"alcanzable": True, "http": 200, "url": url})
    d = c.get("/api/diagnostico", params={"forzar": True}).json()
    assert d["bora"]["alcanzable"] is False and d["meaci"]["alcanzable"] is True
    assert d["ultimo_reporte"].startswith("reporte_")


# ── MEACI: una falla ya no se ve como "sin coincidencias" ────────────────────
class _ClienteFalso:
    def __init__(self, resp):
        self.resp = resp

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return self.resp


def test_cruce_cuit_distingue_no_encontrado_de_meaci_caido(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _ClienteFalso(_Resp(404)))
    assert c.get("/api/cruce-cuit", params={"cuit": "1"}).json()["detalle"] == "sin coincidencias"
    monkeypatch.setattr(main.httpx, "AsyncClient", _ClienteFalso(_Resp(404, "text/html")))
    d = c.get("/api/cruce-cuit", params={"cuit": "1"}).json()
    assert "error" in d and "detalle" not in d


def test_cruce_bulk_informa_el_error(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _ClienteFalso(_Resp(404, "text/html")))
    d = c.get("/api/cruce-cuits-bulk", params={"cuits": "1,2"}).json()
    assert d["alertas"] == {} and "MEACI no disponible" in d["error"]


# ── Marco normativo ──────────────────────────────────────────────────────────
def _fila(organismo, tipo, cuit="", monto=""):
    return {"organismo_contratante": organismo, "organismo_base": organismo, "tipo_proceso_bora": tipo,
            "cuit_proveedor": cuit, "monto_adjudicado_bora": monto, "fecha": "2026-10-30"}


def test_directas_repetidas_al_mismo_proveedor():
    filas = [_fila("MINISTERIO DE SALUD", "Contratación Directa 1/2026", "30-1"),
             _fila("MINISTERIO DE SALUD", "Contratación Directa 2/2026", "30-1"),
             _fila("MINISTERIO DE SALUD", "Licitación Pública 3/2026", "30-1"),
             _fila("EMPRESA ARGENTINA DE NAVEGACIÓN AÉREA S.E.", "Contratación Directa 4/2026", "30-2"),
             _fila("EMPRESA ARGENTINA DE NAVEGACIÓN AÉREA S.E.", "Contratación Directa 5/2026", "30-2")]
    ev = marco_normativo.evaluar_contratos(filas)
    repetidas = [any("desdobló" in a for a in e["alertas"]) for e in ev]
    assert repetidas == [True, True, False, False, False]  # el ente con régimen propio no se evalúa
    assert ev[3]["regimen_propio"]


def test_endpoint_normativa_contratos():
    d = c.get("/api/contratos/normativa", params={"limite": 5}).json()
    assert d["sin_datos"] is False and d["resumen"]["avisos"] > 0
    assert len(d["contratos"]) <= 5 and all(e["alertas"] for e in d["contratos"])
    assert c.get("/api/contratos/normativa", params={"desde": "mal"}).status_code == 400
    assert c.get("/api/marco-normativo").json()["modulo"]["valor"] == marco_normativo.VALOR_MODULO
