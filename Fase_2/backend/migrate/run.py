"""Job de migración del esquema.

Se ejecuta ANTES de desplegar la API, no desde su arranque:

    python -m migrate.run                 # aplica lo pendiente
    python -m migrate.run --verificar     # solo informa la versión, sin DDL

En Azure es un Container Apps Job con `--parallelism 1`; el `pg_advisory_lock`
de `apply_schema()` es la red de seguridad si alguien lo lanza dos veces.

Conéctate con el rol dueño (`acredittia_owner` o el administrador del servidor):
el script crea roles, revoca privilegios y altera políticas, cosa que
`acredittia_app` no puede hacer. Usa `MIGRATE_DATABASE_URL` si el rol de
migración es distinto del de la API, que es lo normal en producción.
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("acredittia.migrate")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    # El rol de migración suele ser distinto del de la API. Se resuelve antes de
    # importar `app.database`, porque el engine se construye al importarlo.
    url = os.environ.get("MIGRATE_DATABASE_URL")
    if url:
        os.environ["DATABASE_URL"] = url
        log.info("Usando MIGRATE_DATABASE_URL")

    from app.config import settings
    from app.database import (SCHEMA_VERSION, apply_schema, schema_version,
                              wait_for_db)

    wait_for_db()
    actual = schema_version()
    log.info("Versión de esquema detectada: %s (esperada %s)", actual, SCHEMA_VERSION)

    if "--verificar" in argv:
        if actual != SCHEMA_VERSION:
            log.error("Esquema desfasado")
            return 1
        log.info("Esquema al día")
        return 0

    if actual >= SCHEMA_VERSION:
        log.info("Nada que aplicar")
        return 0

    log.info("Aplicando scripts desde %s", settings.schema_dir)
    final = apply_schema()
    if final != SCHEMA_VERSION:
        log.error("La migración terminó en versión %s, se esperaba %s",
                  final, SCHEMA_VERSION)
        return 1
    log.info("Esquema aplicado correctamente: versión %s", final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
