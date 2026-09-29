# Despliegue en Azure

Cuatro procesos, **una sola imagen**, diferenciados por el `command`. Así un
despliegue no puede dejar la API y el worker en versiones distintas del código.

| Proceso | Recurso Azure | Command |
|---|---|---|
| API | Container App | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| Worker | Container App | `celery -A worker.celery_app worker --concurrency=4` |
| Beat (cron) | Container App, 1 réplica fija | `celery -A worker.celery_app beat` |
| Migración | Container Apps Job, `--parallelism 1` | `python -m migrate.run` |

## Recursos necesarios

- **Azure Database for PostgreSQL Flexible Server** 16. Habilitar previamente
  `azure.extensions = CITEXT,PG_TRGM`, que el esquema exige.
- **Azure Cache for Redis** — broker y backend de Celery.
- **Storage Account** con un contenedor privado (`docs`).
- **Key Vault** (HSM recomendado) con una clave RSA llamada `plataforma-cred`.

## Tres roles de base de datos, no uno

Los crea `04_rls.sql` y `06_migracion_v11.sql`. La separación no es cosmética:
es lo que hace que el vault JWE (§8.2) sea real.

| Rol | Lo usa | Puede leer `credencial_jwe` |
|---|---|---|
| `acredittia_owner` | job de migración | sí (es el dueño) |
| `acredittia_app` | API | **no** — SELECT revocado |
| `acredittia_worker` | worker y beat | sí |

Cambia las contraseñas placeholder `CAMBIAR_EN_KEY_VAULT` en el primer
despliegue y guárdalas en Key Vault. Si la API se ejecutara con el rol del
worker, la separación de privilegios desaparece sin que nada falle: es el error
de configuración más fácil de cometer y el más difícil de notar.

## Variables de entorno

### API
```
DATABASE_URL      postgresql+psycopg://acredittia_app:<pwd>@<host>:5432/acredittia?sslmode=require
DB_RLS_ENABLED    true
DB_APPLY_SCHEMA_ON_START  false      # lo hace el job de migración
SCHEMA_VERSION_ESPERADA   6
JWT_SECRET        <secreto de Key Vault>
CORS_ORIGINS      https://app.acredittia.cl
PUBLIC_BASE_URL   https://api.acredittia.cl
STORAGE_BACKEND   azure
AZURE_STORAGE_ACCOUNT  acredittiast
AZURE_BLOB_CONTAINER   docs
QUEUE_BACKEND     celery
REDIS_URL         rediss://:<clave>@<cache>.redis.cache.windows.net:6380/0
JWE_BACKEND       keyvault
AZURE_KEYVAULT_URL     https://acredittia-kv.vault.azure.net
KEYVAULT_KEY_NAME      plataforma-cred
IA_BACKEND        claude
ANTHROPIC_API_KEY <secreto de Key Vault>
```

### Worker y beat
Lo mismo, salvo:
```
WORKER_DATABASE_URL  postgresql+psycopg://acredittia_worker:<pwd>@<host>:5432/acredittia?sslmode=require
```
`worker/celery_app.py` la copia sobre `DATABASE_URL` antes de importar la
aplicación, para que el engine se construya con el rol correcto.

### Job de migración
```
MIGRATE_DATABASE_URL  postgresql+psycopg://acredittia_owner:<pwd>@<host>:5432/acredittia?sslmode=require
SCHEMA_DIR            /schema      # monta modelo_datos/
```

## Identidad administrada

Asigna a la identidad de los Container Apps:

- **Storage Blob Data Contributor** sobre la cuenta de almacenamiento. Hace falta
  el rol de *Contributor*, no solo *Reader*, porque emitir SAS de usuario
  delegado exige pedir la clave de delegación.
- **Key Vault Crypto User** sobre la clave `plataforma-cred` (operaciones
  `wrapKey` y `unwrapKey`).
- **Key Vault Secrets User** si se usa `KeyVaultSecretWriter` para las
  credenciales de integración.

`DefaultAzureCredential` las resuelve sin secretos en configuración. En
desarrollo la misma credencial usa `az login`.

## Parámetro del servidor obligatorio

```
log_error_verbosity = terse
```

Sin él, una violación de NOT NULL o UNIQUE sobre `plataforma_credenciales` emite
`DETAIL: Failing row contains (...)` con el JWE incluido, y ese texto va al log
del servidor. El JWE no es descifrable sin Key Vault, pero el log pasaría a
tener la misma clasificación que la base de datos. Ver §4.8 y §10 del modelo de
datos.

## Orden de despliegue

```bash
# 1. Esquema (falla el deploy si no termina bien)
az containerapp job start -n acredittia-migrate -g <rg>

# 2. Verificación
az containerapp job start -n acredittia-migrate -g <rg> --command "python -m migrate.run --verificar"

# 3. API, worker y beat
az containerapp update -n acredittia-api    -g <rg> --image <acr>/acredittia:<tag>
az containerapp update -n acredittia-worker -g <rg> --image <acr>/acredittia:<tag>
az containerapp update -n acredittia-beat   -g <rg> --image <acr>/acredittia:<tag>
```

La API comprueba la versión del esquema al arrancar y **falla rápido** si no
coincide, así que un despliegue con el orden invertido se detecta en el probe y
no a mitad de un request.

## Probes

- Liveness: `GET /health`
- Readiness: `GET /health/esquema` — devuelve 503 si la base no está en la
  versión esperada.

## Escalado

- **API**: sin estado, escala por peticiones. El contexto de tenant vive en un
  `ContextVar` por request, no en el proceso.
- **Worker**: escala por longitud de cola con KEDA sobre Redis.
- **Beat**: exactamente **1 réplica**. Dos instancias dispararían el cron dos
  veces; los snapshots de cumplimiento lo absorben (son idempotentes por día),
  pero los reportes programados se duplicarían.

## Cron diario

`celery beat` ejecuta a las 00:30 America/Santiago: recálculo de vencimientos,
snapshots de cumplimiento, expiración de credenciales. A las 04:00 purga los
blobs temporales de extracción IA. Cada hora, al minuto 5, dispara los reportes
programados que toquen.

Con `QUEUE_BACKEND=inproc` el cron corre dentro del proceso de la API vía
APScheduler; sirve para desarrollo y **no** para producción con más de una
réplica.
