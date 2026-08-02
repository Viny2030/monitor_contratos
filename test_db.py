import os
import psycopg2

# La URL de conexión ya NO se hardcodea acá — la contraseña vieja quedó
# expuesta en el repo público (GitHub) y fue rotada en Railway. Ahora se
# toma de la variable de entorno DATABASE_URL (la misma que usa db.py/main.py).
url = os.environ.get("DATABASE_URL", "")

if not url:
    raise SystemExit(
        "❌ Falta configurar DATABASE_URL (export DATABASE_URL=... o setearla "
        "en el .env local) — no hay credencial hardcodeada por seguridad."
    )

conn = psycopg2.connect(url)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM donaciones_consultas")
print("Donaciones:", cur.fetchone()[0])

cur.execute("SELECT COUNT(*) FROM estadisticas_acceso")
print("Estadisticas:", cur.fetchone()[0])

conn.close()
