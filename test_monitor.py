"""Tests de extracción, indicadores de riesgo, carga del tablero y límite de IA."""
import os
import pandas as pd
import diario

AVISO = ('Texto del Aviso: Objeto: "ADQUISICIÓN DE PRODUCTOS CARNICOS" ADJUDICATARIO: TUX S.A '
         'CUIT: 30-71434715-9 POR UN TOTAL DE PESOS ... ($ 166.115.250,00) e. 14/09/2026')


def test_extractores():
    assert diario._extraer_proveedor(AVISO) == "TUX S.A"
    assert diario._extraer_monto_bora(AVISO) == "$166.115.250,00"
    assert diario._normalizar_cuit(AVISO) == "30-71434715-9"
    assert diario._normalizar_cuit("CUIT 30-71434715-8") == ""  # dígito verificador inválido


def _fila(**kw):
    f = {"cancelada": False, "monto_adjudicado_bora": "", "tipo_procedimiento": "",
         "tipo_proceso_bora": "Licitación Pública 1/26", "cuit_proveedor": "30-71434715-9",
         "cobro_en_tgn": "❌ NO"}
    f.update(kw)
    return f


def test_riesgo_sin_indicadores_que_no_miden_riesgo():
    df = diario._agregar_riesgo(pd.DataFrame([
        _fila(),                                   # nada
        _fila(cuit_proveedor=""),                  # sin CUIT: informativo, 0 puntos
        _fila(monto_adjudicado_bora="$5.000.000"),  # monto chico cualquiera: ya no suma
        _fila(tipo_proceso_bora="Contratación Directa 3/26"),
    ]))
    assert list(df["score_riesgo_licit"]) == [0, 0, 0, 3]
    assert list(df["nivel_riesgo_licit"]) == ["Bajo", "Bajo", "Bajo", "Medio"]


def test_monto_justo_bajo_tope():
    tope = 1_000 * diario.MODULO_ARS
    df = diario._agregar_riesgo(pd.DataFrame([_fila(monto_adjudicado_bora=f"${tope*0.95:,.0f}".replace(",", "."))]))
    assert df.iloc[0]["score_riesgo_licit"] == 2


def test_tablero_ignora_hojas_que_no_son_flujo(tmp_path, monkeypatch):
    import main
    d = tmp_path / "2026-10"; d.mkdir()
    with pd.ExcelWriter(d / "reporte_2026-10-06.xlsx") as w:
        pd.DataFrame({"nro_proceso": range(50)}).to_excel(w, sheet_name="🛒 Comprar ONC", index=False)
    fila = {"link_bora": "L1", "cuit_proveedor": "30714347159", "organismo_contratante": "X", "nivel_riesgo_licit": "Bajo"}
    for dia in ["2026-10-07", "2026-10-08"]:  # mismo aviso en dos días (feriado)
        with pd.ExcelWriter(d / f"reporte_{dia}.xlsx") as w:
            pd.DataFrame([fila]).to_excel(w, sheet_name="🚨 Flujo Completo", index=False)
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    main._invalidar_cache()
    df = main._cargar_df()
    main._invalidar_cache()
    assert len(df) == 1
    assert df.iloc[0]["cuit_proveedor"] == "30714347159"


def test_limite_ia(monkeypatch):
    from fastapi.testclient import TestClient
    import main, agentic_ai
    monkeypatch.setattr(agentic_ai, "ia_disponible", lambda: True)
    monkeypatch.setattr(agentic_ai, "explicar_perfil_organismo", lambda p: {"disponible": True, "explicacion": "ok"})
    main._ia_usos.update({"dia": None, "por_ip": {}, "total": 0})
    c = TestClient(main.app)
    assert [c.post("/api/ia/explicar-organismo", json={}).status_code for _ in range(4)] == [200, 200, 200, 429]
