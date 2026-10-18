"""
tools/reextraer_avisos.py
=========================
Vuelve a leer del BORA los avisos de adjudicación que quedaron sin CUIT, sin
proveedor o sin monto, y guarda lo recuperado en
data/correcciones/avisos_reextraidos.csv. No modifica ningún reporte Excel.

main.py lee ese CSV al cargar los datos y completa SOLO las celdas vacías
(ver _aplicar_correcciones), así que nunca pisa un dato que vino del aviso.

Uso:
    python tools/reextraer_avisos.py                 # todos los avisos incompletos
    python tools/reextraer_avisos.py --desde 2026-09-01
    python tools/reextraer_avisos.py --limite 20     # prueba rápida

Es reanudable: los avisos ya revisados (estén o no en el CSV) se anotan en
data/correcciones/.revisados.txt y no se vuelven a pedir.
"""
import argparse
import os
import re
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)

import pandas as pd  # noqa: E402

import diario  # noqa: E402
import main  # noqa: E402

SALIDA = os.path.join(main.CORRECCIONES_DIR, "avisos_reextraidos.csv")
REVISADOS = os.path.join(main.CORRECCIONES_DIR, ".revisados.txt")
CAMPOS = ["cuit_proveedor", "proveedor_adjudicado", "monto_adjudicado_bora"]


def _incompletos(desde=None):
    df = main._cargar_df()
    falta = pd.Series(False, index=df.index)
    for c in CAMPOS:
        if c in df.columns:
            falta |= df[c].astype(str).str.strip() == ""
    if "cancelada" in df.columns:
        falta &= ~df["cancelada"].astype(str).str.lower().isin(["true", "1"])
    df = df[falta & df["link_bora"].astype(str).str.contains("/detalleAviso/tercera/")]
    if desde:
        df = df[df["fecha"] >= pd.to_datetime(desde)]
    return df


def main_cli():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--desde")
    p.add_argument("--limite", type=int, default=0)
    p.add_argument("--pausa", type=float, default=1.0, help="segundos entre avisos (cortesía con el BORA)")
    a = p.parse_args()

    os.makedirs(main.CORRECCIONES_DIR, exist_ok=True)
    revisados = set(open(REVISADOS, encoding="utf-8").read().split()) if os.path.exists(REVISADOS) else set()
    previos = pd.read_csv(SALIDA, dtype=str).fillna("") if os.path.exists(SALIDA) else pd.DataFrame(columns=["link_bora"] + CAMPOS)

    df = _incompletos(a.desde)
    pendientes = [r for _, r in df.iterrows() if r["link_bora"] not in revisados]
    if a.limite:
        pendientes = pendientes[:a.limite]
    print(f"📋 {len(df)} avisos incompletos · {len(pendientes)} por revisar")

    nuevos, recuperados = [], {c: 0 for c in CAMPOS}
    for i, r in enumerate(pendientes, 1):
        m = re.search(r"/detalleAviso/tercera/(\d+)/(\d{8})", r["link_bora"])
        if not m:
            continue
        aviso_id, f = m.group(1), m.group(2)
        texto = diario._texto_aviso_bora(aviso_id, f"{f[:4]}-{f[4:6]}-{f[6:]}")
        fila = {"link_bora": r["link_bora"],
                "cuit_proveedor": diario._normalizar_cuit(texto) if texto else "",
                "proveedor_adjudicado": diario._extraer_proveedor(texto) if texto else "",
                "monto_adjudicado_bora": diario._extraer_monto_bora(texto) if texto else ""}
        # solo cuenta lo que completa una celda hoy vacía
        aporta = {c: fila[c] for c in CAMPOS if fila[c] and not str(r.get(c, "")).strip()}
        if aporta:
            nuevos.append({"link_bora": r["link_bora"], **{c: aporta.get(c, "") for c in CAMPOS}})
            for c in aporta:
                recuperados[c] += 1
        # Solo se marca como revisado si el BORA devolvió el texto: si no respondió,
        # la próxima corrida (por ejemplo, la del cron del día siguiente) lo reintenta.
        if texto:
            with open(REVISADOS, "a", encoding="utf-8") as fh:
                fh.write(r["link_bora"] + "\n")
        if i % 25 == 0:
            print(f"  … {i}/{len(pendientes)} · recuperados: {recuperados}")
            _guardar(previos, nuevos)
        time.sleep(a.pausa)

    _guardar(previos, nuevos)
    print(f"✅ Listo. Recuperados: {recuperados} → {os.path.relpath(SALIDA, RAIZ)}")


def _guardar(previos, nuevos):
    if not nuevos and previos.empty:
        return
    todo = pd.concat([previos, pd.DataFrame(nuevos)], ignore_index=True)
    todo = todo.drop_duplicates(subset=["link_bora"], keep="last")
    todo.to_csv(SALIDA, index=False, encoding="utf-8")


if __name__ == "__main__":
    main_cli()
