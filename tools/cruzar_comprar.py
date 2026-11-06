"""
tools/cruzar_comprar.py
=======================
Vuelve a cruzar TODOS los avisos de los reportes (data/AAAA-MM/reporte_*.xlsx)
con las adjudicaciones COMPR.AR 2016-2026 de datos.gob.ar (ver cruce_comprar.py)
y guarda el resultado en data/cruce/comprar.csv, una fila por aviso (link_bora).

No modifica los reportes: el sitio (main.py) aplica este archivo al cargar,
igual que hace con data/correcciones/. Así un aviso que hoy figura "⏳ COMPR.AR
publicado hasta ..." pasa a "✅ SÍ" cuando datos.gob.ar publique el período.

Uso:
    python tools/cruzar_comprar.py            # baja (si cambió) y cruza
    python tools/cruzar_comprar.py --csv ruta  # usa un CSV local de COMPR.AR
"""
import argparse
import contextlib
import glob
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
for _flujo in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError):
        _flujo.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd  # noqa: E402

import cruce_comprar as cc  # noqa: E402

DATA_DIR = os.path.join(RAIZ, "data")
SALIDA = os.path.join(DATA_DIR, "cruce", "comprar.csv")
HOJAS = ["🚨 Flujo Completo", "🔗 Flujo Cruzado"]
COLUMNAS = ["link_bora", "en_comprar", "nro_proceso_comprar", "tipo_procedimiento", "modalidad",
            "monto_comprar", "rubros", "unidad_operativa", "proveedor_comprar", "cuit_comprar", "comprar_corte"]


def avisos() -> pd.DataFrame:
    dfs = []
    for archivo in sorted(glob.glob(os.path.join(DATA_DIR, "**", "reporte_*.xlsx"), recursive=True)):
        try:
            xl = pd.ExcelFile(archivo)
            hoja = next((h for h in HOJAS if h in xl.sheet_names), None)
            if not hoja:
                continue
            df = xl.parse(hoja, dtype=str).fillna("")
        except Exception as e:
            print(f"⚠️ {os.path.basename(archivo)}: {e}")
            continue
        cols = [c for c in ("link_bora", "tipo_proceso_bora", "organismo_contratante",
                            "fecha_publicacion", "fecha") if c in df.columns]
        dfs.append(df[cols])
    if not dfs:
        return pd.DataFrame()
    df = pd.concat(dfs, ignore_index=True)
    if "fecha_publicacion" not in df:
        df["fecha_publicacion"] = ""
    if "fecha" in df:
        df["fecha_publicacion"] = df["fecha_publicacion"].where(df["fecha_publicacion"] != "", df["fecha"])
    df = df[df["link_bora"].astype(str).str.strip() != ""]
    return df.drop_duplicates("link_bora", keep="last")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", help="CSV local de adjudicaciones COMPR.AR (si no, se baja con caché)")
    a = p.parse_args(argv)
    ruta = a.csv or cc.descargar()
    if not ruta:
        print("⚠️ Sin archivo de COMPR.AR: no se actualiza el cruce.")
        return 0
    indice = cc.IndiceComprar.desde_csv(ruta)
    df = avisos()
    if df.empty:
        print("⚠️ No hay avisos en los reportes.")
        return 0
    corte = indice.corte.isoformat() if indice.corte else ""
    filas = []
    for _, r in df.iterrows():
        res = indice.cruzar(r.get("tipo_proceso_bora", ""), r.get("organismo_contratante", ""), r.get("fecha_publicacion"))
        filas.append({"link_bora": r["link_bora"], **res, "comprar_corte": corte})
    out = pd.DataFrame(filas).reindex(columns=COLUMNAS).fillna("")
    os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
    out.sort_values("link_bora").to_csv(SALIDA, index=False, encoding="utf-8")
    resumen = out["en_comprar"].str.replace(r"publicado hasta .*", "publicado hasta …", regex=True).value_counts()
    print(f"✅ Cruce COMPR.AR (datos hasta {corte}): {len(out)} avisos → {SALIDA}")
    for estado, n in resumen.items():
        print(f"   {estado}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
