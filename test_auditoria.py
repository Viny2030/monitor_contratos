import pytest
import pandas as pd
from analisis import analizar_boletin, MATRIZ_TEORICA

# ==========================================
# DEFINICIÓN DE GRUPOS DEMOGRÁFICOS / SECTORES
# ==========================================
CASOS_STRESS_TEST = [
    # GRUPO 1: JUBILADOS (Sector Vulnerable)
    {
        "texto": "Se decreta la nueva fórmula de movilidad jubilatoria con ajuste trimestral.",
        "grupo": "Jubilados",
        "esperado": "Jubilaciones / Pensiones",
    },
    # GRUPO 2: USUARIOS DE SERVICIOS (Población General)
    {
        "texto": "Apruébase el nuevo cuadro tarifario para la distribución de energía eléctrica.",
        "grupo": "Usuarios",
        "esperado": "Tarifas Servicios Públicos",
    },
    # GRUPO 3: EMPRESAS CONTRATISTAS (Sector Privilegiado)
    {
        "texto": "Autorízase la redeterminación de precios en la obra pública de saneamiento.",
        "grupo": "Empresas",
        "esperado": "Obra Pública / Contratos",
    },
    # GRUPO 4: CASOS CONFUSOS (Borde / Falso Positivo)
    {
        "texto": "Declaración de interés cultural a la obra de teatro local.",
        "grupo": "Cultura",
        "esperado": "No identificado",
    },
]


@pytest.mark.parametrize("caso", CASOS_STRESS_TEST)
def test_cobertura_demografica(caso):
    """
    Stress Test: Verifica que el algoritmo funcione equitativamente
    para diferentes sectores (Jubilados vs Empresas).
    """
    df_simulado = pd.DataFrame([
        {
            "fecha": "2024-01-01",
            "seccion": "primera",
            "detalle": caso["texto"],
            "tipo_decision": "No identificado",
            "link": "http://test",
        }
    ])

    df_procesado, _, _ = analizar_boletin(df_simulado)
    resultado_obtenido = df_procesado.iloc[0]["tipo_decision"]

    mensaje_error = (
        f"\n[FALLO DE SESGO EN GRUPO: {caso['grupo']}]\n"
        f"Texto: '{caso['texto']}'\n"
        f"Esperaba clasificar como: '{caso['esperado']}'\n"
        f"Pero el algoritmo dijo: '{resultado_obtenido}'\n"
        f"-> RIESGO: El sistema está ciego ante este sector."
    )

    assert resultado_obtenido == caso["esperado"], mensaje_error


def test_auditoria_diccionario_completo():
    """
    Verifica la integridad del diccionario MATRIZ_TEORICA.
    Asegura que no se hayan borrado categorías críticas accidentalmente.
    """
    sectores_criticos = [
        "Jubilaciones / Pensiones",
        "Privatización / Concesión",
        "Tarifas Servicios Públicos",
    ]

    for sector in sectores_criticos:
        assert sector in MATRIZ_TEORICA, (
            f"¡ALERTA CRÍTICA! Se borró la categoría '{sector}'."
        )
        assert len(MATRIZ_TEORICA[sector]) > 0, (
            f"La categoría '{sector}' está vacía (sin palabras clave)."
        )


# ==========================================
# INSTRUCCIONES DE USO
# ==========================================
# pytest test_auditoria.py -v