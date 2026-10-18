"""
db.py — Conexión y operaciones PostgreSQL
Solo para tracking de visitas y donaciones.
Los datos de contratos viven en Excel (data/).
"""

import os
import logging

import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger(__name__)

_DATABASE_URL = os.getenv("DATABASE_URL", "")


def _get_conn():
    url = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def init_db():
    """Crea las tablas si no existen. Se llama una vez al arrancar."""
    if not _DATABASE_URL:
        log.info("DATABASE_URL no configurada — tracking desactivado")
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS visitas (
                    id         SERIAL PRIMARY KEY,
                    seccion    VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_visitas_ts ON visitas (created_at);

                CREATE TABLE IF NOT EXISTS donaciones (
                    id         SERIAL PRIMARY KEY,
                    nombre     VARCHAR(100),
                    apellido   VARCHAR(100),
                    email      VARCHAR(254),
                    pais       VARCHAR(30),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_donaciones_ts ON donaciones (created_at);
            """)
            conn.commit()
        conn.close()
        log.info("DB inicializada OK")
    except Exception as e:
        log.warning(f"DB init error: {e}")


def registrar_visita(seccion: str):
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO visitas (seccion) VALUES (%s)", (seccion,))
            conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"tracking error: {e}")


def registrar_donacion(nombre: str, apellido: str, email: str, pais: str) -> int | None:
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO donaciones (nombre, apellido, email, pais) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (nombre.strip(), apellido.strip(), email.strip(), pais),
            )
            row = cur.fetchone()
            conn.commit()
        conn.close()
        return row["id"]
    except Exception as e:
        log.warning(f"donacion error: {e}")
        return None


def get_stats() -> dict:
    """Estadísticas para el endpoint /api/stats."""
    if not _DATABASE_URL:
        return {"error": "DB no configurada"}
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM visitas;")
            total = cur.fetchone()["total"]

            cur.execute(
                "SELECT seccion, COUNT(*) AS v FROM visitas "
                "GROUP BY seccion ORDER BY v DESC;"
            )
            por_seccion = list(cur.fetchall())

            cur.execute(
                "SELECT DATE(created_at AT TIME ZONE 'America/Argentina/Buenos_Aires') AS dia, "
                "COUNT(*) AS v FROM visitas GROUP BY dia ORDER BY dia DESC LIMIT 14;"
            )
            por_dia = list(cur.fetchall())

            cur.execute(
                "SELECT id, nombre, apellido, email, pais, created_at "
                "FROM donaciones ORDER BY created_at DESC LIMIT 50;"
            )
            donaciones = list(cur.fetchall())

        conn.close()
        return {
            "total_visitas": total,
            "por_seccion": por_seccion,
            "por_dia": por_dia,
            "donaciones": donaciones,
        }
    except Exception as e:
        return {"error": str(e)}


# ── Límite diario de consultas a la IA ───────────────────────────────────────
# Antes el contador vivía en la memoria del proceso: se reiniciaba con cada
# deploy o reinicio de Railway. En la base persiste y lo comparten todas las
# réplicas. Si no hay base, main.py usa el contador en memoria como antes.
_SQL_IA = """
CREATE TABLE IF NOT EXISTS ia_usos (
    dia    DATE        NOT NULL,
    clave  VARCHAR(32) NOT NULL,
    usos   INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (dia, clave)
);
"""


def ia_reservar(dia, clave: str, limite_visitante: int, limite_global: int) -> int | None:
    """
    Reserva una consulta. Devuelve los usos previos del visitante si se reservó,
    -1 si se alcanzó un límite, o None si la base no está disponible.
    """
    if not _DATABASE_URL:
        return None
    try:
        conn = _get_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(_SQL_IA)
                cur.execute("SELECT pg_advisory_xact_lock(734200)")  # una reserva por vez
                cur.execute("SELECT COALESCE(SUM(usos), 0) AS t FROM ia_usos WHERE dia = %s", (dia,))
                total = cur.fetchone()["t"]
                cur.execute("SELECT usos FROM ia_usos WHERE dia = %s AND clave = %s", (dia, clave))
                fila = cur.fetchone()
                usados = fila["usos"] if fila else 0
                if usados >= limite_visitante or total >= limite_global:
                    return -1
                cur.execute(
                    "INSERT INTO ia_usos (dia, clave, usos) VALUES (%s, %s, 1) "
                    "ON CONFLICT (dia, clave) DO UPDATE SET usos = ia_usos.usos + 1",
                    (dia, clave),
                )
                return usados
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"ia_reservar: base no disponible ({e})")
        return None


def ia_devolver(dia, clave: str) -> None:
    """Devuelve una consulta reservada que no llegó a responder (no cuenta)."""
    if not _DATABASE_URL:
        return
    try:
        conn = _get_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute("UPDATE ia_usos SET usos = GREATEST(usos - 1, 0) WHERE dia = %s AND clave = %s", (dia, clave))
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"ia_devolver: {e}")
