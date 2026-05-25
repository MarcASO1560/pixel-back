# Pixel Studio API

Backend de `pixel.studio`, una aplicacion para crear, organizar y guardar recursos audiovisuales para videojuegos. Este servicio expone la API principal y se conecta a PostgreSQL para gestionar usuarios, proyectos, carpetas, recursos, versiones y exports.

## Iniciar el servicio

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
docker compose up -d db
fastapi dev app/main.py
```

API local:

```text
http://127.0.0.1:8000
```

Documentacion:

```text
http://127.0.0.1:8000/docs
```
