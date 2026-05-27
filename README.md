# Pixel Studio API

Backend de `Sefkira Studio`, una aplicacion para crear, organizar y guardar recursos audiovisuales para videojuegos. Este servicio expone la API principal y se conecta a PostgreSQL para gestionar usuarios, proyectos, carpetas, recursos, versiones y exports.

## Iniciar el servicio

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Antes de iniciar la API, PostgreSQL debe estar disponible con la configuracion indicada en `.env`.

El `.env` local solo necesita estos valores:

```text
BACKEND_CORS_ORIGINS="http://localhost:4321,http://localhost:5173,http://127.0.0.1:4321,http://127.0.0.1:5173"
DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/pixel_studio"
SECRET_KEY="change-this-secret-key-use-at-least-32-characters"
FRONTEND_AUTH_TOKEN="change-this-frontend-auth-token"
FRONTEND_URL="http://127.0.0.1:4321"
GOOGLE_CLIENT_ID="tu-google-client-id.apps.googleusercontent.com"
RESEND_API_KEY="re_xxxxxxxxx"
RESEND_FROM_EMAIL="Sefkira Studio <no-reply@sefkirastudio.com>"
```

Despues crea la base de datos `pixel_studio` si no existe y ejecuta las migraciones:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\createdb.exe" -U postgres pixel_studio
.\.venv\Scripts\python.exe -m alembic upgrade head
```

## Variables para Vercel

En Vercel, configura solo estas variables:

```text
BACKEND_CORS_ORIGINS="https://sefkirastudio.com,https://www.sefkirastudio.com,http://localhost:4321"
DATABASE_URL="postgresql+psycopg://usuario:password@host:6543/postgres"
SECRET_KEY="clave-larga-de-32-caracteres-o-mas"
FRONTEND_AUTH_TOKEN="token-privado-que-tambien-usara-el-frontend"
FRONTEND_URL="https://sefkirastudio.com"
GOOGLE_CLIENT_ID="tu-google-client-id.apps.googleusercontent.com"
RESEND_API_KEY="re_xxxxxxxxx"
RESEND_FROM_EMAIL="Sefkira Studio <no-reply@sefkirastudio.com>"
SMTP_HOST="smtp.gmail.com"
SMTP_PORT=587
SMTP_USERNAME="tu-email@gmail.com"
SMTP_PASSWORD="app-password-o-token-smtp"
SMTP_FROM_EMAIL="tu-email@gmail.com"
SMTP_FROM_NAME="Sefkira Studio"
SMTP_USE_TLS=true
```

El backend ya permite por defecto `localhost`, `sefkirastudio.com`, `www.sefkirastudio.com`, `pixelartstudio.app` y `www.pixelartstudio.app`. `BACKEND_CORS_ORIGINS` solo hace falta si quieres sumar mas origenes.

Para Supabase en Vercel, usa la conexion `Transaction pooler` del panel de Supabase. Vercel es serverless, y Supabase recomienda ese modo para funciones temporales. El backend ya usa `NullPool` para no abrir un pool extra encima del pooler de Supabase y desactiva prepared statements para ser compatible con el pooler de transacciones.

`PROJECT_NAME`, `API_V1_STR`, `ACCESS_TOKEN_EXPIRE_MINUTES` y los datos de PostgreSQL separados tienen valores por defecto en el codigo, asi que no hace falta crearlos en Vercel. Tampoco hay usuario admin por defecto en variables de entorno.

Para crear una sesion con Google desde el frontend, llama a:

```text
POST /api/v1/auth/google
```

Body:

```json
{
  "credential": "jwt-devuelto-por-google-identity-services"
}
```

La API verifica ese token contra `GOOGLE_CLIENT_ID`, crea o actualiza el usuario por email y devuelve un `access_token`.

Para crear una cuenta con email y password:

```text
POST /api/v1/auth/register
```

Body:

```json
{
  "username": "sefkira",
  "email": "user@example.com",
  "password": "password-larga",
  "password_confirmation": "password-larga"
}
```

Para iniciar sesion con email y password:

```text
POST /api/v1/auth/login
```

Body:

```json
{
  "email": "user@example.com",
  "password": "password-larga"
}
```

Para pedir un reset de password:

```text
POST /api/v1/auth/password-reset/request
```

Body:

```json
{
  "email": "user@example.com"
}
```

El endpoint responde siempre `{"status": "ok"}` para no revelar si existe la cuenta. Si SMTP esta configurado, enviara un enlace a `FRONTEND_URL` con `reset_token` en la query. Ese token nunca se devuelve en la respuesta del endpoint; solo debe llegar al propietario del email.

El backend usa Resend si `RESEND_API_KEY` esta configurada. SMTP queda como respaldo opcional. Para Gmail no sirve la password normal de la cuenta: hay que crear una app password o usar credenciales SMTP equivalentes.

El endpoint legacy con token compartido sigue disponible en:

```text
POST /api/v1/auth/session
```

Body:

```json
{
  "auth_token": "valor_de_FRONTEND_AUTH_TOKEN",
  "email": "user@example.com",
  "display_name": "Nombre de Google",
  "avatar_url": "https://lh3.googleusercontent.com/...",
  "is_admin": false
}
```

La respuesta devuelve un `access_token`. Ese token se usa en `Authorize` para el resto de rutas privadas.

API local:

```text
http://127.0.0.1:8000
```

Documentacion:

```text
http://127.0.0.1:8000/docs
```
