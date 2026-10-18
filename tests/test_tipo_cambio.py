"""Tipo de cambio en la emisión de exportación desde el servicio."""

import pytest

from app.config import Settings
from app.main import create_app
from rypsystems_dte import TiposCambioFijos

from .conftest import RUT, caf_xml

EXPORTACION = {'documento': {
    'Encabezado': {'IdDoc': {'TipoDTE': 110, 'IndServicio': 3},
                   'Receptor': {'RUTRecep': '55555555-5', 'RznSocRecep': 'Northwind Inc.',
                                'Extranjero': {'Nacionalidad': 225}, 'GiroRecep': 'Software',
                                'DirRecep': 'San Francisco'},
                   'Totales': {'TpoMoneda': 'DOLAR USA'}},
    'Detalle': [{'NmbItem': 'Desarrollo', 'QtyItem': 10, 'PrcItem': 45}],
    'Referencia': [{'TpoDocRef': 812, 'FolioRef': 1, 'RazonRef': 'RESOLUCION SNA'}],
}}


@pytest.fixture
def exportadora(client, empresa):
    assert client.post(f'/v1/empresas/{RUT}/cafs', files={'archivo': ('c.xml', caf_xml(110))}).status_code == 201


def otra_moneda(client, doc):
    return client.get(f'/v1/documentos/{doc["id"]}/xml').content.split(b'<OtraMoneda>')[1].split(b'</OtraMoneda>')[0]


def test_certificacion_usa_referenciales(client, exportadora):
    doc = client.post(f'/v1/empresas/{RUT}/documentos', json=EXPORTACION).json()
    assert b'<TpoCambio>950</TpoCambio>' in otra_moneda(client, doc)
    consulta = client.get(f'/v1/empresas/{RUT}/tipo-cambio', params={'moneda': 'DOLAR USA', 'fecha': '2026-10-19'})
    assert consulta.json()['tipo_cambio'] == 950 and consulta.json()['fuente'] == 'TiposCambioFijos'


def test_produccion_requiere_banco_central(client, exportadora, app):
    client.patch(f'/v1/empresas/{RUT}', json={'ambiente': 'produccion'})
    respuesta = client.post(f'/v1/empresas/{RUT}/documentos', json=EXPORTACION)
    assert respuesta.status_code == 422 and 'DTE_BCCH_TOKEN' in respuesta.json()['detail']
    assert client.get(f'/v1/empresas/{RUT}/tipo-cambio', params={'moneda': 'DOLAR USA'}).status_code == 409
    # Con TpoCambio informado se emite igual (el CAF falso no se valida contra el ambiente)
    con_cambio = {'documento': {**EXPORTACION['documento'], 'Encabezado': {
        **EXPORTACION['documento']['Encabezado'], 'OtraMoneda': {'TpoMoneda': 'PESO CL', 'TpoCambio': 948.75}}}}
    doc = client.post(f'/v1/empresas/{RUT}/documentos', json=con_cambio)
    assert doc.status_code == 202, doc.text
    assert b'<TpoCambio>948.75</TpoCambio>' in otra_moneda(client, doc.json())


def test_banco_central_configurado(client, exportadora, app):
    app.state.servicio.banco_central = TiposCambioFijos({'DOLAR USA': 948.75})
    client.patch(f'/v1/empresas/{RUT}', json={'ambiente': 'produccion'})
    doc = client.post(f'/v1/empresas/{RUT}/documentos', json=EXPORTACION).json()
    assert b'<TpoCambio>948.75</TpoCambio>' in otra_moneda(client, doc)


def test_configuracion_del_banco_central(settings, database):
    con_token = Settings(**{**settings.__dict__, 'bcch_token': 'TOKEN', 'bcch_series': {'EURO': 'SERIE.EUR'}})
    banco = create_app(con_token, database).state.servicio.banco_central
    assert banco.credenciales == {'token': 'TOKEN'} and banco.series['EURO'] == 'SERIE.EUR'
    assert create_app(settings, database).state.servicio.banco_central is None
