"""Tests de la API: empresas, certificados, CAF, emisión, PDF y anulación."""

import threading

from fastapi.testclient import TestClient
from lxml import etree

from .conftest import RUT, TOKEN, caf_xml, factura


def test_requiere_token(app):
    with TestClient(app) as sin_token:
        assert sin_token.get(f'/v1/empresas/{RUT}').status_code == 401
        assert sin_token.get(f'/v1/empresas/{RUT}', headers={'Authorization': 'Bearer otro'}).status_code == 401
        assert sin_token.get('/salud').status_code == 200
        assert sin_token.get('/licencia').json()['licencia'] == 'AGPL-3.0-or-later'


def test_empresa_y_folios(client, empresa):
    assert empresa['rut'] == RUT and empresa['ambiente'] == 'certificacion'
    assert client.post('/v1/empresas', json={**empresa, 'emisor': empresa['emisor']}).status_code == 409
    folios = client.get(f'/v1/empresas/{RUT}/folios').json()
    assert folios['disponibles'] == {'33': 10, '39': 10, '56': 10, '61': 10}
    # CAF repetido o de otra empresa
    assert client.post(f'/v1/empresas/{RUT}/cafs', files={'archivo': ('c.xml', caf_xml(33))}).status_code == 409
    otro = client.post(f'/v1/empresas/{RUT}/cafs', files={'archivo': ('c.xml', caf_xml(34, rut='11222333-9'))})
    assert otro.status_code == 422 and 'no de' in otro.json()['detail']
    # Emisor incompleto
    respuesta = client.patch(f'/v1/empresas/{RUT}', json={'emisor': {'RznSoc': 'X'}})
    assert respuesta.status_code == 422
    # Con CAF de certificación no se puede pasar a producción
    respuesta = client.patch(f'/v1/empresas/{RUT}', json={'ambiente': 'produccion'})
    assert respuesta.status_code == 409 and 'ambiente' in respuesta.json()['detail']
    assert client.patch(f'/v1/empresas/{RUT}', json={'ambiente': 'certificacion'}).status_code == 200


def test_emitir_factura(client, empresa):
    respuesta = client.post(f'/v1/empresas/{RUT}/documentos', json=factura())
    assert respuesta.status_code == 202, respuesta.text
    doc = respuesta.json()
    assert (doc['tipo'], doc['folio'], doc['estado'], doc['monto_total']) == (33, 1, 'emitido', 119000)
    assert doc['receptor'] == '60803000-K'

    assert client.get(f'/v1/documentos/{doc["id"]}').json()['folio'] == 1
    xml = client.get(f'/v1/documentos/{doc["id"]}/xml')
    raiz = etree.fromstring(xml.content)
    assert raiz.xpath('string(//*[local-name()="RUTEmisor"])') == RUT
    assert raiz.xpath('string(//*[local-name()="RznSoc"])') == 'SASCO SpA'
    for formato in ('carta', '80mm'):
        pdf = client.get(f'/v1/documentos/{doc["id"]}/pdf', params={'formato': formato, 'cedible': True})
        assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')

    siguiente = client.post(f'/v1/empresas/{RUT}/documentos', json=factura()).json()
    assert siguiente['folio'] == 2
    listado = client.get(f'/v1/empresas/{RUT}/documentos', params={'tipo': 33}).json()
    assert listado['total'] == 2
    assert client.get(f'/v1/empresas/{RUT}/folios').json()['disponibles']['33'] == 8


def test_idempotencia(client, empresa):
    cabecera = {'Idempotency-Key': 'pedido-123'}
    primera = client.post(f'/v1/empresas/{RUT}/documentos', json=factura(), headers=cabecera)
    segunda = client.post(f'/v1/empresas/{RUT}/documentos', json=factura(), headers=cabecera)
    assert primera.status_code == 202 and segunda.status_code == 200
    assert primera.json()['id'] == segunda.json()['id']


def test_errores_de_emision(client, empresa):
    con_folio = factura()
    con_folio['documento']['Encabezado']['IdDoc']['Folio'] = 5
    assert client.post(f'/v1/empresas/{RUT}/documentos', json=con_folio).status_code == 422
    sin_caf = factura()
    sin_caf['documento']['Encabezado']['IdDoc']['TipoDTE'] = 34
    respuesta = client.post(f'/v1/empresas/{RUT}/documentos', json=sin_caf)
    assert respuesta.status_code == 409 and 'No quedan folios' in respuesta.json()['detail']
    invalida = factura()
    invalida['documento']['Encabezado']['Receptor']['RUTRecep'] = 'xx'
    respuesta = client.post(f'/v1/empresas/{RUT}/documentos', json=invalida)
    assert respuesta.status_code == 422
    # Un error de datos no consume el folio
    assert client.post(f'/v1/empresas/{RUT}/documentos', json=factura()).json()['folio'] == 1
    assert client.get('/v1/documentos/no-existe').status_code == 404
    assert client.get('/v1/empresas/1-9').status_code == 404


def test_boleta(client, empresa):
    respuesta = client.post(f'/v1/empresas/{RUT}/documentos', json={'documento': {
        'Encabezado': {'IdDoc': {'TipoDTE': 39}},
        'Detalle': [{'NmbItem': 'Cafe', 'QtyItem': 2, 'PrcItem': 1500}],
    }})
    assert respuesta.status_code == 202, respuesta.text
    assert respuesta.json()['receptor'] == '66666666-6' and respuesta.json()['monto_total'] == 3000


def test_anular(client, empresa):
    original = client.post(f'/v1/empresas/{RUT}/documentos', json=factura()).json()
    respuesta = client.post(f'/v1/documentos/{original["id"]}/anular', json={'razon': 'Error en el monto'})
    assert respuesta.status_code == 202, respuesta.text
    nota = respuesta.json()
    assert (nota['tipo'], nota['folio'], nota['monto_total'], nota['anula_id']) == (61, 1, 119000, original['id'])
    xml = etree.fromstring(client.get(f'/v1/documentos/{nota["id"]}/xml').content)
    assert xml.xpath('string(//*[local-name()="Referencia"]/*[local-name()="CodRef"])') == '1'
    # Una sola anulación por documento; la NC se anula con una ND
    assert client.post(f'/v1/documentos/{original["id"]}/anular').status_code == 409
    nd = client.post(f'/v1/documentos/{nota["id"]}/anular').json()
    assert nd['tipo'] == 56


def test_folios_sin_repetir_en_paralelo(client, empresa):
    folios, errores = [], []

    def emitir():
        respuesta = client.post(f'/v1/empresas/{RUT}/documentos', json=factura())
        (folios if respuesta.status_code == 202 else errores).append(respuesta.json())

    hilos = [threading.Thread(target=emitir) for _ in range(10)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    assert errores == []
    assert sorted(d['folio'] for d in folios) == list(range(1, 11))
    agotado = client.post(f'/v1/empresas/{RUT}/documentos', json=factura())
    assert agotado.status_code == 409


def test_token_correcto(client):
    assert client.headers['Authorization'] == f'Bearer {TOKEN}'
