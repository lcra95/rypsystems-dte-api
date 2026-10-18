"""Documentos recibidos de proveedores: carga, respuestas del intercambio y aceptación o reclamo en el SII."""

import base64
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from lxml import etree
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rypsystems_dte import DtePdf

from ..models import DocumentoRecibido, Empresa, EnvioRecibido
from ..recibidos import Recibidos
from ..schemas import (
    AceptacionIn,
    ReclamoIn,
    RespuestaIntercambioIn,
    envio_recibido_a_dict,
    recibido_a_dict,
)
from ..servicio import NoEncontrado
from .deps import empresa, sesion

router = APIRouter(tags=['recibidos'])


def recibidos(request: Request) -> Recibidos:
    return request.app.state.recibidos


def recibido(recibido_id: str, db: Session = Depends(sesion)) -> DocumentoRecibido:
    registro = db.get(DocumentoRecibido, recibido_id)
    if registro is None:
        raise NoEncontrado(f'No existe el documento recibido {recibido_id}.')
    return registro


def envio(envio_id: str, db: Session = Depends(sesion)) -> EnvioRecibido:
    registro = db.get(EnvioRecibido, envio_id)
    if registro is None:
        raise NoEncontrado(f'No existe el envío recibido {envio_id}.')
    return registro


@router.post('/empresas/{rut}/recibidos', status_code=201)
async def cargar(archivo: UploadFile = File(..., description='EnvioDTE recibido del proveedor'),
                 validar_timbre: bool = Form(True), registro: Empresa = Depends(empresa),
                 db: Session = Depends(sesion), rec: Recibidos = Depends(recibidos)):
    """
    Carga un EnvioDTE recibido. Valida esquema, firmas, RUT receptor y
    timbres; los documentos que no pasan quedan en estado no_recibido.
    """
    envio_db, nuevos, repetidos = rec.cargar(db, registro, await archivo.read(), archivo.filename or 'EnvioDTE.xml',
                                             validar_timbre)
    return {'envio': envio_recibido_a_dict(envio_db), 'documentos': [recibido_a_dict(d) for d in nuevos],
            'repetidos': repetidos}


@router.get('/empresas/{rut}/recibidos')
def listar(registro: Empresa = Depends(empresa), db: Session = Depends(sesion), estado: Optional[str] = None,
           tipo: Optional[int] = None, rut_emisor: Optional[str] = None,
           limite: int = Query(50, ge=1, le=500), desplazamiento: int = Query(0, ge=0)):
    filtros = [DocumentoRecibido.empresa_id == registro.id]
    if estado:
        filtros.append(DocumentoRecibido.estado == estado)
    if tipo:
        filtros.append(DocumentoRecibido.tipo == tipo)
    if rut_emisor:
        filtros.append(DocumentoRecibido.rut_emisor == rut_emisor.replace('.', '').upper())
    total = db.scalar(select(func.count()).select_from(DocumentoRecibido).where(*filtros))
    documentos = db.scalars(select(DocumentoRecibido).where(*filtros).order_by(DocumentoRecibido.creado.desc())
                            .limit(limite).offset(desplazamiento))
    return {'total': total, 'documentos': [recibido_a_dict(d) for d in documentos]}


@router.get('/recibidos/{recibido_id}')
def ver(doc: DocumentoRecibido = Depends(recibido)):
    return recibido_a_dict(doc)


@router.get('/recibidos/{recibido_id}/xml', response_class=Response,
            responses={200: {'content': {'application/xml': {}}}})
def xml(doc: DocumentoRecibido = Depends(recibido)):
    return Response(doc.xml, media_type='application/xml; charset=ISO-8859-1', headers={
        'Content-Disposition': f'attachment; filename="{doc.rut_emisor}_T{doc.tipo}F{doc.folio}.xml"'})


@router.get('/recibidos/{recibido_id}/pdf', response_class=Response,
            responses={200: {'content': {'application/pdf': {}}}})
def pdf(doc: DocumentoRecibido = Depends(recibido)):
    # La resolución impresa es la del emisor, que viene en la carátula del envío.
    caratula = etree.fromstring(doc.envio.xml).xpath('//*[local-name()="Caratula"]')[0]
    resolucion = {'fecha': caratula.xpath('string(./*[local-name()="FchResol"])'),
                  'numero': int(caratula.xpath('string(./*[local-name()="NroResol"])') or 0)}
    return Response(DtePdf(resolucion).render(doc.xml, cedible=False), media_type='application/pdf', headers={
        'Content-Disposition': f'inline; filename="{doc.rut_emisor}_T{doc.tipo}F{doc.folio}.pdf"'})


@router.post('/recibidos/{recibido_id}/aceptar')
def aceptar(datos: AceptacionIn = AceptacionIn(), doc: DocumentoRecibido = Depends(recibido),
            db: Session = Depends(sesion), rec: Recibidos = Depends(recibidos)):
    """Registra en el SII la aceptación del contenido (ACD) o el recibo de mercaderías (ERM)."""
    rec.registrar(db, doc, 'ERM' if datos.recibo_mercaderias else 'ACD')
    return recibido_a_dict(doc)


@router.post('/recibidos/{recibido_id}/reclamar')
def reclamar(datos: ReclamoIn = ReclamoIn(), doc: DocumentoRecibido = Depends(recibido),
             db: Session = Depends(sesion), rec: Recibidos = Depends(recibidos)):
    """Registra en el SII el reclamo del documento."""
    rec.registrar(db, doc, datos.accion)
    return recibido_a_dict(doc)


@router.get('/recibidos/{recibido_id}/eventos')
def eventos(doc: DocumentoRecibido = Depends(recibido), db: Session = Depends(sesion),
            rec: Recibidos = Depends(recibidos)):
    """Eventos del documento en el registro del SII (y fecha de recepción, desde la que corre el plazo)."""
    respuesta = rec.eventos(db, doc)
    return {**respuesta, 'documento': recibido_a_dict(doc)}


@router.get('/envios-recibidos/{envio_id}')
def ver_envio(registro: EnvioRecibido = Depends(envio), db: Session = Depends(sesion)):
    documentos = db.scalars(select(DocumentoRecibido).where(DocumentoRecibido.envio_id == registro.id))
    return {'envio': envio_recibido_a_dict(registro), 'documentos': [recibido_a_dict(d) for d in documentos]}


@router.post('/envios-recibidos/{envio_id}/respuesta')
def respuesta(datos: RespuestaIntercambioIn = RespuestaIntercambioIn(), registro: EnvioRecibido = Depends(envio),
              db: Session = Depends(sesion), rec: Recibidos = Depends(recibidos)):
    """
    Genera las respuestas del intercambio para el proveedor (en base64):
    recepcion_envio, resultado_dte y recibo_mercaderias. El sistema
    principal las envía por correo al proveedor.
    """
    archivos = rec.respuestas(db, registro, datos.rechazar, datos.recinto, {
        'nombre_contacto': datos.nombre_contacto, 'fono_contacto': datos.fono_contacto,
        'mail_contacto': datos.mail_contacto}, datos.validar_timbre)
    return {nombre: {'nombre': f'{nombre}_{registro.cod_envio}.xml',
                     'contenido': base64.b64encode(xml).decode()} for nombre, xml in archivos.items()}

