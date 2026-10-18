"""Tests de la certificación: sets del SII, libros, simulación, intercambio y muestras impresas."""

import base64
import io
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from app.models import Tarea, ahora
from app.tareas import Procesador

from .conftest import RUT, caf_xml
from .test_recibidos import envio_proveedor  # noqa: F401 - fixture

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
TIPOS = (34, 43, 46, 52, 110, 111, 112)


class DteServiceFalso:
    def __init__(self, sii):
        self.sii = sii

    def upload_dte(self, rut_envia, rut, xml):
        self.sii.libros.append(xml)
        return {'track_id': 9000 + len(self.sii.libros)}


class SiiFalso:
    def __init__(self, certificado, ambiente):
        self.certificado = certificado
        self.envios, self.libros = [], []
        self.respuestas = {}  # track_id -> consulta

    def enviar(self, envio, rut):
        self.envios.append(envio)
        return 8000 + len(self.envios)

    def servicio_dte(self):
        return DteServiceFalso(self)

    def consultar_envio(self, track_id, rut, boletas=False):
        return self.respuestas.get(int(track_id)) or {
            'estado': 'EPR', 'final': True, 'informados': 1, 'aceptados': 1, 'rechazados': 0, 'reparos': 0,
            'detalle': []}


@pytest.fixture
def sii():
    return SiiFalso(None, None)


@pytest.fixture
def worker(app, settings, database, sii):
    def fabrica(certificado, ambiente):
        sii.certificado = certificado
        return sii

    procesador = Procesador(settings, app.state.servicio, cliente_sii=fabrica, http_post=lambda *a, **k: type(
        'R', (), {'status_code': 200})())

    def correr():
        for _ in range(4):
            with database.sesiones() as s:
                s.execute(update(Tarea).where(Tarea.estado == 'pendiente')
                          .values(ejecutar_en=ahora() - timedelta(seconds=1)))
                s.commit()
                procesador.procesar_pendientes(s)
        return sii
    return correr


@pytest.fixture
def certificando(client, empresa):
    for tipo in TIPOS:
        assert client.post(f'/v1/empresas/{RUT}/cafs', files={'archivo': ('c.xml', caf_xml(tipo, 1, 50))}).status_code \
            == 201
    respuesta = client.post(f'/v1/empresas/{RUT}/certificacion/sets',
                            files={'archivo': ('set.txt', (FIXTURES / 'SIISetDePruebas785325664.txt').read_bytes())})
    assert respuesta.status_code == 201, respuesta.text
    return {s['tipo_set'] + (f'_{s["numero_atencion"]}' if s['tipo_set'] == 'exportacion' else ''): s
            for s in respuesta.json()['sets']}


def emitir(client, set_id, codigo=202):
    respuesta = client.post(f'/v1/certificacion/sets/{set_id}/emitir')
    assert respuesta.status_code == codigo, respuesta.text
    return respuesta.json()


def test_carga_los_sets(client, certificando):
    assert set(certificando) == {'basico', 'libro_ventas', 'libro_compras', 'guia', 'libro_guias', 'exenta',
                                 'exportacion_5136570', 'exportacion_5136571', 'liquidacion', 'factura_compra'}
    assert certificando['basico']['casos'] == 8
    estado = client.get(f'/v1/empresas/{RUT}/certificacion').json()
    assert len(estado['sets']) == 10 and all(s['envio'] is None for s in estado['sets'])
    # Cargar de nuevo no duplica
    client.post(f'/v1/empresas/{RUT}/certificacion/sets',
                files={'archivo': ('set.txt', (FIXTURES / 'SIISetDePruebas785325664.txt').read_bytes())})
    assert len(client.get(f'/v1/empresas/{RUT}/certificacion').json()['sets']) == 10
    assert client.post(f'/v1/empresas/{RUT}/certificacion/sets',
                       files={'archivo': ('x.txt', b'nada')}).status_code == 422


def test_sets_libros_y_muestras(client, certificando, worker):
    # Los libros necesitan los sets aceptados
    emitir(client, certificando['libro_ventas']['id'], 409)

    basico = emitir(client, certificando['basico']['id'])
    assert len(basico['documentos']) == 8 and basico['envio']['tipo'] == 'dte'
    assert [d['caso'] for d in basico['documentos']][:2] == ['5136564-1', '5136564-2']
    sii = worker()
    assert len(sii.envios) == 1 and sii.envios[0].count(b'<DTE ') == 8
    estado = client.get(f'/v1/certificacion/sets/{certificando["basico"]["id"]}').json()
    assert estado['envio']['estado'] == 'aceptado'
    assert {d['estado'] for d in estado['documentos']} == {'aceptado'}
    emitir(client, certificando['basico']['id'], 409)  # ya aceptado: no se reemite

    for nombre in ('guia', 'exenta', 'exportacion_5136570', 'exportacion_5136571', 'liquidacion', 'factura_compra'):
        emitir(client, certificando[nombre]['id'])
    sii = worker()
    assert len(sii.envios) == 7

    for libro in ('libro_ventas', 'libro_compras', 'libro_guias'):
        assert emitir(client, certificando[libro]['id'])['envio']['tipo'] == 'libro'
    sii.respuestas.update({9001: {'estado': 'LOK', 'final': False, 'informados': 0, 'aceptados': 0,
                                  'rechazados': 0, 'reparos': 0, 'detalle': []}})
    sii = worker()
    assert len(sii.libros) == 3 and b'<TipoOperacion>VENTA' in sii.libros[0] and b'<LibroGuia' in sii.libros[2]
    libros = {s['tipo_set']: s for s in client.get(f'/v1/empresas/{RUT}/certificacion').json()['sets']}
    assert libros['libro_ventas']['envio']['estado'] == 'aceptado'
    # Sin respuesta LOK el libro queda rechazado (estado EPR no es de libros)
    assert libros['libro_compras']['envio']['estado'] == 'rechazado'

    zip_ = client.get(f'/v1/empresas/{RUT}/certificacion/muestras')
    assert zip_.status_code == 200
    nombres = zipfile.ZipFile(io.BytesIO(zip_.content)).namelist()
    # 8 + 3 + 8 + 3 + 3 + 4 + 3 = 32 documentos; copias cedibles de facturas y guías de venta
    assert sum(1 for n in nombres if not n.endswith('_cedible.pdf')) == 32
    assert 'T33_F1_cedible.pdf' in nombres and 'T61_F1_cedible.pdf' not in nombres


def test_resultado_por_tipo_en_envio_con_reparos(client, certificando, worker, sii):
    # QueryEstUp resume por tipo: las facturas sin problemas quedan aceptadas y las notas de crédito, por revisar.
    sii.respuestas[8001] = {'estado': 'EPR', 'final': True, 'informados': 8, 'aceptados': 7, 'rechazados': 0,
                            'reparos': 1, 'detalle': [
                                {'tipo_docto': '33', 'informados': '4', 'aceptados': '4', 'rechazados': '0',
                                 'reparos': '0'},
                                {'tipo_docto': '61', 'informados': '3', 'aceptados': '2', 'rechazados': '0',
                                 'reparos': '1'},
                                {'tipo_docto': '56', 'informados': '1', 'aceptados': '1', 'rechazados': '0',
                                 'reparos': '0'}]}
    emitir(client, certificando['basico']['id'])
    worker()
    estado = client.get(f'/v1/certificacion/sets/{certificando["basico"]["id"]}').json()
    assert estado['envio']['estado'] == 'aceptado_reparos'
    assert {(d['tipo'], d['estado']) for d in estado['documentos']} == {(33, 'aceptado'), (61, 'revisar'),
                                                                        (56, 'aceptado')}


def test_simulacion(client, certificando, worker):
    client.post(f'/v1/empresas/{RUT}/cafs', files={'archivo': ('c.xml', caf_xml(33, 11, 50))})
    casos = [{'caso': f'SIM-{i}', 'tipo': 33, 'detalle': [{'NmbItem': f'Servicio {i}', 'QtyItem': 1, 'PrcItem': 1000 * i}]}
             for i in range(1, 20)]
    assert client.post(f'/v1/empresas/{RUT}/certificacion/simulacion', json={'casos': casos}).status_code == 422
    casos.append({'caso': 'SIM-20', 'tipo': 61, 'referencia': {'caso': 'SIM-1', 'CodRef': 1, 'RazonRef': 'ANULA'},
                  'detalle': 'copiar'})
    simulacion = client.post(f'/v1/empresas/{RUT}/certificacion/simulacion', json={'casos': casos}).json()
    resultado = emitir(client, simulacion['id'])
    assert len(resultado['documentos']) == 20
    worker()
    estado = client.get(f'/v1/certificacion/sets/{simulacion["id"]}').json()
    assert estado['envio']['estado'] == 'aceptado'


def test_set_de_boletas(client, empresa, worker):
    respuesta = client.post(f'/v1/empresas/{RUT}/certificacion/sets',
                            files={'archivo': ('be.txt', (FIXTURES / 'set_prueba_boleta.txt').read_bytes())})
    boletas = respuesta.json()['sets'][0]
    assert boletas['tipo_set'] == 'boletas' and boletas['casos'] == 5
    resultado = emitir(client, boletas['id'])
    assert resultado['envio']['tipo'] == 'boletas' and len(resultado['documentos']) == 5
    sii = worker()
    assert b'<EnvioBOLETA' in sii.envios[0] and b'<RazonRef>CASO-1</RazonRef>' in sii.envios[0]


def test_intercambio(client, empresa, envio_proveedor):  # noqa: F811
    respuesta = client.post(f'/v1/empresas/{RUT}/certificacion/intercambio',
                            files={'archivo': ('ENVIO_DTE_1.xml', envio_proveedor)})
    assert respuesta.status_code == 200, respuesta.text
    datos = respuesta.json()
    assert set(datos['respuestas']) == {'recepcion_envio', 'resultado_dte', 'recibo_mercaderias'}
    assert b'<RecepcionEnvio>' in base64.b64decode(datos['respuestas']['recepcion_envio']['contenido'])


def test_solo_en_certificacion(client, certificando):
    client.patch(f'/v1/empresas/{RUT}', json={'ambiente': 'produccion'})
    respuesta = client.post(f'/v1/certificacion/sets/{certificando["basico"]["id"]}/emitir')
    assert respuesta.status_code == 409 and 'certificación' in respuesta.json()['detail']
