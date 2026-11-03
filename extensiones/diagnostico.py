"""
GET /api/diagnostico — de un vistazo, qué dependencias externas responden
(revisión del 03/11/2026).

Motivo: dos fallas pasaron días sin que nada las mostrara. El BORA dejó de
responder a servidores fuera de Argentina (22/10/2026) y el cron siguió en verde;
y el cruce con MEACI apuntaba a una dirección de Railway dada de baja, así que
todos los CUIT salían "sin coincidencias". `?forzar=true` ignora la caché.
"""
import os
import threading
import time
from datetime import datetime

import requests
from fastapi import APIRouter

BORA_URL_PRUEBA = os.getenv("BORA_URL_PRUEBA", "https://www.boletinoficial.gob.ar/")
TIMEOUT_SEG = float(os.getenv("DIAGNOSTICO_TIMEOUT_SEG", "8"))
CACHE_SEG = int(os.getenv("DIAGNOSTICO_CACHE_SEG", "300"))
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "es-AR,es;q=0.9"}

router = APIRouter()
_lock = threading.Lock()
_cache = {}


def _cacheado(clave, calcular, forzar=False):
    with _lock:
        previo = _cache.get(clave)
        if previo and not forzar and time.monotonic() - previo[0] < CACHE_SEG:
            return previo[1]
    valor = dict(calcular(), verificado_en=datetime.now().isoformat(timespec="seconds"))
    with _lock:
        _cache[clave] = (time.monotonic(), valor)
    return valor


def probar_bora() -> dict:
    t0 = time.monotonic()
    try:
        r = requests.get(BORA_URL_PRUEBA, headers=_HEADERS, verify=False,
                         timeout=(TIMEOUT_SEG, TIMEOUT_SEG * 2))
        return {"alcanzable": r.status_code < 500, "http": r.status_code,
                "segundos": round(time.monotonic() - t0, 2)}
    except Exception as e:
        return {"alcanzable": False, "http": None, "segundos": round(time.monotonic() - t0, 2),
                "error": type(e).__name__}


def probar_meaci(url: str) -> dict:
    """MEACI responde JSON en /api/meaci-stats; una dirección dada de baja devuelve 404 sin JSON."""
    try:
        r = requests.get(f"{url}/api/meaci-stats", timeout=TIMEOUT_SEG)
        ok = r.status_code == 200 and "json" in r.headers.get("content-type", "")
        return {"alcanzable": ok, "http": r.status_code, "url": url}
    except Exception as e:
        return {"alcanzable": False, "http": None, "url": url, "error": type(e).__name__}


@router.get("/api/diagnostico")
def diagnostico(forzar: bool = False):
    import main as m
    from agentic_ai import MODEL, ia_disponible
    archivos = m._buscar_xlsx()
    return {
        "bora": _cacheado("bora", probar_bora, forzar),
        "meaci": _cacheado("meaci", lambda: probar_meaci(m.MEACI_URL), forzar),
        "ia": {"configurada": ia_disponible(), "modelo": MODEL},
        "ultimo_reporte": os.path.basename(archivos[0]) if archivos else None,
        "reportes": len(archivos),
        "cache_cargado": m._cache["timestamp"],
    }
