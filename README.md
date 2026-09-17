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
FRONTEND_URL="http://127.0.0.1:4321"
GOOGLE_CLIENT_ID="tu-google-client-id.apps.googleusercontent.com"
SUPABASE_URL="https://tu-proyecto.supabase.co"
SUPABASE_PUBLISHABLE_KEY="sb_publishable_xxxxxxxxx"
SUPABASE_JWT_SECRET="legacy-jwt-secret-de-supabase"
SUPABASE_REALTIME_TOKEN_MINUTES=15
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
FRONTEND_URL="https://sefkirastudio.com"
GOOGLE_CLIENT_ID="tu-google-client-id.apps.googleusercontent.com"
SUPABASE_URL="https://tu-proyecto.supabase.co"
SUPABASE_PUBLISHABLE_KEY="sb_publishable_xxxxxxxxx"
SUPABASE_JWT_SECRET="legacy-jwt-secret-de-supabase"
SUPABASE_REALTIME_TOKEN_MINUTES=15
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

`vercel.json` fija una sola region `lhr1` (Londres, AWS `eu-west-2`), junto a la
base de datos Supabase actual. El frontend tambien se ejecuta en esa region:
su proxy HTTP no debe cruzar el Atlantico para llegar a la API. Si cambia la
region de PostgreSQL, actualiza ambos archivos; no requiere multirregion ni
cambiar el plan. El dibujo en vivo viaja por Broadcast directamente, sin esperar
estas peticiones ni el guardado. Sus previews son solo visuales; las operaciones
confirmadas, la cola durable y el historial compartido siguen siendo autoritativos.

La sincronizacion en tiempo real usa Supabase Realtime Broadcast con canales privados por
usuario. La presencia de editores usa un canal privado por proyecto y solo autoriza a su
propietario y miembros; cada conexion anuncia el recurso que tiene abierto para mostrar los
avatares activos en el explorador. Ese mismo canal permite Broadcast privado entre miembros
para cursores, selecciones, parches de pixeles y acciones consolidadas del editor colaborativo.
`SUPABASE_PUBLISHABLE_KEY` es la clave publica del proyecto.
`SUPABASE_JWT_SECRET` es el `Legacy JWT secret` del proyecto y nunca debe exponerse en el
frontend. Tras configurar estas variables, ejecuta `alembic upgrade head` para instalar el
trigger de Broadcast y su politica RLS. Si la configuracion no esta completa, el frontend
usa automaticamente el SSE anterior como respaldo. La firma HS256 queda encapsulada en
`create_supabase_realtime_token` para poder migrarla posteriormente a una clave asimetrica
sin cambiar el protocolo del frontend.

`PROJECT_NAME`, `API_V1_STR`, `ACCESS_TOKEN_EXPIRE_MINUTES` y los datos de PostgreSQL separados tienen valores por defecto en el codigo, asi que no hace falta crearlos en Vercel. Tampoco hay usuario admin por defecto en variables de entorno.

En entornos no locales, incluido Vercel, la API falla al arrancar si `SECRET_KEY`
mantiene el valor por defecto o tiene menos de 32 caracteres.

Para crear una sesion con Google desde el frontend, llama a:

```text
POST /api/v1/auth/google
```

Body:

```json
{
  "credential": "jwt-devuelto-por-google-identity-services",
  "access_token": "access-token-devuelto-por-google"
}
```

Solo hace falta enviar uno de los dos campos. La API verifica el token contra
`GOOGLE_CLIENT_ID`, crea o actualiza el usuario por email y devuelve un
`access_token`.

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

API local:

```text
http://127.0.0.1:8000
```

Documentacion:

```text
http://127.0.0.1:8000/docs
```
