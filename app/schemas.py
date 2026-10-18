"""Esquemas de entrada y salida de la API."""

import re
from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from .models import Caf, Certificado, Documento, Empresa

RUT = r'^\d{1,8}-[\dK]$'


def validar_emisor(valor: Dict[str, Any]) -> Dict[str, Any]:
    faltan = [c for c in ('RznSoc', 'GiroEmis', 'Acteco', 'DirOrigen', 'CmnaOrigen') if not valor.get(c)]
    if faltan:
        raise ValueError(f'Faltan datos del emisor: {", ".join(faltan)}')
    valor = dict(valor)
    valor.pop('RUTEmisor', None)  # el RUT es el de la empresa
    return valor


class EmpresaIn(BaseModel):
    rut: str = Field(pattern=RUT, examples=['78532566-4'])
    emisor: Dict[str, Any] = Field(description='Nodo Emisor del DTE: RznSoc, GiroEmis, Acteco, DirOrigen, '
                                               'CmnaOrigen y opcionales (Telefono, CorreoEmisor, CdgSIISucur...)')
    fecha_resolucion: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    numero_resolucion: int = 0
    ambiente: Literal['certificacion', 'produccion'] = 'certificacion'
    certificado_id: Optional[str] = None

    @field_validator('rut', mode='before')
    @classmethod
    def _rut(cls, valor: str) -> str:
        return str(valor).replace('.', '').strip().upper()

    @field_validator('emisor')
    @classmethod
    def _emisor(cls, valor: Dict[str, Any]) -> Dict[str, Any]:
        return validar_emisor(valor)


class EmpresaUpdate(BaseModel):
    emisor: Optional[Dict[str, Any]] = None
    fecha_resolucion: Optional[str] = Field(None, pattern=r'^\d{4}-\d{2}-\d{2}$')
    numero_resolucion: Optional[int] = None
    ambiente: Optional[Literal['certificacion', 'produccion']] = None
    certificado_id: Optional[str] = None

    @field_validator('emisor')
    @classmethod
    def _emisor(cls, valor: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return None if valor is None else validar_emisor(valor)


class DocumentoIn(BaseModel):
    documento: Dict[str, Any] = Field(description='Encabezado (IdDoc.TipoDTE, Receptor...), Detalle y opcionales '
                                                  'Referencia, DscRcgGlobal... Sin Emisor ni Folio.')


class AnulacionIn(BaseModel):
    razon: str = Field('ANULA DOCUMENTO', max_length=90)


class AjusteFolioIn(BaseModel):
    siguiente: int = Field(ge=1)


def empresa_a_dict(empresa: Empresa) -> Dict[str, Any]:
    return {
        'rut': empresa.rut, 'emisor': empresa.emisor, 'fecha_resolucion': empresa.fecha_resolucion,
        'numero_resolucion': empresa.numero_resolucion, 'ambiente': empresa.ambiente,
        'certificado_id': empresa.certificado_id, 'creado': empresa.creado, 'actualizado': empresa.actualizado,
    }


def certificado_a_dict(certificado: Certificado) -> Dict[str, Any]:
    return {'id': certificado.id, 'rut': certificado.rut, 'nombre': certificado.nombre, 'vence': certificado.vence,
            'creado': certificado.creado}


def caf_a_dict(caf: Caf) -> Dict[str, Any]:
    return {'id': caf.id, 'tipo': caf.tipo, 'desde': caf.desde, 'hasta': caf.hasta, 'siguiente': caf.siguiente,
            'disponibles': max(caf.hasta - caf.siguiente + 1, 0), 'vence': caf.vence}


def documento_a_dict(documento: Documento) -> Dict[str, Any]:
    return {
        'id': documento.id, 'rut_emisor': documento.empresa.rut, 'tipo': documento.tipo, 'folio': documento.folio,
        'fecha': documento.fecha, 'receptor': documento.receptor, 'monto_total': documento.monto_total,
        'estado': documento.estado, 'track_id': documento.track_id, 'detalle_sii': documento.detalle_sii,
        'anula_id': documento.anula_id, 'creado': _iso(documento.creado), 'actualizado': _iso(documento.actualizado),
    }


def _iso(valor: Optional[datetime]) -> Optional[str]:
    return valor.isoformat() if valor else None


def normalizar_rut(rut: str) -> str:
    rut = rut.replace('.', '').strip().upper()
    if not re.match(RUT, rut):
        raise ValueError(f'RUT inválido: {rut}')
    return rut

