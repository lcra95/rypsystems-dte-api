"""Dependencias comunes: sesión, servicio, autenticación y búsquedas."""

from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Documento, Empresa
from ..schemas import normalizar_rut
from ..seguridad import token_valido
from ..servicio import NoEncontrado, Servicio


def sesion(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.sesion()


def servicio(request: Request) -> Servicio:
    return request.app.state.servicio


def autenticar(request: Request, authorization: str = Header('', include_in_schema=False)) -> None:
    esquema, _, token = authorization.partition(' ')
    if esquema.lower() != 'bearer' or not token_valido(token.strip(), request.app.state.settings.service_token):
        raise HTTPException(401, 'Token de servicio inválido.', headers={'WWW-Authenticate': 'Bearer'})


def empresa(rut: str, db: Session = Depends(sesion)) -> Empresa:
    try:
        rut = normalizar_rut(rut)
    except ValueError as e:
        raise HTTPException(422, str(e))
    registro = db.scalar(select(Empresa).where(Empresa.rut == rut))
    if registro is None:
        raise NoEncontrado(f'No existe la empresa {rut}.')
    return registro


def documento(documento_id: str, db: Session = Depends(sesion)) -> Documento:
    registro = db.get(Documento, documento_id)
    if registro is None:
        raise NoEncontrado(f'No existe el documento {documento_id}.')
    return registro
