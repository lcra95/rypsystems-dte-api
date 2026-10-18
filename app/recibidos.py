"""
Documentos recibidos de proveedores.

- Carga de un EnvioDTE recibido (por correo, en el intercambio): se valida
  esquema, firmas, RUT receptor y timbres, y se guarda cada documento.
- Respuestas del intercambio para el proveedor: RecepcionEnvio,
  ResultadoDTE (aprobación o rechazo comercial) y EnvioRecibos (recibo de
  mercaderías, Ley 19.983). El sistema principal las envía por correo.
- Registro de aceptación o reclamo en el SII (Ley 20.956): el receptor
  tiene 8 días desde la recepción del documento en el SII para reclamar;
  después se entiende aceptado.
"""

import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rypsystems_dte import DigitalCertificate, EnvioRecibido as EnvioCore, EnvioRecibos, RespuestaEnvio
from rypsystems_dte import RegistroReclamoService, SiiEnvironment
from rypsystems_dte.xml.schema import schema_errors

from .models import DocumentoRecibido, Empresa, EnvioRecibido
from .servicio import Conflicto, DatosInvalidos, Servicio

AMBIENTES_SII = {'certificacion': SiiEnvironment.CERTIFICACION, 'produccion': SiiEnvironment.PRODUCCION}
ESTADO_POR_ACCION = {'ACD': 'aceptado', 'ERM': 'recibo_mercaderias', 'RCD': 'reclamado', 'RFP': 'reclamado',
                     'RFT': 'reclamado'}


class Recibidos:
    def __init__(self, servicio: Servicio,
                 registro_reclamo: Optional[Callable[[DigitalCertificate, SiiEnvironment], Any]] = None):
        self.servicio = servicio
        self._fabrica = registro_reclamo or (lambda certificado, ambiente: RegistroReclamoService(ambiente))
        self._clientes: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    def registro(self, empresa: Empresa):
        """Cliente del registro de reclamos del SII, autenticado con el certificado de la empresa."""
        certificado = self.servicio.certificado_empresa(empresa)
        clave = (empresa.certificado_id or '', empresa.ambiente)
        with self._lock:
            if clave not in self._clientes:
                self._clientes[clave] = self._fabrica(certificado, AMBIENTES_SII[empresa.ambiente])
            cliente = self._clientes[clave]
        cliente.authenticate(certificado)
        return cliente

    # ------------------------------------------------------------------
    # Carga del EnvioDTE
    # ------------------------------------------------------------------

    def cargar(self, sesion: Session, empresa: Empresa, xml: bytes, nombre: str,
               validar_timbre: bool = True) -> Tuple[EnvioRecibido, List[DocumentoRecibido], List[Dict[str, Any]]]:
        """
        Valida y guarda un EnvioDTE recibido.

        Returns:
            (envío, documentos nuevos, documentos ya registrados antes)
        """
        try:
            envio = EnvioCore(xml, nombre=nombre).validar(empresa.rut, validar_timbre=validar_timbre)
        except Exception as e:
            raise DatosInvalidos(f'No es un EnvioDTE válido: {e}') from e

        for _ in range(5):
            cod_envio = (sesion.scalar(select(func.max(EnvioRecibido.cod_envio))
                                       .where(EnvioRecibido.empresa_id == empresa.id)) or 0) + 1
            registro = EnvioRecibido(
                empresa_id=empresa.id, cod_envio=cod_envio, nombre=envio.nombre, id_envio=envio.id or '',
                rut_emisor=envio.rut_emisor, estado=envio.estado, glosa=envio.glosa, errores=envio.errores[:20],
                xml=envio.xml,
            )
            sesion.add(registro)
            try:
                sesion.flush()
                break
            except IntegrityError:
                sesion.rollback()
        else:
            raise Conflicto('No fue posible registrar el envío; reintente.')

        nuevos, repetidos = [], []
        for doc in envio.documentos:
            previo = sesion.scalar(select(DocumentoRecibido).where(
                DocumentoRecibido.empresa_id == empresa.id, DocumentoRecibido.rut_emisor == doc.rut_emisor,
                DocumentoRecibido.tipo == doc.tipo, DocumentoRecibido.folio == doc.folio))
            if previo is not None or doc.estado == 4:
                repetidos.append({'tipo': doc.tipo, 'folio': doc.folio, 'rut_emisor': doc.rut_emisor,
                                  'id': previo.id if previo else None})
                continue
            recibido_ok = envio.estado == 0 and doc.estado == 0
            nuevo = DocumentoRecibido(
                empresa_id=empresa.id, envio_id=registro.id, rut_emisor=doc.rut_emisor, tipo=doc.tipo,
                folio=doc.folio, fecha=doc.fecha_emision, monto_total=doc.monto_total,
                estado_recepcion=doc.estado if envio.estado == 0 else envio.estado,
                glosa_recepcion=doc.glosa if envio.estado == 0 else envio.glosa,
                estado='pendiente' if recibido_ok else 'no_recibido', xml=doc.xml,
            )
            sesion.add(nuevo)
            nuevos.append(nuevo)
        sesion.commit()
        return registro, nuevos, repetidos

    # ------------------------------------------------------------------
    # Respuestas del intercambio
    # ------------------------------------------------------------------

    def respuestas(self, sesion: Session, envio: EnvioRecibido, rechazar: Iterable[str] = (),
                   recinto: Optional[str] = None, contacto: Optional[Dict[str, Optional[str]]] = None,
                   validar_timbre: bool = True) -> Dict[str, bytes]:
        """
        XML firmados para el proveedor: recepcion_envio siempre; resultado_dte
        (aprobación o rechazo comercial de cada DTE) y recibo_mercaderias (de
        los aprobados) si el envío se recibió conforme.
        """
        empresa = envio.empresa
        certificado = self.servicio.certificado_empresa(empresa)
        core = EnvioCore(envio.xml, nombre=envio.nombre).validar(empresa.rut, validar_timbre=validar_timbre)
        contacto = {k: v for k, v in (contacto or {}).items() if v}
        rechazar = set(rechazar)
        ids = {(d.rut_emisor, d.tipo, d.folio): d.id for d in sesion.scalars(
            select(DocumentoRecibido).where(DocumentoRecibido.envio_id == envio.id))}
        desconocidos = rechazar - set(ids.values())
        if desconocidos:
            raise DatosInvalidos(f'Documentos que no son de este envío: {", ".join(sorted(desconocidos))}')
        rechazados = {clave for clave, doc_id in ids.items() if doc_id in rechazar}

        recepcion = RespuestaEnvio(empresa.rut, core.rut_emisor, id_respuesta=envio.cod_envio, **contacto)
        recepcion.agregar_recepcion_envio(core, cod_envio=envio.cod_envio)
        archivos = {'recepcion_envio': recepcion.generar(certificado)}
        if core.estado == 0:
            resultado = RespuestaEnvio(empresa.rut, core.rut_emisor, id_respuesta=envio.cod_envio, **contacto)
            aceptados = []
            for doc in core.documentos:
                clave = (doc.rut_emisor, doc.tipo, doc.folio)
                if doc.estado != 0:
                    resultado.agregar_resultado_dte(doc.resumen(), envio.cod_envio, estado=2, codigo_rechazo=-1,
                                                    glosa='DTE Rechazado - ' + doc.glosa.replace('DTE No Recibido - ', ''))
                elif clave in rechazados:
                    resultado.agregar_resultado_dte(doc.resumen(), envio.cod_envio, estado=2, codigo_rechazo=-1)
                else:
                    resultado.agregar_resultado_dte(doc.resumen(), envio.cod_envio, estado=0)
                    aceptados.append(doc)
            archivos['resultado_dte'] = resultado.generar(certificado)
            if aceptados:
                recibos = EnvioRecibos(empresa.rut, core.rut_emisor, **contacto)
                lugar = recinto or f'{empresa.emisor["DirOrigen"]}, {empresa.emisor["CmnaOrigen"]}'
                for doc in aceptados:
                    recibos.agregar(doc.resumen(), recinto=lugar, certificate=certificado)
                archivos['recibo_mercaderias'] = recibos.generar(certificado)

        esquemas = {'recepcion_envio': 'RespuestaEnvioDTE_v10.xsd', 'resultado_dte': 'RespuestaEnvioDTE_v10.xsd',
                    'recibo_mercaderias': 'EnvioRecibos_v10.xsd'}
        for nombre, xml in archivos.items():
            errores = schema_errors(xml, esquemas[nombre])
            if errores:
                raise DatosInvalidos(f'{nombre} no cumple {esquemas[nombre]}: {"; ".join(errores[:3])}')
        return archivos

    # ------------------------------------------------------------------
    # Registro de aceptación o reclamo en el SII
    # ------------------------------------------------------------------

    def registrar(self, sesion: Session, recibido: DocumentoRecibido, accion: str) -> Dict[str, Any]:
        respuesta = self.registro(recibido.empresa).registrar(recibido.rut_emisor, recibido.tipo, recibido.folio,
                                                              accion)
        recibido.respuesta_sii = {**respuesta, 'accion': accion}
        if respuesta['ok']:
            recibido.accion = accion
            recibido.estado = ESTADO_POR_ACCION[accion]
        sesion.commit()
        if not respuesta['ok']:
            raise Conflicto(f'El SII no registró la acción {accion}: [{respuesta["codigo"]}] {respuesta["descripcion"]}')
        return respuesta

    def eventos(self, sesion: Session, recibido: DocumentoRecibido) -> Dict[str, Any]:
        """Eventos del documento en el SII; sincroniza el estado con el último aceptación o reclamo."""
        cliente = self.registro(recibido.empresa)
        respuesta = cliente.listar_eventos(recibido.rut_emisor, recibido.tipo, recibido.folio)
        respuesta['fecha_recepcion_sii'] = cliente.fecha_recepcion(recibido.rut_emisor, recibido.tipo, recibido.folio)
        acciones = [e['codigo'] for e in respuesta['eventos'] if e['codigo'] in ESTADO_POR_ACCION]
        if acciones:
            ultima = 'reclamado' if any(ESTADO_POR_ACCION[a] == 'reclamado' for a in acciones) \
                else ESTADO_POR_ACCION[acciones[-1]]
            if ultima != recibido.estado:
                recibido.estado, recibido.accion = ultima, acciones[-1]
                sesion.commit()
        return respuesta
