"""Emisión, consulta, XML, PDF y anulación de documentos."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from ..models import Documento, Empresa
from ..schemas import AnulacionIn, DocumentoIn, documento_a_dict
from ..servicio import Servicio
from .deps import documento, empresa, servicio, sesion

router = APIRouter(tags=['documentos'])


@router.post('/empresas/{rut}/documentos', status_code=202)
def emitir(datos: DocumentoIn, response: Response, registro: Empresa = Depends(empresa),
           db: Session = Depends(sesion), srv: Servicio = Depends(servicio),
           idempotency_key: Optional[str] = Header(None, max_length=100)):
    """
    Emite un documento: asigna el folio, calcula totales, timbra y firma.
    El envío al SII es asíncrono; el estado se informa por webhook y en
    GET /documentos/{id}. Con la misma Idempotency-Key se devuelve el
    documento ya emitido (HTTP 200) en vez de emitir otro.
    """
    doc, creado = srv.emitir(db, registro, datos.documento, idempotency_key)
    if not creado:
        response.status_code = 200
    return documento_a_dict(doc)


@router.get('/documentos/{documento_id}')
def ver(doc: Documento = Depends(documento)):
    return documento_a_dict(doc)


@router.get('/documentos/{documento_id}/xml', response_class=Response,
            responses={200: {'content': {'application/xml': {}}}})
def xml(doc: Documento = Depends(documento)):
    return Response(doc.xml, media_type='application/xml; charset=ISO-8859-1', headers={
        'Content-Disposition': f'attachment; filename="T{doc.tipo}F{doc.folio}.xml"'})


@router.get('/documentos/{documento_id}/pdf', response_class=Response,
            responses={200: {'content': {'application/pdf': {}}}})
def pdf(doc: Documento = Depends(documento), formato: Literal['carta', '80mm'] = 'carta', cedible: bool = False,
        srv: Servicio = Depends(servicio)):
    """PDF en hoja carta o rollo térmico de 80 mm; cedible agrega la copia cedible."""
    return Response(srv.pdf(doc, formato, cedible), media_type='application/pdf', headers={
        'Content-Disposition': f'inline; filename="T{doc.tipo}F{doc.folio}.pdf"'})


@router.post('/documentos/{documento_id}/anular', status_code=202)
def anular(response: Response, datos: AnulacionIn = AnulacionIn(), doc: Documento = Depends(documento),
           db: Session = Depends(sesion), srv: Servicio = Depends(servicio),
           idempotency_key: Optional[str] = Header(None, max_length=100)):
    """
    Anula el documento emitiendo una nota de crédito (o de débito para una
    nota de crédito) con CodRef 1 por el total. Devuelve la nota emitida.
    """
    nota, creado = srv.anular(db, doc, datos.razon, idempotency_key)
    if not creado:
        response.status_code = 200
    return documento_a_dict(nota)
