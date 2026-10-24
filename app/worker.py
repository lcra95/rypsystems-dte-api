"""
Worker: procesa las tareas de envío y consulta al SII y los webhooks.

    python -m app.worker

Se pueden correr varios en paralelo (en PostgreSQL cada tarea la toma uno
solo, con SELECT ... FOR UPDATE SKIP LOCKED).

Tras cada vuelta sin errores toca el archivo DTE_WORKER_LATIDO (por defecto
/tmp/dte-worker-latido); el healthcheck de Docker revisa que sea reciente
(python -m app.worker --salud).
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

from .config import get_settings
from .db import Database
from .seguridad import Cifrador
from .servicio import Servicio
from .tareas import Procesador

logger = logging.getLogger('app.worker')
LATIDO = Path(os.environ.get('DTE_WORKER_LATIDO', '/tmp/dte-worker-latido'))
LATIDO_MAXIMO = 600  # segundos: una vuelta puede esperar varios timeouts del SII


def sano(ahora: float = None) -> bool:
    try:
        return (ahora or time.time()) - LATIDO.stat().st_mtime < LATIDO_MAXIMO
    except FileNotFoundError:
        return False


def main(espera: float = 2.0) -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    settings = get_settings()
    db = Database(settings.database_url)
    procesador = Procesador(settings, Servicio(Cifrador(settings.master_key), settings.permitir_caf_falsos))
    activo = True

    def detener(*_):
        nonlocal activo
        activo = False
        logger.info('Deteniendo el worker...')

    signal.signal(signal.SIGTERM, detener)
    signal.signal(signal.SIGINT, detener)
    logger.info('Worker iniciado.')
    while activo:
        try:
            with db.sesiones() as sesion:
                procesadas = procesador.procesar_pendientes(sesion, maximo=50)
            LATIDO.touch()
        except Exception:  # noqa: BLE001 - p. ej. base de datos no disponible
            logger.exception('Error procesando tareas.')
            procesadas = 0
        if not procesadas:
            time.sleep(espera)


if __name__ == '__main__':
    if '--salud' in sys.argv:
        sys.exit(0 if sano() else 1)
    main()
