"""
tools/actualizar_desde_pc.py
============================
Genera los reportes diarios que faltan corriendo el mismo ciclo que el cron
(diario.main), día por día, desde una conexión de Argentina; después regenera el
análisis de concentración y, con --publicar, hace commit de data/ y push a
`main` (igual que el workflow). No modifica diario.py ni ningún reporte existente.

Por qué: desde el 22/10/2026 el BORA no responde a las IP de GitHub Actions
(ConnectTimeout en los tres intentos). El workflow igual terminaba en verde y
commiteaba solo el análisis de concentración regenerado sobre datos viejos, así
que el último reporte quedó en el 21/10. Desde Argentina el BORA responde en 1-3 s.

Uso (desde la raíz del repo, en un checkout de `main`):

    python tools/actualizar_desde_pc.py --dry-run      # qué días faltan
    python tools/actualizar_desde_pc.py                # genera los que faltan
    python tools/actualizar_desde_pc.py --publicar     # genera y hace push a main

Comprar y TGN se bajan una sola vez por corrida (su estado de hoy, como hace el
cron cada mañana). TGN necesita TGN_TOKEN; sin él esa hoja queda vacía.
"""
import argparse
import contextlib
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)
for _flujo in (sys.stdout, sys.stderr):  # consola de Windows (cp1252): diario.py imprime emojis
    with contextlib.suppress(AttributeError):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

import diario  # noqa: E402
from analisis import aplicar_matriz  # noqa: E402

DESDE_POR_DEFECTO = "2026-10-21"  # último día con reporte del cron antes del bloqueo
RAMA = "main"
TZ_AR = timezone(timedelta(hours=-3))


def hoy_ar() -> date:
    return datetime.now(TZ_AR).date()


def ruta_reporte(d: date) -> str:
    return os.path.join(diario.DATA_DIR, d.strftime("%Y-%m"), f"reporte_{d.isoformat()}.xlsx")


def dias_faltantes(desde: date, hasta: date) -> list:
    try:
        import holidays
        feriados = holidays.Argentina(years=range(desde.year, hasta.year + 1))
    except ImportError:
        feriados = {}
    faltan, d = [], desde
    while d <= hasta:
        if d.weekday() < 5 and d not in feriados and not os.path.exists(ruta_reporte(d)):
            faltan.append(d)
        d += timedelta(days=1)
    return faltan


def generar_dia(d: date, actuales: dict) -> dict:
    """Los mismos pasos que diario.main(), para la fecha d."""
    df_adj, df_licit = diario.extraer_bora(d)
    if df_adj.empty and df_licit.empty:
        return {"fecha": d.isoformat(), "estado": "sin edición del BORA (o no respondió)"}
    if "comprar" not in actuales:
        actuales["comprar"] = diario.extraer_comprar(d.year)
        actuales["tgn"] = diario.extraer_tgn(d.year)
    df_comprar, df_tgn = actuales["comprar"].copy(), actuales["tgn"].copy()
    if not df_adj.empty:
        df_adj = aplicar_matriz(df_adj, col_texto="texto_muestra")
    df_cruce = diario.cruzar_fuentes(df_adj, df_comprar, df_tgn)
    if not df_cruce.empty:
        df_cruce = diario._agregar_riesgo(df_cruce)
    diario.guardar_excels(df_cruce, df_adj, df_licit, df_comprar, df_tgn, d)
    return {"fecha": d.isoformat(), "estado": "ok", "adjudicaciones": len(df_adj),
            "licitaciones": len(df_licit), "comprar": len(df_comprar), "tgn": len(df_tgn),
            "cruce": len(df_cruce)}


def _correr(*cmd, check=True):
    print("   $ " + " ".join(cmd))
    entorno = dict(os.environ, PYTHONIOENCODING="utf-8")  # los scripts imprimen emojis
    return subprocess.run(list(cmd), cwd=RAIZ, check=check, text=True, capture_output=True,
                          encoding="utf-8", errors="replace", env=entorno)


def publicar(fechas: list):
    _correr("git", "add", "data/")
    if _correr("git", "diff", "--staged", "--quiet", check=False).returncode == 0:
        print("Sin cambios en data/, nada que publicar.")
        return
    rango = fechas[0] if len(fechas) == 1 else f"{fechas[0]} a {fechas[-1]}"
    _correr("git", "commit", "-m", f"Monitor diario {rango} — BORA+Comprar+TGN+Concentración (desde PC en Argentina)")
    _correr("git", "pull", "--rebase", "--autostash", "origin", RAMA)
    _correr("git", "push", "origin", f"HEAD:{RAMA}")
    print(f"✅ Publicado en {RAMA}.")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--desde", default=DESDE_POR_DEFECTO)
    p.add_argument("--hasta", default=None, help="por defecto, hoy (hora de Argentina)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--publicar", action="store_true", help=f"commit de data/ y push a {RAMA}")
    a = p.parse_args(argv)

    if a.publicar and not a.dry_run:
        _correr("git", "pull", "--rebase", "--autostash", "origin", RAMA)
    desde = date.fromisoformat(a.desde)
    hasta = date.fromisoformat(a.hasta) if a.hasta else hoy_ar()
    faltan = dias_faltantes(desde, hasta)
    print(f"📅 Días hábiles sin reporte entre {desde} y {hasta}: {[d.isoformat() for d in faltan] or 'ninguno'}")
    if a.dry_run or not faltan:
        return 0

    actuales, resultados = {}, []
    for d in faltan:
        print(f"\n════════ {d} ════════", flush=True)
        resultados.append(generar_dia(d, actuales))

    print("\n📊 RESUMEN")
    for r in resultados:
        print("  ", r)
    generados = [r["fecha"] for r in resultados if r["estado"] == "ok"]
    if generados:
        print("\n🔬 Análisis de concentración…", flush=True)
        r = _correr(sys.executable, "-W", "ignore", "analisis_concentracion.py", "--exportar", check=False)
        print("   OK" if r.returncode == 0 else f"   ⚠️ falló (código {r.returncode}): {r.stderr[-500:]}")
    if a.publicar and generados:
        publicar(generados)
    elif generados:
        print(f"\nGenerados {len(generados)} reportes. Para publicarlos: --publicar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
