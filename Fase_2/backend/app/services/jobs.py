"""Cola de trabajos asíncronos.

Backends (QUEUE_BACKEND):
  celery  Celery sobre Azure Cache for Redis. Un Container App aparte ejecuta
          el worker y otro el beat (cron de vencimientos, snapshots y
          expiración de credenciales).
  inproc  Ejecución en el propio proceso vía BackgroundTasks. Solo desarrollo y
          tests: los jobs no sobreviven un reinicio ni escalan con réplicas.

Los routers nunca importan Celery: encolan con `enqueue(nombre, **kwargs)` y el
registro `TAREAS` resuelve la función. Así el mismo código corre con y sin
Redis, y añadir una tarea no obliga a tocar la configuración de la cola.
"""
from __future__ import annotations

import logging
import traceback
from abc import ABC, abstractmethod
from typing import Callable

from ..config import settings

log = logging.getLogger("acredittia.jobs")

# nombre lógico -> función. La registran los módulos de tareas al importarse.
TAREAS: dict[str, Callable] = {}


def tarea(nombre: str):
    """Decorador de registro. La función debe ser serializable por kwargs."""
    def wrap(fn: Callable) -> Callable:
        TAREAS[nombre] = fn
        return fn
    return wrap


class JobQueue(ABC):
    @abstractmethod
    def enqueue(self, nombre: str, **kwargs) -> None: ...


class InProcessQueue(JobQueue):
    """Ejecuta de inmediato y en el mismo hilo, capturando la excepción.

    Se ejecuta síncronamente a propósito: en tests el resultado debe estar
    disponible en la aserción siguiente sin esperas.
    """

    def enqueue(self, nombre: str, **kwargs) -> None:
        fn = TAREAS.get(nombre)
        if fn is None:
            log.error("Tarea desconocida: %s", nombre)
            return
        try:
            fn(**kwargs)
        except Exception:
            log.error("Tarea %s falló: %s", nombre, traceback.format_exc())


class CeleryQueue(JobQueue):
    def __init__(self):
        from worker.celery_app import ejecutar_tarea    # import perezoso
        self._ejecutar = ejecutar_tarea

    def enqueue(self, nombre: str, **kwargs) -> None:
        self._ejecutar.delay(nombre, kwargs)


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = CeleryQueue() if settings.queue_backend == "celery" else InProcessQueue()
    return _queue


def reset_queue() -> None:
    global _queue
    _queue = None


def enqueue(nombre: str, **kwargs) -> None:
    """Encola una tarea por nombre lógico."""
    get_queue().enqueue(nombre, **kwargs)
