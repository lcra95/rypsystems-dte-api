"""Tests de documentos recibidos: carga del EnvioDTE, respuestas del intercambio y registro de reclamos."""

import base64
from datetime import datetime

import pytest
from lxml import etree

from rypsystems_dte import emitir_documento
from rypsystems_dte.dte import DocumentEnvelope
from rypsystems_dte.identifier import CafFaker
from rypsystems_dte.xml.schema import schema_errors

from app.recibidos import Recibidos

from .conftest import RUT

PROVEEDOR = '11222333-9'
EMISOR_PROVEEDOR = {'RUTEmisor': PROVEEDOR, 'RznSoc': 'Proveedor SpA', 'GiroEmis': 'Comercio', 'Acteco': 471000,
                    'DirOrigen': 'Av. Siempre Viva 742', 'CmnaOrigen': 'Santiago'}
NOSOTROS = {'RUTRecep': RUT, 'RznSocRecep': 'SASCO SpA', 'GiroRecep': 'Servicios', 'DirRecep': 'Santa Cruz',
            'CmnaRecep': 'Santa Cruz'}
OTRO = {**NOSOTROS, 'RUTRecep': '60803000-K', 'RznSocRecep': 'SII'}


@pytest.fixture(scope='module')
def envio_proveedor(certificado):
    caf = CafFaker(rut=PROVEEDOR, tipo_documento=33, desde=1, hasta=10).create()
    documentos = [
        emitir_documento({'Encabezado': {'IdDoc': {'TipoDTE': 33, 'Folio': folio, 'FmaPago': 2},
                                         'Emisor': dict(EMISOR_PROVEEDOR), 'Receptor': dict(receptor)},
                          'Detalle': [{'NmbItem': f'Insumo {folio}', 'QtyItem': 1, 'PrcItem': 10000 * folio}]},
                         caf, certificado)
        for folio, receptor in ((1, NOSOTROS), (2, NOSOTROS), (3, OTRO))
    ]
    sobre = DocumentEnvelope(certificado, '2014-08-22', 80, rut_receptor=RUT)
    for doc in documentos:
        sobre.add_document(doc.xml)
    return sobre.build()


class RegistroFalso:
    def __init__(self):
        self.acciones = []
        self.eventos = []

    def authenticate(self, certificado):
        return 'TOKEN'

    def registrar(self, rut_emisor, tipo, folio, accion):
        if any(a[:3] == (rut_emisor, tipo, folio) for a in self.acciones):
            return {'ok': False, 'codigo': 7, 'descripcion': 'Evento registrado previamente', 'eventos': []}
        self.acciones.append((rut_emisor, tipo, folio, accion))
        return {'ok': True, 'codigo': 0, 'descripcion': 'Accion Completada OK', 'eventos': []}

    def listar_eventos(self, rut_emisor, tipo, folio):
        return {'codigo': 15, 'descripcion': 'Listado de eventos del documento', 'eventos': self.eventos}

    def fecha_recepcion(self, rut_emisor, tipo, folio):
        return datetime(2026, 10, 18, 10, 0, 0)


@pytest.fixture
def registro(app):
    falso = RegistroFalso()
    app.state.recibidos = Recibidos(app.state.servicio, registro_reclamo=lambda certificado, ambiente: falso)
    return falso


def cargar(client, envio, **datos):
    return client.post(f'/v1/empresas/{RUT}/recibidos', files={'archivo': ('ENVIO_DTE_1.xml', envio)}, data=datos)


def test_cargar_envio(client, empresa, envio_proveedor):
    respuesta = cargar(client, envio_proveedor)
    assert respuesta.status_code == 201, respuesta.text
    datos = respuesta.json()
    assert datos['envio']['estado'] == 0 and datos['envio']['cod_envio'] == 1
    estados = {d['folio']: (d['estado'], d['estado_recepcion']) for d in datos['documentos']}
    assert estados == {1: ('pendiente', 0), 2: ('pendiente', 0), 3: ('no_recibido', 3)}

    # El mismo envío de nuevo: los documentos ya están registrados
    otra = cargar(client, envio_proveedor).json()
    assert otra['envio']['cod_envio'] == 2 and otra['documentos'] == [] and len(otra['repetidos']) == 3

    listado = client.get(f'/v1/empresas/{RUT}/recibidos', params={'estado': 'pendiente'}).json()
    assert listado['total'] == 2
    doc = listado['documentos'][0]
    assert client.get(f'/v1/recibidos/{doc["id"]}/xml').content.startswith(b'<?xml')
    pdf = client.get(f'/v1/recibidos/{doc["id"]}/pdf')
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')


def test_envio_invalido(client, empresa, envio_proveedor):
    assert cargar(client, b'<no-es-xml').status_code == 422
    alterado = envio_proveedor.replace(b'Insumo 1', b'Insumo X')
    datos = cargar(client, alterado).json()
    assert datos['envio']['estado'] == 2  # firma del envío inválida
    assert {d['estado'] for d in datos['documentos']} == {'no_recibido'}


def test_respuestas_del_intercambio(client, empresa, envio_proveedor):
    datos = cargar(client, envio_proveedor).json()
    rechazado = next(d for d in datos['documentos'] if d['folio'] == 2)
    respuesta = client.post(f'/v1/envios-recibidos/{datos["envio"]["id"]}/respuesta', json={
        'rechazar': [rechazado['id']], 'nombre_contacto': 'Contabilidad', 'mail_contacto': 'conta@example.com'})
    assert respuesta.status_code == 200, respuesta.text
    archivos = {k: base64.b64decode(v['contenido']) for k, v in respuesta.json().items()}
    assert set(archivos) == {'recepcion_envio', 'resultado_dte', 'recibo_mercaderias'}
    assert schema_errors(archivos['recepcion_envio'], 'RespuestaEnvioDTE_v10.xsd') == []
    assert schema_errors(archivos['recibo_mercaderias'], 'EnvioRecibos_v10.xsd') == []

    resultado = etree.fromstring(archivos['resultado_dte'])
    estados = {int(r.xpath('string(./*[local-name()="Folio"])')): r.xpath('string(./*[local-name()="EstadoDTE"])')
               for r in resultado.xpath('//*[local-name()="ResultadoDTE"]')}
    assert estados == {1: '0', 2: '2', 3: '2'}
    recibos = etree.fromstring(archivos['recibo_mercaderias']).xpath('//*[local-name()="DocumentoRecibo"]')
    assert [r.xpath('string(./*[local-name()="Folio"])') for r in recibos] == ['1']

    otro = client.post(f'/v1/envios-recibidos/{datos["envio"]["id"]}/respuesta', json={'rechazar': ['no-es-de-aqui']})
    assert otro.status_code == 422


def test_aceptar_y_reclamar_en_el_sii(client, empresa, envio_proveedor, registro):
    docs = {d['folio']: d for d in cargar(client, envio_proveedor).json()['documentos']}
    aceptado = client.post(f'/v1/recibidos/{docs[1]["id"]}/aceptar', json={'recibo_mercaderias': True})
    assert aceptado.status_code == 200 and aceptado.json()['estado'] == 'recibo_mercaderias'
    reclamado = client.post(f'/v1/recibidos/{docs[2]["id"]}/reclamar', json={'accion': 'RCD'})
    assert reclamado.status_code == 200 and reclamado.json()['estado'] == 'reclamado'
    assert registro.acciones == [(PROVEEDOR, 33, 1, 'ERM'), (PROVEEDOR, 33, 2, 'RCD')]

    repetido = client.post(f'/v1/recibidos/{docs[1]["id"]}/aceptar')
    assert repetido.status_code == 409 and 'previamente' in repetido.json()['detail']
    assert client.post(f'/v1/recibidos/{docs[2]["id"]}/reclamar', json={'accion': 'ACD'}).status_code == 422

    # Los eventos del SII sincronizan el estado (p. ej. aceptado desde el sitio del SII)
    registro.eventos = [{'codigo': 'ACD', 'descripcion': 'Acepta Contenido', 'responsable': RUT, 'fecha': 'x'}]
    eventos = client.get(f'/v1/recibidos/{docs[3]["id"]}/eventos').json()
    assert eventos['documento']['estado'] == 'aceptado'
    assert eventos['fecha_recepcion_sii'].startswith('2026-10-18')
