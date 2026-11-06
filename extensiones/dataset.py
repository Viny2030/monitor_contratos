"""
Carga del dataset publicado en GitHub (revisión del 03/11/2026).

Desde el 22/10/2026 el BORA no responde a servidores fuera de Argentina, así que
ni este servidor ni GitHub Actions pueden generar reportes: los genera la PC en
Argentina (tools/actualizar_desde_pc.py, workflow ejecucion_diaria_argentina.yml)
y los publica en la rama `main`. Este módulo trae esos archivos al disco del
servidor sin esperar un redeploy, y limpia la caché para que el tablero los use.

  POST /api/datos/actualizar   Baja de GitHub lo que falte o haya cambiado
                               (1 consulta a la API + 1 descarga por archivo)
  GET  /api/datos/estado       Última sincronización
"""
import hashlib
import os
import re
import shutil
import threading
import time
from datetime import datetime

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

GITHUB_REPO = os.getenv("DATASET_REPO", "Viny2030/monitor_contratos")
GITHUB_RAMA = os.getenv("DATASET_RAMA", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")  # opcional: sin token, 60 consultas/hora por IP
SINCRONIZAR_ESPERA_SEG = int(os.getenv("SINCRONIZAR_ESPERA_SEG", "120"))

# Lo que el tablero lee: reportes diarios, concentración, correcciones de avisos y el cruce COMPR.AR.
_RUTA_DATASET = re.compile(
    r"^data/("
    r"\d{4}-\d{2}/(?:reporte|flujo_licitaciones)_\d{4}-\d{2}-\d{2}\.xlsx"
    r"|analisis/concentracion_\d{4}-\d{2}-\d{2}\.xlsx"
    r"|correcciones/[\w.-]+\.csv"
    r"|cruce/[\w.-]+\.csv"
    r")$"
)

router = APIRouter()
_lock = threading.Lock()
_estado = {"corriendo": False, "ultima": None, "_t": 0.0}


def _main():
    import main
    return main


def sha_blob(contenido: bytes) -> str:
    """El sha que git asigna al archivo: permite saber si la copia local ya es la publicada."""
    return hashlib.sha1(b"blob %d\0" % len(contenido) + contenido).hexdigest()


def respaldos_dir() -> str:
    return os.getenv("RESPALDOS_DIR", "/app/respaldos" if os.path.exists("/app") else "respaldos")


def listar_publicados() -> list:
    """[{'relativa', 'sha'}] de los archivos del dataset en la rama publicada."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "monitor-contratos-dataset"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/{GITHUB_RAMA}?recursive=1",
                     headers=headers, timeout=20)
    if r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0":
        raise RuntimeError("GitHub limitó las consultas de este servidor por una hora: "
                           "configurar GITHUB_TOKEN o probar más tarde.")
    r.raise_for_status()
    return [{"relativa": m.group(1), "sha": item["sha"]}
            for item in r.json().get("tree", [])
            if item.get("type") == "blob" and (m := _RUTA_DATASET.match(item.get("path", "")))]


def _descargar(relativa: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_RAMA}/data/{relativa}"
    r = requests.get(url, headers={"User-Agent": "monitor-contratos-dataset"}, timeout=60)
    r.raise_for_status()
    return r.content


def _respaldar(ruta: str, relativa: str):
    destino = os.path.join(respaldos_dir(), os.path.dirname(relativa))
    os.makedirs(destino, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(ruta))
    shutil.copy2(ruta, os.path.join(destino, f"{base}__{datetime.now():%Y%m%dT%H%M%S}{ext}"))


def sincronizar(data_dir: str) -> dict:
    """Baja los archivos que falten o difieran de los publicados. No borra nada local."""
    nuevos, actualizados, errores = [], [], []
    publicados = listar_publicados()
    for p in publicados:
        destino = os.path.join(data_dir, *p["relativa"].split("/"))
        existia = os.path.exists(destino)
        if existia:
            with open(destino, "rb") as f:
                if sha_blob(f.read()) == p["sha"]:
                    continue
        try:
            contenido = _descargar(p["relativa"])
            if sha_blob(contenido) != p["sha"]:
                raise ValueError("el archivo bajado no coincide con el publicado")
            if existia:
                _respaldar(destino, p["relativa"])
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with open(destino + ".tmp", "wb") as f:
                f.write(contenido)
            os.replace(destino + ".tmp", destino)
            (actualizados if existia else nuevos).append(p["relativa"])
        except Exception as e:
            errores.append({"archivo": p["relativa"], "error": str(e)[:200]})
    reportes = sorted(re.search(r"reporte_(\d{4}-\d{2}-\d{2})", p["relativa"]).group(1)
                      for p in publicados if "/reporte_" in p["relativa"])
    return {"publicados": len(publicados), "nuevos": nuevos, "actualizados": actualizados,
            "errores": errores, "ultimo_dia_publicado": reportes[-1] if reportes else None}


@router.post("/api/datos/actualizar")
def actualizar_datos():
    with _lock:
        if _estado["corriendo"]:
            return JSONResponse(status_code=409, content={"detail": "Ya hay una actualización en curso."})
        if _estado["ultima"] and time.monotonic() - _estado["_t"] < SINCRONIZAR_ESPERA_SEG:
            return dict(_estado["ultima"], reciente=True)  # no se vuelve a consultar GitHub enseguida
        _estado["corriendo"] = True
    try:
        m = _main()
        resultado = sincronizar(m.DATA_DIR)
        if resultado["nuevos"] or resultado["actualizados"]:
            m._invalidar_cache()
        archivos = m._buscar_xlsx()
        resultado.update({"status": "ok",
                          "ultimo_reporte": os.path.basename(archivos[0]) if archivos else None,
                          "actualizado_en": datetime.now().isoformat(timespec="seconds")})
        with _lock:
            _estado.update({"ultima": resultado, "_t": time.monotonic()})
        return resultado
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo leer el dataset publicado en GitHub: {e}")
    finally:
        with _lock:
            _estado["corriendo"] = False


@router.get("/api/datos/estado")
def estado_datos():
    with _lock:
        return {"repositorio": f"{GITHUB_REPO}@{GITHUB_RAMA}", "corriendo": _estado["corriendo"],
                "ultima": _estado["ultima"]}
