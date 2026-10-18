"""Certificación de empresas ante el SII: sets de pruebas, libros, simulación, intercambio y muestras impresas."""

import base64
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..certificacion import Certificacion, documentos_del_envio
from ..models import Empresa, EnvioSii, SetPrueba
from ..schemas import (
    EmisionSetIn,
    SimulacionIn,
    envio_recibido_a_dict,
    envio_sii_a_dict,
    recibido_a_dict,
    set_prueba_a_dict,
)
from ..servicio import DatosInvalidos, NoEncontrado
from .deps import empresa, sesion
from .recibidos import recibidos

router = APIRouter(tags=['certificación'])


def certificacion(request: Request) -> Certificacion:
    return request.app.state.certificacion


def set_prueba(set_id: str, db: Session = Depends(sesion)) -> SetPrueba:
    registro = db.get(SetPrueba, set_id)
    if registro is None:
        raise NoEncontrado(f'No existe el set {set_id}.')
    return registro


@router.post('/empresas/{rut}/certificacion/sets', status_code=201)
async def cargar_sets(archivo: UploadFile = File(..., description='SIISetDePruebas<RUT>.txt o el set de boletas'),
                      tipos_cambio: Optional[str] = Form(None, description='JSON {"DOLAR USA": 950, ...} para '
                                                                           'exportación'),
                      registro: Empresa = Depends(empresa), db: Session = Depends(sesion),
                      cert: Certificacion = Depends(certificacion)):
    """Registra los sets de pruebas que entregó el SII. Los ya emitidos no se reemplazan."""
    try:
        cambios = json.loads(tipos_cambio) if tipos_cambio else None
    except json.JSONDecodeError as e:
        raise DatosInvalidos(f'tipos_cambio no es un JSON válido: {e}')
    sets = cert.cargar_sets(db, registro, await archivo.read(), cambios)
    return {'sets': [set_prueba_a_dict(s) for s in sets]}


@router.post('/empresas/{rut}/certificacion/simulacion', status_code=201)
def cargar_simulacion(datos: SimulacionIn, registro: Empresa = Depends(empresa), db: Session = Depends(sesion),
                      cert: Certificacion = Depends(certificacion)):
    """Registra la simulación: entre 20 y 100 documentos de todos los tipos certificados, con datos reales."""
    return set_prueba_a_dict(cert.cargar_simulacion(db, registro, datos.casos))


@router.get('/empresas/{rut}/certificacion')
def estado(registro: Empresa = Depends(empresa), db: Session = Depends(sesion)):
    """Sets de la certificación y el estado de su envío al SII."""
    sets = db.scalars(select(SetPrueba).where(SetPrueba.empresa_id == registro.id)
                      .order_by(SetPrueba.creado, SetPrueba.numero_atencion))
    return {'rut': registro.rut, 'ambiente': registro.ambiente, 'sets': [set_prueba_a_dict(s) for s in sets]}


@router.get('/certificacion/sets/{set_id}')
def ver_set(registro: SetPrueba = Depends(set_prueba), db: Session = Depends(sesion)):
    documentos = documentos_del_envio(db, registro.envio_id) if registro.envio_id else []
    return {**set_prueba_a_dict(registro, documentos), 'datos': registro.datos}


@router.post('/certificacion/sets/{set_id}/emitir', status_code=202)
def emitir_set(datos: EmisionSetIn = EmisionSetIn(), registro: SetPrueba = Depends(set_prueba),
               db: Session = Depends(sesion), cert: Certificacion = Depends(certificacion)):
    """
    Emite todos los casos del set en un envío (o genera el libro) y lo
    agenda para el SII. Los libros requieren que los sets en que se basan
    estén aceptados. Un set rechazado se puede volver a emitir.
    """
    cert.emitir(db, registro, datos.receptores)
    return set_prueba_a_dict(registro, documentos_del_envio(db, registro.envio_id))


@router.post('/empresas/{rut}/certificacion/intercambio')
async def intercambio(archivo: UploadFile = File(..., description='XML del set de intercambio entregado por el SII'),
                      nombre_contacto: Optional[str] = Form(None), mail_contacto: Optional[str] = Form(None),
                      registro: Empresa = Depends(empresa), db: Session = Depends(sesion),
                      rec=Depends(recibidos)):
    """
    Carga el envío del set de intercambio y genera las tres respuestas para
    subir al SII (base64): recepcion_envio, recibo_mercaderias y resultado_dte.
    Los timbres del set del SII no se validan (no verifican por diseño).
    """
    envio, documentos, _ = rec.cargar(db, registro, await archivo.read(), archivo.filename or 'ENVIO_DTE.xml',
                                      validar_timbre=False)
    archivos = rec.respuestas(db, envio, contacto={'nombre_contacto': nombre_contacto, 'mail_contacto': mail_contacto},
                              validar_timbre=False)
    return {
        'envio': envio_recibido_a_dict(envio), 'documentos': [recibido_a_dict(d) for d in documentos],
        'respuestas': {nombre: {'nombre': f'{nombre}_{envio.cod_envio}.xml',
                                'contenido': base64.b64encode(xml).decode()} for nombre, xml in archivos.items()},
    }


@router.get('/empresas/{rut}/certificacion/muestras', response_class=Response,
            responses={200: {'content': {'application/zip': {}}}})
def muestras(registro: Empresa = Depends(empresa), db: Session = Depends(sesion),
             cert: Certificacion = Depends(certificacion)):
    """ZIP con un PDF por documento y por copia cedible, para la etapa de documentos impresos."""
    return Response(cert.muestras(db, registro), media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="muestras_{registro.rut}.zip"'})


@router.get('/envios/{envio_id}', tags=['documentos'])
def ver_envio(envio_id: str, db: Session = Depends(sesion)):
    envio = db.get(EnvioSii, envio_id)
    if envio is None:
        raise NoEncontrado(f'No existe el envío {envio_id}.')
    return envio_sii_a_dict(envio)
