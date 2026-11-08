"""/api/frescura: reportes diarios y corte de COMPR.AR."""
from fastapi.testclient import TestClient

import main


def test_frescura_contratos():
    d = TestClient(main.app).get("/api/frescura").json()
    assert d["formato"] == "frescura/1" and d["sitio"] == "contratos"
    filas = {x["id"]: x for x in d["datos"]}
    assert set(filas) == {"reportes", "cruce_comprar"}
    assert filas["cruce_comprar"]["periodo_fin"]  # sale de data/cruce/comprar.csv
    assert filas["reportes"]["ultima_carga"][:2] == "20"
