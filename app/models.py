"""
Modelo de datos del servicio DTE.

Solo guarda el estado técnico de la facturación (empresas emisoras,
certificados, CAF, documentos y tareas pendientes con el SII). Clientes,
cobros y reglas de negocio viven en el sistema principal.

Certificados, sus contraseñas y los CAF (que contienen la clave privada de
timbraje) se guardan cifrados con la clave maestra.
"""

import uuid
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def nuevo_id() -> str:
    return str(uuid.uuid4())


class Certificado(Base):
    """Certificado digital (.pfx) con el que se firman y envían los documentos."""

    __tablename__ = 'certificados'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nuevo_id)
    rut: Mapped[str] = mapped_column(String(12))
    nombre: Mapped[str] = mapped_column(String(200))
    vence: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pfx_cifrado: Mapped[bytes] = mapped_column(LargeBinary)
    clave_cifrada: Mapped[bytes] = mapped_column(LargeBinary)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class Empresa(Base):
    """Empresa emisora (un RUT) y su autorización del SII."""

    __tablename__ = 'empresas'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rut: Mapped[str] = mapped_column(String(12), unique=True)
    # Nodo Emisor del DTE (RznSoc, GiroEmis, Acteco, DirOrigen, CmnaOrigen...).
    emisor: Mapped[Dict[str, Any]] = mapped_column(JSON)
    fecha_resolucion: Mapped[str] = mapped_column(String(10))
    numero_resolucion: Mapped[int] = mapped_column(Integer, default=0)
    ambiente: Mapped[str] = mapped_column(String(20), default='certificacion')
    certificado_id: Mapped[Optional[str]] = mapped_column(ForeignKey('certificados.id'), nullable=True)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, onupdate=ahora)

    certificado: Mapped[Optional[Certificado]] = relationship()


class Caf(Base):
    """Código de autorización de folios. `siguiente` es el próximo folio libre."""

    __tablename__ = 'cafs'
    __table_args__ = (UniqueConstraint('empresa_id', 'tipo', 'desde'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    empresa_id: Mapped[int] = mapped_column(ForeignKey('empresas.id'), index=True)
    tipo: Mapped[int] = mapped_column(Integer)
    desde: Mapped[int] = mapped_column(Integer)
    hasta: Mapped[int] = mapped_column(Integer)
    siguiente: Mapped[int] = mapped_column(Integer)
    vence: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    xml_cifrado: Mapped[bytes] = mapped_column(LargeBinary)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


ESTADOS_DOCUMENTO = (
    'emitido',           # timbrado y firmado, por enviar
    'enviado',           # recibido por el SII (con track id), en revisión
    'aceptado',
    'aceptado_reparos',
    'rechazado',
    'revisar',           # envío de varios documentos con reparos o rechazos en su tipo (ver el envío)
    'error',             # no se pudo enviar o consultar (ver detalle)
)


class EnvioSii(Base):
    """
    Envío al SII: un sobre con uno o varios documentos (EnvioDTE o
    EnvioBOLETA) o un libro. Tiene su track id y el resultado del SII.
    """

    __tablename__ = 'envios_sii'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nuevo_id)
    empresa_id: Mapped[int] = mapped_column(ForeignKey('empresas.id'), index=True)
    tipo: Mapped[str] = mapped_column(String(10), default='dte')  # dte, boletas, libro
    estado: Mapped[str] = mapped_column(String(20), default='pendiente')
    xml: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    track_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    detalle_sii: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, onupdate=ahora)

    empresa: Mapped[Empresa] = relationship()


class SetPrueba(Base):
    """Set de la certificación de una empresa: set de pruebas del SII, libro o simulación."""

    __tablename__ = 'sets_prueba'

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nuevo_id)
    empresa_id: Mapped[int] = mapped_column(ForeignKey('empresas.id'), index=True)
    numero_atencion: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tipo_set: Mapped[str] = mapped_column(String(20))
    titulo: Mapped[str] = mapped_column(String(120))
    datos: Mapped[Dict[str, Any]] = mapped_column(JSON)
    envio_id: Mapped[Optional[str]] = mapped_column(ForeignKey('envios_sii.id'), nullable=True)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)

    empresa: Mapped[Empresa] = relationship()
    envio: Mapped[Optional[EnvioSii]] = relationship()


class Documento(Base):
    __tablename__ = 'documentos'
    __table_args__ = (
        UniqueConstraint('empresa_id', 'tipo', 'folio'),
        UniqueConstraint('empresa_id', 'idempotency_key'),
        Index('ix_documentos_empresa_estado', 'empresa_id', 'estado'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nuevo_id)
    empresa_id: Mapped[int] = mapped_column(ForeignKey('empresas.id'))
    tipo: Mapped[int] = mapped_column(Integer)
    folio: Mapped[int] = mapped_column(Integer)
    fecha: Mapped[str] = mapped_column(String(10))
    receptor: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    monto_total: Mapped[int] = mapped_column(Integer, default=0)
    estado: Mapped[str] = mapped_column(String(20), default='emitido')
    xml: Mapped[bytes] = mapped_column(LargeBinary)
    datos: Mapped[Dict[str, Any]] = mapped_column(JSON)
    track_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    detalle_sii: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    anula_id: Mapped[Optional[str]] = mapped_column(ForeignKey('documentos.id'), nullable=True)
    envio_id: Mapped[Optional[str]] = mapped_column(ForeignKey('envios_sii.id'), nullable=True, index=True)
    set_id: Mapped[Optional[str]] = mapped_column(ForeignKey('sets_prueba.id'), nullable=True, index=True)
    caso: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, onupdate=ahora)

    empresa: Mapped[Empresa] = relationship()
    envio: Mapped[Optional[EnvioSii]] = relationship()


class Tarea(Base):
    """
    Cola de trabajo en la base de datos: enviar y consultar (un envío) y webhook.
    Un worker las toma con bloqueo (SKIP LOCKED en PostgreSQL).
    """

    __tablename__ = 'tareas'
    __table_args__ = (Index('ix_tareas_pendientes', 'estado', 'ejecutar_en'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tipo: Mapped[str] = mapped_column(String(20))
    documento_id: Mapped[Optional[str]] = mapped_column(ForeignKey('documentos.id'), nullable=True)
    envio_id: Mapped[Optional[str]] = mapped_column(ForeignKey('envios_sii.id'), nullable=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    estado: Mapped[str] = mapped_column(String(20), default='pendiente')  # pendiente, en_proceso, hecha, fallida
    ejecutar_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    bloqueada_hasta: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    intentos: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EnvioRecibido(Base):
    """EnvioDTE recibido de un proveedor (intercambio entre contribuyentes)."""

    __tablename__ = 'envios_recibidos'
    __table_args__ = (UniqueConstraint('empresa_id', 'cod_envio'),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nuevo_id)
    empresa_id: Mapped[int] = mapped_column(ForeignKey('empresas.id'), index=True)
    # Número correlativo del envío para la empresa (CodEnvio de las respuestas).
    cod_envio: Mapped[int] = mapped_column(Integer)
    nombre: Mapped[str] = mapped_column(String(80))
    id_envio: Mapped[str] = mapped_column(String(80))
    rut_emisor: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    estado: Mapped[int] = mapped_column(Integer)
    glosa: Mapped[str] = mapped_column(String(200))
    errores: Mapped[list] = mapped_column(JSON, default=list)
    xml: Mapped[bytes] = mapped_column(LargeBinary)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)

    empresa: Mapped[Empresa] = relationship()


ESTADOS_RECIBIDO = (
    'pendiente',          # recibido; el plazo de 8 días para reclamar corre desde la recepción en el SII
    'aceptado',           # contenido aceptado (ACD) en el registro del SII
    'recibo_mercaderias',  # recibo de mercaderías o servicios otorgado (ERM)
    'reclamado',          # reclamado (RCD, RFP o RFT)
    'no_recibido',        # no pasó la validación del intercambio (firma, RUT, timbre...)
)


class DocumentoRecibido(Base):
    """DTE recibido de un proveedor."""

    __tablename__ = 'documentos_recibidos'
    __table_args__ = (
        UniqueConstraint('empresa_id', 'rut_emisor', 'tipo', 'folio'),
        Index('ix_recibidos_empresa_estado', 'empresa_id', 'estado'),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=nuevo_id)
    empresa_id: Mapped[int] = mapped_column(ForeignKey('empresas.id'))
    envio_id: Mapped[str] = mapped_column(ForeignKey('envios_recibidos.id'))
    rut_emisor: Mapped[str] = mapped_column(String(12))
    tipo: Mapped[int] = mapped_column(Integer)
    folio: Mapped[int] = mapped_column(Integer)
    fecha: Mapped[str] = mapped_column(String(10))
    monto_total: Mapped[int] = mapped_column(Integer, default=0)
    # Validación del intercambio (EstadoRecepDTE): 0 = recibido OK.
    estado_recepcion: Mapped[int] = mapped_column(Integer)
    glosa_recepcion: Mapped[str] = mapped_column(String(200))
    estado: Mapped[str] = mapped_column(String(20), default='pendiente')
    accion: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    respuesta_sii: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON, nullable=True)
    xml: Mapped[bytes] = mapped_column(LargeBinary)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, onupdate=ahora)

    empresa: Mapped[Empresa] = relationship()
    envio: Mapped[EnvioRecibido] = relationship()
