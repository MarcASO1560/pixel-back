# Pixel Studio API

Backend de `pixel.studio`, una aplicacion para crear, organizar y guardar recursos audiovisuales para videojuegos. Este servicio expone la API principal y se conecta a PostgreSQL para gestionar usuarios, proyectos, carpetas, recursos, versiones y exports.

## Iniciar el servicio

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Antes de iniciar la API, PostgreSQL debe estar disponible con la configuracion indicada en `.env`.

Para crear una sesion desde el frontend, llama a:

```text
POST /api/v1/auth/session
Header: X-API-Key: valor_de_FRONTEND_API_KEY
```

Body:

```json
{
  "email": "admin@example.com",
  "display_name": "Admin",
  "is_admin": true
}
```

Todas las rutas privadas usan el mismo header `X-API-Key`.

API local:

```text
http://127.0.0.1:8000
```

Documentacion:

```text
http://127.0.0.1:8000/docs
```
