"""
Operaciones del servicio sobre el core rypsystems-dte: registrar
certificados y CAF, asignar folios, emitir, anular y generar PDF.

La emisión es síncrona (normaliza, timbra y firma en la misma transacción
que asigna el folio); el envío al SII y las consultas de estado las hace el
worker a partir de las tareas.
"""

import copy
import threading
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rypsystems_dte import (
    Caf as CafCore,
    CafValidator,
    DigitalCertificate,
    DocumentoEmitido,
    DtePdf,
    DtePdfTermico,
    EmisionError,
    anular as anular_core,
    emitir_documento,
)
from rypsystems_dte.certificacion.set_pruebas import TIPOS_CAMBIO_CERTIFICACION
from rypsystems_dte.emision import DOCUMENTO_ANULACION
from rypsystems_dte.tipo_cambio import EXPORTACION, ProveedorTipoCambio, TiposCambioFijos
from rypsystems_dte.sii.base import SiiAmbiente

from .models import Caf, Certificado, Documento, Empresa, EnvioSii, Tarea
from .seguridad import Cifrador


class ServicioError(Exception):
    """Error de negocio del servicio, con su código HTTP."""

    status = 400

    def __init__(self, mensaje: str, status: Optional[int] = None):
        super().__init__(mensaje)
        if status:
            self.status = status


class NoEncontrado(ServicioError):
    status = 404


class Conflicto(ServicioError):
    status = 409


class DatosInvalidos(ServicioError):
    status = 422


INTENTOS_FOLIO = 20
AMBIENTES = {'certificacion': SiiAmbiente.CERTIFICACION, 'produccion': SiiAmbiente.PRODUCCION}


class Servicio:
    def __init__(self, cifrador: Cifrador, permitir_caf_falsos: bool = False,
                 banco_central: Optional[ProveedorTipoCambio] = None):
        self.cifrador = cifrador
        self.permitir_caf_falsos = permitir_caf_falsos
        self.banco_central = banco_central
        self._certificados: Dict[str, DigitalCertificate] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Certificados y CAF
    # ------------------------------------------------------------------

    def registrar_certificado(self, sesion: Session, pfx: bytes, clave: str) -> Certificado:
        try:
            certificado = DigitalCertificate.from_pkcs12(pfx, clave)
        except Exception as e:
            raise DatosInvalidos(f'No fue posible abrir el certificado: {e}') from e
        if not certificado.is_active():
            raise DatosInvalidos(f'El certificado de {certificado.get_id()} no está vigente.')
        registro = Certificado(
            rut=certificado.get_id(), nombre=certificado.get_name(),
            vence=datetime.fromisoformat(certificado.get_to()).astimezone(timezone.utc),
            pfx_cifrado=self.cifrador.cifrar(pfx), clave_cifrada=self.cifrador.cifrar(clave.encode()),
        )
        sesion.add(registro)
        sesion.flush()
        return registro

    def certificado(self, registro: Certificado) -> DigitalCertificate:
        """Certificado descifrado (se mantiene en memoria por proceso)."""
        with self._lock:
            if registro.id not in self._certificados:
                pfx = self.cifrador.descifrar(registro.pfx_cifrado)
                clave = self.cifrador.descifrar(registro.clave_cifrada).decode()
                self._certificados[registro.id] = DigitalCertificate.from_pkcs12(pfx, clave)
            return self._certificados[registro.id]

    def certificado_empresa(self, empresa: Empresa) -> DigitalCertificate:
        if not empresa.certificado:
            raise Conflicto(f'La empresa {empresa.rut} no tiene certificado asignado.')
        return self.certificado(empresa.certificado)

    def registrar_caf(self, sesion: Session, empresa: Empresa, xml: bytes) -> Caf:
        try:
            caf = CafCore(xml)
            if not caf.es_falso():
                CafValidator().validate(caf)
        except Exception as e:
            raise DatosInvalidos(f'El CAF no es válido: {e}') from e
        if caf.es_falso() and not self.permitir_caf_falsos:
            raise DatosInvalidos('El CAF es falso (IDK 666).')
        if caf.get_emisor()['rut'] != empresa.rut:
            raise DatosInvalidos(f'El CAF es de {caf.get_emisor()["rut"]}, no de {empresa.rut}.')
        if not caf.es_falso() and caf.get_ambiente() != AMBIENTES[empresa.ambiente]:
            raise DatosInvalidos(f'El CAF no es del ambiente de {empresa.ambiente}.')
        if sesion.scalar(select(Caf).where(Caf.empresa_id == empresa.id, Caf.tipo == caf.get_tipo_documento(),
                                           Caf.desde == caf.get_folio_desde())):
            raise Conflicto('Ese CAF ya está registrado.')
        # Los folios ya usados de este rango (p. ej. emitidos con otro sistema) se saltan.
        usados = sesion.scalar(
            select(Documento.folio).where(Documento.empresa_id == empresa.id,
                                          Documento.tipo == caf.get_tipo_documento(),
                                          Documento.folio.between(caf.get_folio_desde(), caf.get_folio_hasta()))
            .order_by(Documento.folio.desc()).limit(1))
        vence = caf.get_fecha_vencimiento()
        registro = Caf(
            empresa_id=empresa.id, tipo=caf.get_tipo_documento(), desde=caf.get_folio_desde(),
            hasta=caf.get_folio_hasta(), siguiente=(usados or caf.get_folio_desde() - 1) + 1,
            vence=date.fromisoformat(vence) if vence else None, xml_cifrado=self.cifrador.cifrar(caf.get_xml()),
        )
        sesion.add(registro)
        sesion.flush()
        return registro

    def ajustar_siguiente_folio(self, sesion: Session, caf: Caf, siguiente: int) -> Caf:
        """Avanza el próximo folio de un CAF (folios ya usados fuera de este servicio). No retrocede."""
        if siguiente < caf.siguiente or siguiente > caf.hasta + 1:
            raise DatosInvalidos(f'El siguiente folio debe estar entre {caf.siguiente} y {caf.hasta + 1}.')
        caf.siguiente = siguiente
        return caf

    def tipo_cambio(self, empresa: Empresa) -> Optional[ProveedorTipoCambio]:
        """
        Tipo de cambio para exportación: el del Banco Central si está
        configurado; en certificación, si no, los referenciales del SII.
        """
        if self.banco_central is not None:
            return self.banco_central
        if empresa.ambiente == 'certificacion':
            return TiposCambioFijos(TIPOS_CAMBIO_CERTIFICACION)
        return None

    def caf_core(self, caf: Caf) -> CafCore:
        return CafCore(self.cifrador.descifrar(caf.xml_cifrado))

    def asignar_folio(self, sesion: Session, empresa: Empresa, tipo: int) -> Tuple[Caf, int]:
        """
        Toma el siguiente folio libre del tipo, bloqueando la fila del CAF
        (SELECT ... FOR UPDATE) para que dos emisiones simultáneas no usen
        el mismo folio. El folio queda usado solo si la transacción se confirma.
        """
        consulta = (
            select(Caf)
            .where(Caf.empresa_id == empresa.id, Caf.tipo == tipo, Caf.siguiente <= Caf.hasta,
                   or_(Caf.vence.is_(None), Caf.vence > date.today()))
            .order_by(Caf.desde)
            .limit(1)
            .with_for_update()
        )
        caf = sesion.scalars(consulta).first()
        if caf is None:
            raise Conflicto(f'No quedan folios disponibles del tipo {tipo} para {empresa.rut}.')
        folio = caf.siguiente
        caf.siguiente += 1
        return caf, folio

    # ------------------------------------------------------------------
    # Emisión
    # ------------------------------------------------------------------

    def emitir(self, sesion: Session, empresa: Empresa, datos: Dict[str, Any],
               idempotency_key: Optional[str] = None, anula: Optional[Documento] = None) -> Tuple[Documento, bool]:
        """
        Emite un documento y encola su envío al SII.

        Returns:
            (documento, creado). creado es False si la Idempotency-Key ya
            existía y se devuelve el documento emitido antes.
        """
        if idempotency_key:
            previo = sesion.scalar(select(Documento).where(Documento.empresa_id == empresa.id,
                                                           Documento.idempotency_key == idempotency_key))
            if previo:
                return previo, False
        datos = copy.deepcopy(datos)
        try:
            id_doc = datos['Encabezado']['IdDoc']
            tipo = int(id_doc['TipoDTE'])
        except (KeyError, TypeError, ValueError):
            raise DatosInvalidos('El documento debe traer Encabezado.IdDoc.TipoDTE.')
        if id_doc.get('Folio'):
            raise DatosInvalidos('El folio lo asigna el servicio; no lo envíe en IdDoc.Folio.')
        certificado = self.certificado_empresa(empresa)
        datos['Encabezado']['Emisor'] = {**empresa.emisor, 'RUTEmisor': empresa.rut}
        tipo_cambio = self.tipo_cambio(empresa)
        otra = datos['Encabezado'].get('OtraMoneda') or {}
        if tipo in EXPORTACION and tipo_cambio is None and not (isinstance(otra, dict) and otra.get('TpoCambio')):
            raise DatosInvalidos('Los documentos de exportación requieren OtraMoneda.TpoCambio, o configurar el '
                                 'Banco Central (DTE_BCCH_TOKEN) para calcularlo.')

        for _ in range(INTENTOS_FOLIO):
            caf, folio = self.asignar_folio(sesion, empresa, tipo)
            id_doc['Folio'] = folio
            try:
                emitido = emitir_documento(datos, self.caf_core(caf), certificado, tipo_cambio=tipo_cambio)
            except EmisionError as e:
                sesion.rollback()
                raise DatosInvalidos(str(e)) from e
            try:
                documento = self._guardar(sesion, empresa, emitido, datos, idempotency_key, anula)
                sesion.commit()
                return documento, True
            except IntegrityError:
                # Folio tomado por otra transacción (posible en SQLite, que no tiene FOR UPDATE)
                # o Idempotency-Key repetida en paralelo.
                sesion.rollback()
                if idempotency_key:
                    previo = sesion.scalar(select(Documento).where(Documento.empresa_id == empresa.id,
                                                                   Documento.idempotency_key == idempotency_key))
                    if previo:
                        return previo, False
        raise Conflicto('No fue posible asignar un folio; reintente.')

    def anular(self, sesion: Session, original: Documento, razon: str = 'ANULA DOCUMENTO',
               idempotency_key: Optional[str] = None) -> Tuple[Documento, bool]:
        """Emite la nota que anula el documento (CodRef 1) y la encola.

        La Idempotency-Key se mira PRIMERO: un reintento tras un timeout debe
        recibir la nota ya emitida, no "ya tiene una anulación".
        """
        if idempotency_key:
            previo = sesion.scalar(select(Documento).where(Documento.empresa_id == original.empresa_id,
                                                           Documento.idempotency_key == idempotency_key))
            if previo:
                return previo, False
        if original.estado == 'rechazado':
            raise Conflicto('El documento fue rechazado por el SII; no requiere anulación.')
        tipo = DOCUMENTO_ANULACION.get(original.tipo)
        if not tipo:
            raise DatosInvalidos(f'Los documentos tipo {original.tipo} no se anulan con una nota.')
        if sesion.scalar(select(Documento).where(Documento.anula_id == original.id,
                                                 Documento.estado != 'rechazado')):
            raise Conflicto('El documento ya tiene una anulación.')
        empresa = original.empresa
        certificado = self.certificado_empresa(empresa)
        caf, folio = self.asignar_folio(sesion, empresa, tipo)
        try:
            emitido = anular_core(DocumentoEmitido.desde_xml(original.xml), folio, self.caf_core(caf), certificado,
                                  razon=razon[:90])
        except EmisionError as e:
            sesion.rollback()
            raise DatosInvalidos(str(e)) from e
        documento = self._guardar(sesion, empresa, emitido, emitido.datos, idempotency_key, original)
        sesion.commit()
        return documento, True

    def _guardar(self, sesion: Session, empresa: Empresa, emitido: DocumentoEmitido, datos: Dict[str, Any],
                 idempotency_key: Optional[str], anula: Optional[Documento]) -> Documento:
        documento = self.guardar_documento(sesion, empresa, emitido, datos, idempotency_key=idempotency_key,
                                           anula=anula)
        self.encolar_envio(sesion, empresa, [documento])
        return documento

    @staticmethod
    def guardar_documento(sesion: Session, empresa: Empresa, emitido: DocumentoEmitido, datos: Dict[str, Any],
                          idempotency_key: Optional[str] = None, anula: Optional[Documento] = None,
                          set_id: Optional[str] = None, caso: Optional[str] = None) -> Documento:
        documento = Documento(
            empresa_id=empresa.id, tipo=emitido.tipo, folio=emitido.folio, fecha=emitido.fecha,
            receptor=emitido.receptor, monto_total=int(emitido.totales.get('MntTotal') or 0), estado='emitido',
            xml=emitido.xml, datos=datos, idempotency_key=idempotency_key, anula_id=anula.id if anula else None,
            set_id=set_id, caso=caso,
        )
        sesion.add(documento)
        sesion.flush()
        return documento

    @staticmethod
    def encolar_envio(sesion: Session, empresa: Empresa, documentos: List[Documento]) -> EnvioSii:
        """Agrupa los documentos en un envío (EnvioDTE o EnvioBOLETA) y agenda su envío al SII."""
        boletas = {d.tipo in (39, 41) for d in documentos}
        if len(boletas) != 1:
            raise DatosInvalidos('Un envío no puede mezclar boletas con otros documentos.')
        envio = EnvioSii(empresa_id=empresa.id, tipo='boletas' if boletas.pop() else 'dte')
        sesion.add(envio)
        sesion.flush()
        for documento in documentos:
            documento.envio_id = envio.id
        sesion.add(Tarea(tipo='enviar', envio_id=envio.id))
        return envio

    @staticmethod
    def encolar_libro(sesion: Session, empresa: Empresa, xml: bytes) -> EnvioSii:
        envio = EnvioSii(empresa_id=empresa.id, tipo='libro', xml=xml)
        sesion.add(envio)
        sesion.flush()
        sesion.add(Tarea(tipo='enviar', envio_id=envio.id))
        return envio

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------

    @staticmethod
    def pdf(documento: Documento, formato: str = 'carta', cedible: bool = False) -> bytes:
        empresa = documento.empresa
        resolucion = {'fecha': empresa.fecha_resolucion, 'numero': empresa.numero_resolucion}
        clase = DtePdfTermico if formato == '80mm' else DtePdf
        return clase(resolucion).render(documento.xml, cedible=cedible)
