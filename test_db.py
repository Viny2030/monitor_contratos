import psycopg2

url = "postgresql://postgres:IeNhJzpwXifkYtWorZPgZAURnUKycBJw@interchange.proxy.rlwy.net:36645/railway"

conn = psycopg2.connect(url)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM donaciones_consultas")
print("Donaciones:", cur.fetchone()[0])

cur.execute("SELECT COUNT(*) FROM estadisticas_acceso")
print("Estadisticas:", cur.fetchone()[0])

conn.close()