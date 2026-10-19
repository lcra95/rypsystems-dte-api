"""
Fixtures: servicio con SQLite temporal (o PostgreSQL con
DTE_TEST_DATABASE_URL), certificado autofirmado y CAF falsos.
"""

import os

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, pkcs12
from fastapi.testclient import TestClient
from sqlalchemy import text

from rypsystems_dte.firma import SelfSignedCertificate
from rypsystems_dte.identifier import CafFaker

from app.config import Settings
from app.db import Base, Database
from app.main import create_app

TOKEN = 'token-de-prueba'
RUT = '76192083-9'
EMISOR = {'RznSoc': 'SASCO SpA', 'GiroEmis': 'Servicios informaticos', 'Acteco': 620900,
          'DirOrigen': 'Santa Cruz', 'CmnaOrigen': 'Santa Cruz'}
RECEPTOR = {'RUTRecep': '60803000-K', 'RznSocRecep': 'Servicio de Impuestos Internos', 'GiroRecep': 'Gobierno',
            'DirRecep': 'Teatinos 120', 'CmnaRecep': 'Santiago'}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=os.environ.get('DTE_TEST_DATABASE_URL') or f'sqlite:///{tmp_path / "dte.db"}',
        service_token=TOKEN, master_key=Fernet.generate_key().decode(), permitir_caf_falsos=True,
        webhook_url='https://principal.test/webhooks/dte', webhook_secret='secreto',
    )


@pytest.fixture
def database(settings):
    db = Database(settings.database_url)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    if db.engine.dialect.name == 'postgresql':
        with db.engine.begin() as conexion:
            conexion.execute(text('DROP TABLE IF EXISTS tareas, documentos, cafs, empresas, certificados CASCADE'))
    db.engine.dispose()


@pytest.fixture
def app(settings, database):
    return create_app(settings, database)


@pytest.fixture
def client(app):
    with TestClient(app, headers={'Authorization': f'Bearer {TOKEN}'}) as cliente:
        yield cliente


@pytest.fixture(scope='session')
def certificado():
    return SelfSignedCertificate().create()


@pytest.fixture(scope='session')
def pfx(certificado):
    return pkcs12.serialize_key_and_certificates(b'prueba', certificado._private_key, certificado._certificate, None,
                                                 BestAvailableEncryption(b'clave'))


def caf_xml(tipo, desde=1, hasta=10, rut=RUT):
    return CafFaker(rut=rut, tipo_documento=tipo, desde=desde, hasta=hasta).create().get_xml()


@pytest.fixture
def empresa(client, pfx):
    certificado = client.post('/v1/certificados', files={'archivo': ('firma.pfx', pfx)}, data={'clave': 'clave'})
    assert certificado.status_code == 201, certificado.text
    respuesta = client.post('/v1/empresas', json={
        'rut': RUT, 'emisor': EMISOR, 'fecha_resolucion': '2026-10-17', 'numero_resolucion': 0,
        'certificado_id': certificado.json()['id'],
    })
    assert respuesta.status_code == 201, respuesta.text
    for tipo in (33, 39, 56, 61):
        caf = client.post(f'/v1/empresas/{RUT}/cafs', files={'archivo': ('caf.xml', caf_xml(tipo))})
        assert caf.status_code == 201, caf.text
    return respuesta.json()


def factura(**extra):
    return {'documento': {
        'Encabezado': {'IdDoc': {'TipoDTE': 33, 'FmaPago': 2}, 'Receptor': dict(RECEPTOR)},
        'Detalle': [{'NmbItem': 'Desarrollo de software', 'QtyItem': 2, 'PrcItem': 50000}],
        **extra,
    }}


def pasar_a_produccion(app, rut=RUT):
    """Deja la empresa en producción por la base (la API no lo permite si ya tiene CAF)."""
    from app.models import Empresa
    with app.state.db.sesiones() as db:
        db.execute(Empresa.__table__.update().where(Empresa.rut == rut).values(ambiente='produccion'))
        db.commit()
