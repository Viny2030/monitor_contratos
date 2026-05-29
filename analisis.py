"""
analisis.py — Matriz XAI Monteverde
Motor de clasificación de fenómenos corruptivos legales.

Referencia:
    Monteverde, V. H. (2020). Great corruption – theory of corrupt phenomena.
    Journal of Financial Crime, Vol. 28 No. 2, pp. 580-595.
    https://doi.org/10.1108/JFC-04-2020-0062
"""

import re
import unicodedata
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# MATRIZ TEÓRICA — 7 escenarios de Monteverde (2020)
# ─────────────────────────────────────────────────────────────────────────────

MATRIZ_TEORICA = {
    "Privatización / Concesión": {
        "keywords": [
            "concesion", "privatizacion", "venta de pliegos", "subasta",
            "licitacion de concesion", "adjudicacion de concesion",
            "transferencia de activos", "enajenacion",
        ],
        "transferencia": "Estado → Privados",
        "peso": 9,
    },
    "Obra Pública / Contratos": {
        "keywords": [
            "obra publica", "licitacion publica", "contratacion directa",
            "redeterminacion de precios", "contrato de obra",
            "adjudicacion de obra", "construccion", "infraestructura",
            "vial", "hidraulica", "edilicia",
        ],
        "transferencia": "Estado → Empresas Contratistas",
        "peso": 8,
    },
    "Tarifas Servicios Públicos": {
        "keywords": [
            "tarifa", "aumento tarifario", "cuadro tarifario",
            "servicio publico", "distribucion electrica", "gas natural",
            "agua potable", "transporte publico", "peaje",
            "concesionaria de servicio",
        ],
        "transferencia": "Usuarios → Concesionarias",
        "peso": 7,
    },
    "Precios Regulados": {
        "keywords": [
            "precio regulado", "precio maximo", "precio minimo",
            "canasta basica", "precio de referencia", "precio sugerido",
            "acuerdo de precios", "congelamiento de precios",
        ],
        "transferencia": "Consumidores → Productores",
        "peso": 6,
    },
    "Salarios y Paritarias": {
        "keywords": [
            "paritaria", "convenio colectivo", "salario minimo",
            "actualizacion salarial", "aumento salarial", "smvm",
            "remuneracion", "escala salarial",
        ],
        "transferencia": "Asalariados → Empleadores / Estado",
        "peso": 5,
    },
    "Jubilaciones / Pensiones": {
        "keywords": [
            "movilidad jubilatoria", "haber minimo", "anses",
            "jubilacion", "pension", "prestacion basica universal",
            "formula de movilidad", "actualizacion previsional",
            "pami", "retiro",
        ],
        "transferencia": "Jubilados → Estado",
        "peso": 10,  # peso máximo — mayor impacto social
    },
    "Traslado de Impuestos": {
        "keywords": [
            "impuesto", "tributo", "alicuota", "iva", "ganancias",
            "ingresos brutos", "derechos de exportacion", "retencion",
            "percepcion impositiva", "carga fiscal",
        ],
        "transferencia": "Contribuyentes → Estado / Empresas",
        "peso": 9,
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# FUNCIONES DE CLASIFICACIÓN
# ─────────────────────────────────────────────────────────────────────────────

def _normalizar(texto: str) -> str:
    """Elimina tildes y pasa a minúsculas para matching robusto."""
    texto = texto.lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto).strip()


def clasificar(texto: str) -> dict:
    """
    Clasifica un texto según la Matriz XAI Monteverde.

    Retorna dict con:
        tipo_decision     : escenario teórico matched (o "Sin clasificar")
        transferencia     : dirección del flujo económico
        peso              : índice 0-10
        nivel_riesgo      : Alto / Medio / Bajo / Sin riesgo
        keywords_matched  : lista de palabras que activaron la clasificación
    """
    texto_norm = _normalizar(str(texto))

    mejor_escenario = None
    mejor_peso = 0
    mejor_transferencia = "—"
    keywords_encontradas = []

    for escenario, datos in MATRIZ_TEORICA.items():
        matches = [kw for kw in datos["keywords"] if kw in texto_norm]
        if matches:
            if datos["peso"] > mejor_peso:
                mejor_escenario = escenario
                mejor_peso = datos["peso"]
                mejor_transferencia = datos["transferencia"]
                keywords_encontradas = matches

    if mejor_escenario is None:
        return {
            "tipo_decision": "Sin clasificar",
            "transferencia": "—",
            "peso": 0,
            "nivel_riesgo": "Sin riesgo",
            "keywords_matched": [],
        }

    if mejor_peso >= 8:
        nivel = "Alto"
    elif mejor_peso >= 5:
        nivel = "Medio"
    else:
        nivel = "Bajo"

    return {
        "tipo_decision": mejor_escenario,
        "transferencia": mejor_transferencia,
        "peso": mejor_peso,
        "nivel_riesgo": nivel,
        "keywords_matched": keywords_encontradas,
    }


def aplicar_matriz(df: pd.DataFrame, col_texto: str = "detalle") -> pd.DataFrame:
    """
    Aplica clasificar() fila a fila sobre la columna col_texto.
    Agrega columnas: tipo_decision, transferencia, peso, nivel_riesgo.
    """
    if col_texto not in df.columns:
        raise ValueError(f"Columna '{col_texto}' no encontrada en el DataFrame.")

    resultados = df[col_texto].fillna("").apply(clasificar)
    df = df.copy()
    df["tipo_decision"] = resultados.apply(lambda r: r["tipo_decision"])
    df["transferencia"] = resultados.apply(lambda r: r["transferencia"])
    df["indice_fenomeno"] = resultados.apply(lambda r: r["peso"])
    df["nivel_riesgo"] = resultados.apply(lambda r: r["nivel_riesgo"])
    return df


def calcular_hhi(serie_montos: pd.Series) -> float:
    """
    Índice Herfindahl-Hirschman de concentración.
    Rango 0-10000. >2500 = alta concentración.
    """
    total = serie_montos.sum()
    if total == 0:
        return 0.0
    participaciones = serie_montos / total * 100
    return float((participaciones ** 2).sum())


def interpretar_hhi(hhi: float) -> str:
    if hhi >= 2500:
        return "Alta concentración — riesgo de captura"
    elif hhi >= 1500:
        return "Concentración moderada"
    else:
        return "Mercado competitivo"