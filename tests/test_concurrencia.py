"""
Concurrencia real en PostgreSQL (DTE_TEST_DATABASE_URL=postgresql+psycopg://...):

- Emisiones en paralelo: SELECT ... FOR UPDATE sobre el CAF asigna folios sin
  repetir y sin depender de reintentos.
- Varios workers sobre la misma cola: SKIP LOCKED hace que cada envío se suba
  al SII una sola vez.
"""

import re
import threading
from collections import Counter

import pytest
from sqlalchemy import func, select

from app.models import Documento, EnvioSii
from app.tareas import Procesador

from .conftest import RUT, caf_xml, factura

pytestmark = pytest.mark.usefixtures('solo_postgres')

EMISIONES = 50
WORKERS = 4


@pytest.fixture
def solo_postgres(database):
    if database.engine.dialect.name != 'postgresql':
        pytest.skip('Requiere PostgreSQL (DTE_TEST_DATABASE_URL)')


def en_paralelo(funcion, veces):
    errores = []

    def correr():
        try:
            funcion()
        except Exception as e:  # noqa: BLE001
            errores.append(e)

    hilos = [threading.Thread(target=correr) for _ in range(veces)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    return errores


def test_folios_y_cola_en_paralelo(client, empresa, app, settings, database, monkeypatch):
    client.post(f'/v1/empresas/{RUT}/cafs', files={'archivo': ('c.xml', caf_xml(33, 11, 100))})
    asignaciones = Counter()
    original = app.state.servicio.asignar_folio

    def contar(sesion, empresa_, tipo):
        caf, folio = original(sesion, empresa_, tipo)
        asignaciones[folio] += 1
        return caf, folio

    monkeypatch.setattr(app.state.servicio, 'asignar_folio', contar)
    respuestas = []
    errores = en_paralelo(lambda: respuestas.append(
        client.post(f'/v1/empresas/{RUT}/documentos', json=factura()).status_code), EMISIONES)
    assert errores == [] and respuestas == [202] * EMISIONES
    with database.sesiones() as s:
        folios = sorted(s.scalars(select(Documento.folio).where(Documento.tipo == 33)))
    assert folios == list(range(1, EMISIONES + 1))
    # Con FOR UPDATE ningún folio se asigna dos veces (no hubo reintentos por choque).
    assert max(asignaciones.values()) == 1

    subidas = Counter()
    lock = threading.Lock()

    class SiiFalso:
        def __init__(self, certificado, ambiente):
            self.certificado = certificado

        def enviar(self, envio, rut):
            with lock:
                subidas[re.search(rb'<Folio>(\d+)</Folio>', envio).group(1)] += 1
                return 100000 + sum(subidas.values())

        def consultar_envio(self, track_id, rut, boletas=False):
            return {'estado': 'EPR', 'final': True, 'informados': 1, 'aceptados': 1, 'rechazados': 0, 'reparos': 0,
                    'detalle': []}

    procesadores = [Procesador(settings, app.state.servicio, cliente_sii=SiiFalso,
                               http_post=lambda *a, **k: type('R', (), {'status_code': 200})())
                    for _ in range(WORKERS)]

    def worker(procesador):
        with database.sesiones() as sesion:
            procesador.procesar_pendientes(sesion, maximo=1000)

    hilos = [threading.Thread(target=worker, args=(p,)) for p in procesadores]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()

    with database.sesiones() as s:
        enviados = s.scalar(select(func.count()).select_from(EnvioSii).where(EnvioSii.track_id.is_not(None)))
    assert enviados == EMISIONES
    assert sum(subidas.values()) == EMISIONES and max(subidas.values()) == 1
