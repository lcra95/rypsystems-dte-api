"""Empresas emisoras, certificados y CAF."""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Caf, Certificado, Documento, Empresa
from ..schemas import (
    AjusteFolioIn,
    EmpresaIn,
    EmpresaUpdate,
    caf_a_dict,
    certificado_a_dict,
    documento_a_dict,
    empresa_a_dict,
)
from ..servicio import Conflicto, NoEncontrado, Servicio
from .deps import empresa, servicio, sesion

router = APIRouter()


def _certificado(db: Session, certificado_id: Optional[str]) -> None:
    if certificado_id and db.get(Certificado, certificado_id) is None:
        raise NoEncontrado(f'No existe el certificado {certificado_id}.')


# --- Certificados ---------------------------------------------------------

@router.post('/certificados', status_code=201, tags=['certificados'])
async def subir_certificado(archivo: UploadFile = File(..., description='.pfx o .p12'),
                            clave: str = Form(...), db: Session = Depends(sesion),
                            srv: Servicio = Depends(servicio)):
    """Registra un certificado digital. El archivo y su clave se guardan cifrados."""
    registro = srv.registrar_certificado(db, await archivo.read(), clave)
    db.commit()
    return certificado_a_dict(registro)


@router.get('/certificados', tags=['certificados'])
def listar_certificados(db: Session = Depends(sesion)):
    return [certificado_a_dict(c) for c in db.scalars(select(Certificado).order_by(Certificado.creado))]


# --- Empresas ---------------------------------------------------------------

@router.post('/empresas', status_code=201, tags=['empresas'])
def crear_empresa(datos: EmpresaIn, db: Session = Depends(sesion)):
    if db.scalar(select(Empresa).where(Empresa.rut == datos.rut)):
        raise Conflicto(f'La empresa {datos.rut} ya existe.')
    _certificado(db, datos.certificado_id)
    registro = Empresa(**datos.model_dump())
    db.add(registro)
    db.commit()
    return empresa_a_dict(registro)


@router.get('/empresas/{rut}', tags=['empresas'])
def ver_empresa(registro: Empresa = Depends(empresa)):
    return empresa_a_dict(registro)


@router.patch('/empresas/{rut}', tags=['empresas'])
def actualizar_empresa(datos: EmpresaUpdate, registro: Empresa = Depends(empresa), db: Session = Depends(sesion)):
    cambios = datos.model_dump(exclude_unset=True)
    _certificado(db, cambios.get('certificado_id'))
    for campo, valor in cambios.items():
        setattr(registro, campo, valor)
    db.commit()
    return empresa_a_dict(registro)


# --- CAF y folios -----------------------------------------------------------

@router.post('/empresas/{rut}/cafs', status_code=201, tags=['folios'])
async def subir_caf(archivo: UploadFile = File(..., description='XML del CAF descargado del SII'),
                    registro: Empresa = Depends(empresa), db: Session = Depends(sesion),
                    srv: Servicio = Depends(servicio)):
    caf = srv.registrar_caf(db, registro, await archivo.read())
    db.commit()
    return caf_a_dict(caf)


@router.get('/empresas/{rut}/folios', tags=['folios'])
def folios(registro: Empresa = Depends(empresa), db: Session = Depends(sesion)):
    """Folios disponibles por tipo y detalle de cada CAF."""
    cafs = [caf_a_dict(c) for c in db.scalars(select(Caf).where(Caf.empresa_id == registro.id)
                                               .order_by(Caf.tipo, Caf.desde))]
    resumen = {}
    for caf in cafs:
        resumen.setdefault(caf['tipo'], 0)
        resumen[caf['tipo']] += caf['disponibles']
    return {'disponibles': resumen, 'cafs': cafs}


@router.patch('/empresas/{rut}/cafs/{caf_id}', tags=['folios'])
def ajustar_caf(caf_id: int, datos: AjusteFolioIn, registro: Empresa = Depends(empresa),
                db: Session = Depends(sesion), srv: Servicio = Depends(servicio)):
    """Avanza el próximo folio de un CAF (p. ej. folios ya usados en otro sistema)."""
    caf = db.get(Caf, caf_id)
    if caf is None or caf.empresa_id != registro.id:
        raise NoEncontrado(f'No existe el CAF {caf_id} de {registro.rut}.')
    srv.ajustar_siguiente_folio(db, caf, datos.siguiente)
    db.commit()
    return caf_a_dict(caf)


# --- Documentos de la empresa -------------------------------------------------

@router.get('/empresas/{rut}/documentos', tags=['documentos'])
def listar_documentos(registro: Empresa = Depends(empresa), db: Session = Depends(sesion),
                      tipo: Optional[int] = None, estado: Optional[str] = None, folio: Optional[int] = None,
                      limite: int = Query(50, ge=1, le=500), desplazamiento: int = Query(0, ge=0)):
    filtros = [Documento.empresa_id == registro.id]
    if tipo:
        filtros.append(Documento.tipo == tipo)
    if estado:
        filtros.append(Documento.estado == estado)
    if folio:
        filtros.append(Documento.folio == folio)
    total = db.scalar(select(func.count()).select_from(Documento).where(*filtros))
    documentos = db.scalars(select(Documento).where(*filtros).order_by(Documento.creado.desc())
                            .limit(limite).offset(desplazamiento))
    return {'total': total, 'documentos': [documento_a_dict(d) for d in documentos]}
