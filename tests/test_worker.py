"""Tests del worker: envío al SII, consultas de estado, webhooks y reintentos."""

import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from rypsystems_dte.sii.webservices.exceptions import SiiCommunicationError

from app.models import Documento, Tarea, ahora
from app.tareas import Procesador, estado_documento

from .conftest import RUT, factura


class SiiFalso:
    """Reemplaza a ClienteSii: registra envíos y entrega estados programados."""

    def __init__(self, certificado, ambiente):
        self.certificado = certificado
        self.ambiente = ambiente
        self.envios = []
        self.estados = []
        self.fallas_envio = 0

    def enviar(self, envio, rut):
        if self.fallas_envio:
            self.fallas_envio -= 1
            raise SiiCommunicationError('HTTP 503')
        self.envios.append((envio, rut))
        return 262850000 + len(self.envios)

    def consultar_envio(self, track_id, rut, boletas=False):
        return self.estados.pop(0)


def consulta(estado='EPR', aceptados=1, rechazados=0, reparos=0, final=True):
    return {'estado': estado, 'final': final, 'informados': 1, 'aceptados': aceptados, 'rechazados': rechazados,
            'reparos': reparos, 'detalle': []}


class Webhooks:
    def __init__(self):
        self.recibidos = []
        self.status = 200

    def __call__(self, url, data, timeout, headers):
        self.recibidos.append((url, data, headers))
        return type('R', (), {'status_code': self.status})()


@pytest.fixture
def entorno(app, settings, database):
    sii = {}

    def fabrica(certificado, ambiente):
        sii['cliente'] = SiiFalso(certificado, ambiente)
        return sii['cliente']

    webhooks = Webhooks()
    procesador = Procesador(settings, app.state.servicio, cliente_sii=fabrica, http_post=webhooks)
    return procesador, sii, webhooks, database


def adelantar(database):
    """Deja todas las tareas pendientes listas para ejecutarse ahora."""
    with database.sesiones() as s:
        s.execute(update(Tarea).where(Tarea.estado == 'pendiente').values(ejecutar_en=ahora() - timedelta(seconds=1)))
        s.commit()


def documento(database, doc_id):
    with database.sesiones() as s:
        return s.get(Documento, doc_id)


def test_flujo_completo(client, empresa, entorno):
    procesador, sii, webhooks, database = entorno
    doc = client.post(f'/v1/empresas/{RUT}/documentos', json=factura()).json()

    with database.sesiones() as s:
        assert procesador.procesar_pendientes(s) == 2          # enviar y webhook "enviado"
    cliente = sii['cliente']
    envio, rut = cliente.envios[0]
    assert rut == RUT and b'<EnvioDTE' in envio
    assert documento(database, doc['id']).estado == 'enviado'
    assert documento(database, doc['id']).track_id == '262850001'

    cliente.estados = [consulta(estado='SOK', aceptados=0, final=False), consulta()]
    adelantar(database)
    with database.sesiones() as s:
        procesador.procesar_pendientes(s)                     # consulta: aún en proceso, se reagenda
    assert documento(database, doc['id']).estado == 'enviado'
    adelantar(database)
    with database.sesiones() as s:
        procesador.procesar_pendientes(s)                     # consulta final + webhooks
    final = client.get(f'/v1/documentos/{doc["id"]}').json()
    assert final['estado'] == 'aceptado' and final['detalle_sii']['aceptados'] == 1

    eventos = [json.loads(cuerpo) for _, cuerpo, _ in webhooks.recibidos]
    assert [(e['estado_anterior'], e['documento']['estado']) for e in eventos] == [
        ('emitido', 'enviado'), ('enviado', 'aceptado')]
    url, cuerpo, cabeceras = webhooks.recibidos[-1]
    esperada = 'sha256=' + hmac.new(b'secreto', cuerpo, hashlib.sha256).hexdigest()
    assert cabeceras['X-DTE-Firma'] == esperada and cabeceras['X-DTE-Evento'] == 'documento.estado'


def test_boleta_se_envia_como_envio_boleta(client, empresa, entorno):
    procesador, sii, _, database = entorno
    client.post(f'/v1/empresas/{RUT}/documentos', json={'documento': {
        'Encabezado': {'IdDoc': {'TipoDTE': 39}}, 'Detalle': [{'NmbItem': 'Cafe', 'QtyItem': 1, 'PrcItem': 1500}]}})
    with database.sesiones() as s:
        procesador.procesar_pendientes(s)
    assert b'<EnvioBOLETA' in sii['cliente'].envios[0][0]


def test_reintento_y_error_de_envio(client, empresa, entorno, settings):
    procesador, sii, _, database = entorno
    doc = client.post(f'/v1/empresas/{RUT}/documentos', json=factura()).json()
    procesador.cliente_sii(documento_con_empresa(database, doc['id']))
    sii['cliente'].fallas_envio = 1
    with database.sesiones() as s:
        procesador.procesar_pendientes(s)
        tarea = s.scalar(select(Tarea).where(Tarea.documento_id == doc['id'], Tarea.tipo == 'enviar'))
        assert tarea.estado == 'pendiente' and 'HTTP 503' in tarea.error
    adelantar(database)
    with database.sesiones() as s:
        procesador.procesar_pendientes(s)
    assert documento(database, doc['id']).estado == 'enviado'

    # Se agotan los intentos: el documento queda con error
    otro = client.post(f'/v1/empresas/{RUT}/documentos', json=factura()).json()
    sii['cliente'].fallas_envio = settings.tareas_max_intentos
    for _ in range(settings.tareas_max_intentos):
        adelantar(database)
        with database.sesiones() as s:
            procesador.procesar_pendientes(s)
    estado = documento(database, otro['id'])
    assert estado.estado == 'error' and estado.detalle_sii['etapa'] == 'enviar'


def documento_con_empresa(database, doc_id):
    s = database.sesiones()
    doc = s.get(Documento, doc_id)
    doc.empresa.certificado  # carga la relación
    return doc


def test_webhook_fallido_se_reintenta(client, empresa, entorno):
    procesador, sii, webhooks, database = entorno
    client.post(f'/v1/empresas/{RUT}/documentos', json=factura())
    webhooks.status = 500
    with database.sesiones() as s:
        procesador.procesar_pendientes(s)
        tarea = s.scalar(select(Tarea).where(Tarea.tipo == 'webhook'))
        assert tarea.estado == 'pendiente' and 'HTTP 500' in tarea.error


@pytest.mark.parametrize('resultado, estado', [
    (consulta(), 'aceptado'),
    (consulta(aceptados=0, reparos=1), 'aceptado_reparos'),
    (consulta(aceptados=0, rechazados=1), 'rechazado'),
    (consulta(estado='RSC', aceptados=0), 'rechazado'),
    (consulta(estado='-11', aceptados=0), 'rechazado'),
])
def test_estado_documento(resultado, estado):
    assert estado_documento(resultado) == estado
