# Imagen única para los cuatro procesos: API, worker, beat y job de migración.
# Se diferencian solo por el `command`, así que un despliegue no puede dejarlos
# en versiones distintas del código.
FROM python:3.12-slim

# psycopg[binary] trae libpq; curl es para los probes de Container Apps.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY worker ./worker
COPY migrate ./migrate
COPY tests ./tests

# Usuario sin privilegios; los directorios de datos se montan como volumen.
RUN useradd --create-home --uid 10001 acredittia \
    && mkdir -p /data/uploads /data/keys \
    && chown -R acredittia:acredittia /data
USER acredittia

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# API. El worker usa `celery -A worker.celery_app worker`, el beat `... beat`
# y el job de migración `python -m migrate.run`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
