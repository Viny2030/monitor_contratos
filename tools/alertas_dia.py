"""
tools/alertas_dia.py
====================
Arma el aviso por email del cron: adjudicaciones de riesgo Alto del reporte del
día y CUITs con sanciones internacionales (MEACI). Si no hay nada que avisar no
genera mail: así el correo deja de llegar todos los días con "OK" y llega solo
cuando hay algo para mirar.

Uso (lo llama .github/workflows/ejecucion_diaria.yml):
    python tools/alertas_dia.py [--fecha AAAA-MM-DD] [--salida alertas.txt]

Escribe en $GITHUB_OUTPUT  hay_alertas=true|false  y el cuerpo del mail en --salida.
"""
import argparse
import glob
import os
import sys
from datetime import date

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
os.chdir(RAIZ)

import pandas as pd  # noqa: E402

SITIO = "https://monitorcontratos-production.up.railway.app"


def _meaci(cuits):
    if not cuits:
        return {}
    try:
        import requests
        url = os.getenv("MEACI_URL", "https://meaci-production.up.railway.app").rstrip("/")
        r = requests.get(f"{url}/api/cruce-cuits-bulk", params={"cuits": ",".join(cuits[:50])}, timeout=20)
        return r.json().get("alertas", {}) if r.status_code == 200 else {}
    except Exception as e:
        print(f"⚠️ MEACI no respondió: {e}")
        return {}


def armar(fecha: str) -> tuple[bool, str]:
    rutas = glob.glob(os.path.join("data", fecha[:7], f"reporte_{fecha}.xlsx"))
    if not rutas:
        return False, ""
    xl = pd.ExcelFile(rutas[0])
    hoja = next((h for h in ["🚨 Flujo Completo", "🔗 Flujo Cruzado"] if h in xl.sheet_names), None)
    if not hoja:
        return False, ""
    df = xl.parse(hoja, dtype=str).fillna("")
    from diario import _agregar_riesgo
    base = df.copy()
    if "cancelada" in base.columns:
        base["cancelada"] = base["cancelada"].str.strip().str.lower().isin(["true", "1"])
    df[["indicadores_riesgo", "nivel_riesgo_licit"]] = _agregar_riesgo(base)[["indicadores_riesgo", "nivel_riesgo_licit"]].values

    altos = df[df["nivel_riesgo_licit"] == "Alto"]
    cuits = sorted({c for c in df.get("cuit_proveedor", pd.Series(dtype=str)) if c.strip()})
    sancionados = _meaci(cuits)

    if altos.empty and not sancionados:
        return False, ""

    lineas = [f"Monitor de Contratos — alertas del {fecha}", ""]
    if not altos.empty:
        lineas.append(f"🔴 {len(altos)} adjudicación(es) de riesgo ALTO:")
        for _, r in altos.iterrows():
            lineas += [f"  • {r.get('organismo_contratante', '')}",
                       f"    {r.get('tipo_proceso_bora', '')} · {r.get('proveedor_adjudicado', '') or 'proveedor sin leer'}"
                       f" · CUIT {r.get('cuit_proveedor', '') or '—'} · {r.get('monto_adjudicado_bora', '') or 'sin monto'}",
                       f"    {r.get('indicadores_riesgo', '')}",
                       f"    {r.get('link_bora', '')}"]
        lineas.append("")
    if sancionados:
        lineas.append(f"🌍 {len(sancionados)} CUIT con coincidencias en sanciones internacionales (MEACI):")
        for cuit in sancionados:
            fila = df[df["cuit_proveedor"] == cuit].head(1)
            nombre = fila["proveedor_adjudicado"].iloc[0] if not fila.empty else ""
            lineas.append(f"  • {cuit} {nombre} — {SITIO}/?proveedor={cuit}")
        lineas.append("")
    lineas += [f"Ver el día completo: {SITIO}/?seccion=contratos",
               "Son indicadores algorítmicos de riesgo, no acusaciones: revisar el aviso original."]
    return True, "\n".join(lineas)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fecha", default=date.today().isoformat())
    p.add_argument("--salida", default="alertas.txt")
    a = p.parse_args()
    hay, cuerpo = armar(a.fecha)
    if hay:
        with open(a.salida, "w", encoding="utf-8") as f:
            f.write(cuerpo)
        print(cuerpo)
    else:
        print(f"Sin alertas para el {a.fecha}")
    salida_gh = os.getenv("GITHUB_OUTPUT")
    if salida_gh:
        with open(salida_gh, "a", encoding="utf-8") as f:
            f.write(f"hay_alertas={'true' if hay else 'false'}\n")


if __name__ == "__main__":
    main()
