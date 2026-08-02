"""
agentic_ai.py — Prototipo de Agentic AI para Monitor de Contratos.

Usa la API de Anthropic (Claude) para generar explicaciones narrativas en
lenguaje natural sobre los perfiles de organismos/proveedores y las alertas
sistémicas (HHI, fragmentación, proveedor único, fantasmas) que ya calcula
el resto del sistema (main.py, analisis_concentracion.py).

Degradación elegante: si no está configurada ANTHROPIC_API_KEY, o falla la
librería `anthropic`, todas las funciones devuelven
{"disponible": False, "motivo": "..."} en vez de romper el endpoint.
"""

import os

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

try:
    import anthropic
    _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
except ImportError:
    anthropic = None
    _client = None


def ia_disponible() -> bool:
    """True si hay librería `anthropic` instalada Y ANTHROPIC_API_KEY configurada."""
    return _client is not None


def _no_disponible(motivo: str) -> dict:
    return {"disponible": False, "motivo": motivo}


def _pedir_a_claude(system: str, prompt: str, max_tokens: int = 500) -> dict:
    if not ia_disponible():
        if anthropic is None:
            return _no_disponible(
                "La librería 'anthropic' no está instalada en este entorno."
            )
        return _no_disponible(
            "ANTHROPIC_API_KEY no está configurada — el asistente de IA está deshabilitado."
        )
    try:
        resp = _client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = "".join(
            bloque.text for bloque in resp.content if getattr(bloque, "type", "") == "text"
        )
        return {"disponible": True, "explicacion": texto.strip()}
    except Exception as e:
        return _no_disponible(f"Error al consultar la IA: {e}")


_SYSTEM_BASE = (
    "Sos un analista de integridad pública que explica, en español rioplatense claro "
    "y sin tecnicismos innecesarios, indicadores de riesgo de contrataciones estatales "
    "argentinas (BORA, Comprar.gob.ar, TGN) según la metodología de Fenómenos "
    "Corruptivos del Ph.D. Vicente Monteverde. No acusás a nadie de haber cometido un "
    "delito: describís qué significa el patrón detectado, por qué es una señal de "
    "riesgo (no una prueba), y qué preguntas de auditoría o control ciudadano "
    "ayudarían a confirmarlo o descartarlo. Sé concreto y breve (4-8 líneas)."
)


def explicar_perfil_organismo(perfil: dict) -> dict:
    """
    Genera una explicación narrativa del perfil de un organismo contratante,
    a partir de los datos ya calculados por GET /api/organismos/{nombre}
    (total_contratos, monto_total, proveedores_unicos, hhi, hhi_interpretacion,
    red_flags, top_proveedores).
    """
    hhi = perfil.get("hhi", 0)
    prompt = f"""Perfil del organismo contratante "{perfil.get('organismo', perfil.get('nombre', ''))}":

- Contratos totales: {perfil.get('total_contratos', '—')}
- Monto total adjudicado: {perfil.get('monto_total', '—')}
- Proveedores únicos: {perfil.get('proveedores_unicos', '—')}
- Índice HHI de concentración: {hhi} ({perfil.get('hhi_interpretacion', '—')})
- Red flags detectadas: {perfil.get('red_flags') or 'ninguna'}
- Top proveedores: {perfil.get('top_proveedores', [])[:5]}

Explicá qué indica este perfil en términos de riesgo de captura o concentración,
y qué habría que auditar primero."""
    return _pedir_a_claude(_SYSTEM_BASE, prompt)


def explicar_perfil_proveedor(perfil: dict) -> dict:
    """
    Genera una explicación narrativa del perfil de un proveedor,
    a partir de los datos de GET /api/proveedores/{cuit} (total_contratos,
    monto_adjudicado, monto_cobrado_tgn, organismos_unicos, red_flags,
    top_organismos).
    """
    prompt = f"""Perfil del proveedor "{perfil.get('nombre', '')}" (CUIT {perfil.get('cuit', '—')}):

- Contratos totales: {perfil.get('total_contratos', '—')}
- Monto adjudicado: {perfil.get('monto_adjudicado', '—')}
- Monto efectivamente cobrado en TGN: {perfil.get('monto_cobrado_tgn', '—')}
- Organismos contratantes distintos: {perfil.get('organismos_unicos', '—')}
- Red flags detectadas: {perfil.get('red_flags') or 'ninguna'}
- Top organismos: {perfil.get('top_organismos', [])[:5]}

Explicá qué indica este perfil (por ejemplo: concentración en pocos organismos,
brecha entre adjudicado y cobrado, etc.) y qué habría que verificar primero."""
    return _pedir_a_claude(_SYSTEM_BASE, prompt)


_ETIQUETAS_MONITOR = {
    "fragmentacion":   "Fragmentación de contrataciones (posible división para evitar licitación pública)",
    "unico":           "Proveedor único (el organismo adjudica siempre al mismo CUIT)",
    "hhi":             "Concentración de mercado (Índice HHI)",
    "fantasmas":       "Adjudicado sin cobro registrado en TGN (posible proveedor 'fantasma')",
}


def explicar_alerta_monitor(tipo: str, fila: dict) -> dict:
    """
    Explica una fila puntual de alguna de las 4 pestañas de /api/monitor
    (fragmentación, proveedor único, HHI, fantasmas).

    tipo : una de "fragmentacion" | "unico" | "hhi" | "fantasmas"
    fila : el dict de la fila tal cual lo devuelve /api/monitor
    """
    etiqueta = _ETIQUETAS_MONITOR.get(tipo, tipo)
    prompt = f"""Tipo de alerta sistémica: {etiqueta}

Datos de la fila detectada:
{fila}

Explicá en qué consiste esta alerta específica, por qué constituye una señal
de riesgo según la metodología Monteverde, y qué pregunta concreta de
auditoría permitiría confirmar o descartar el riesgo."""
    return _pedir_a_claude(_SYSTEM_BASE, prompt)
