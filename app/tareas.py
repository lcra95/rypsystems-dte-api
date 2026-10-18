"""
Tareas con el SII y webhooks, procesadas por el worker.

- enviar: arma el sobre del envío (EnvioDTE o EnvioBOLETA con sus
  documentos) o toma el libro ya firmado, lo sube al SII y agenda la consulta.
- consultar: consulta el estado del envío hasta que el SII termina de
  procesarlo (espera creciente) y actualiza el envío y sus documentos.
- webhook: avisa al sistema principal el cambio de estado (HMAC-SHA256 en
  la cabecera X-DTE-Firma).

Los errores de comunicación se reintentan con espera creciente. Si se agotan
los intentos, el envío y sus documentos quedan en estado "error".
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rypsystems_dte import ClienteSii, SiiEnvironment, armar_envio
from rypsystems_dte.errors import RypDteError

from .config import Settings
from .models import Documento, Empresa, EnvioSii, Tarea, ahora
from .schemas import documento_a_dict, envio_sii_a_dict
from .seguridad import firmar_webhook
from .servicio import Servicio

logger = logging.getLogger(__name__)

AMBIENTES_SII = {'certificacion': SiiEnvironment.CERTIFICACION, 'produccion': SiiEnvironment.PRODUCCION}
BLOQUEO = timedelta(minutes=5)
# Estados intermedios de un libro en QueryEstUp (recibido, esquema y carátula correctos).
LIBRO_EN_PROCESO = ('REC', 'SOK', 'CRT', 'FOK', 'PDR', 'LRC', 'LSO', None)


class ErrorReintentable(Exception):
    """Falla temporal: la tarea se reintenta más tarde."""


def estado_documento(consulta: Dict) -> str:
    """Estado de un envío de DTE según su consulta (QueryEstUp)."""
    # Estados finales distintos de EPR: rechazo del envío (esquema, firma, carátula o códigos de error).
    if consulta['estado'] != 'EPR' or consulta['rechazados']:
        return 'rechazado'
    if consulta['reparos']:
        return 'aceptado_reparos'
    if consulta['aceptados']:
        return 'aceptado'
    return 'rechazado'


def estado_libro(estado: Optional[str]) -> str:
    # LOK: libro cuadrado; LNC: libro no cuadrado (aceptado con observaciones).
    return {'LOK': 'aceptado', 'LNC': 'aceptado_reparos'}.get(estado or '', 'rechazado')


class Procesador:
    """Ejecuta tareas. cliente_sii permite reemplazar la comunicación con el SII en pruebas."""

    def __init__(self, settings: Settings, servicio: Servicio,
                 cliente_sii: Optional[Callable[..., ClienteSii]] = None,
                 http_post: Optional[Callable[..., requests.Response]] = None):
        self.settings = settings
        self.servicio = servicio
        self._fabrica_cliente = cliente_sii or ClienteSii
        self._http_post = http_post or requests.post
        self._clientes: Dict[Tuple[str, str], ClienteSii] = {}
        self._lock = threading.Lock()

    def cliente_sii(self, empresa: Empresa) -> ClienteSii:
        clave = (empresa.certificado_id or '', empresa.ambiente)
        with self._lock:
            if clave not in self._clientes:
                self._clientes[clave] = self._fabrica_cliente(self.servicio.certificado_empresa(empresa),
                                                              AMBIENTES_SII[empresa.ambiente])
            return self._clientes[clave]

    # ------------------------------------------------------------------
    # Cola
    # ------------------------------------------------------------------

    def tomar(self, sesion: Session) -> Optional[Tarea]:
        """Toma la próxima tarea lista (también las que quedaron bloqueadas por un worker caído)."""
        momento = ahora()
        consulta = (
            select(Tarea)
            .where(Tarea.ejecutar_en <= momento,
                   or_(Tarea.estado == 'pendiente',
                       (Tarea.estado == 'en_proceso') & (Tarea.bloqueada_hasta < momento)))
            .order_by(Tarea.ejecutar_en)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        tarea = sesion.scalars(consulta).first()
        if tarea is None:
            return None
        tarea.estado = 'en_proceso'
        tarea.bloqueada_hasta = momento + BLOQUEO
        tarea.intentos += 1
        sesion.commit()
        return tarea

    def procesar_siguiente(self, sesion: Session) -> bool:
        """Procesa una tarea. Devuelve False si no había tareas listas."""
        tarea = self.tomar(sesion)
        if tarea is None:
            return False
        manejador = {'enviar': self.enviar, 'consultar': self.consultar, 'webhook': self.webhook}[tarea.tipo]
        try:
            manejador(sesion, tarea)
            tarea.estado = 'hecha'
            tarea.error = None
        except Exception as e:  # noqa: BLE001 - toda falla queda registrada en la tarea
            sesion.rollback()
            tarea = sesion.get(Tarea, tarea.id)
            reintentable = isinstance(e, (ErrorReintentable, RypDteError, requests.RequestException))
            tarea.error = f'{type(e).__name__}: {e}'[:2000]
            if reintentable and tarea.intentos < self.settings.tareas_max_intentos:
                tarea.estado = 'pendiente'
                tarea.ejecutar_en = ahora() + timedelta(seconds=min(30 * 2 ** (tarea.intentos - 1), 3600))
                logger.warning(f'Tarea {tarea.id} ({tarea.tipo}) se reintentará: {tarea.error}')
            else:
                tarea.estado = 'fallida'
                logger.error(f'Tarea {tarea.id} ({tarea.tipo}) falló: {tarea.error}')
                if tarea.envio_id and tarea.tipo in ('enviar', 'consultar'):
                    envio = sesion.get(EnvioSii, tarea.envio_id)
                    self._estado_envio(sesion, envio, 'error', {'error': tarea.error, 'etapa': tarea.tipo})
        tarea.bloqueada_hasta = None
        sesion.commit()
        return True

    def procesar_pendientes(self, sesion: Session, maximo: int = 100) -> int:
        procesadas = 0
        while procesadas < maximo and self.procesar_siguiente(sesion):
            procesadas += 1
        return procesadas

    # ------------------------------------------------------------------
    # Manejadores
    # ------------------------------------------------------------------

    @staticmethod
    def documentos(sesion: Session, envio: EnvioSii) -> List[Documento]:
        return list(sesion.scalars(select(Documento).where(Documento.envio_id == envio.id)
                                   .order_by(Documento.creado, Documento.tipo, Documento.folio)))

    def enviar(self, sesion: Session, tarea: Tarea) -> None:
        envio = sesion.get(EnvioSii, tarea.envio_id)
        if envio.track_id:
            return  # ya enviado (tarea repetida tras una caída)
        empresa = envio.empresa
        cliente = self.cliente_sii(empresa)
        if envio.tipo == 'libro':
            track_id = cliente.servicio_dte().upload_dte(cliente.certificado.get_id(), empresa.rut,
                                                         envio.xml)['track_id']
        else:
            documentos = self.documentos(sesion, envio)
            envio.xml = armar_envio([d.xml for d in documentos], cliente.certificado, empresa.fecha_resolucion,
                                    empresa.numero_resolucion)
            track_id = cliente.enviar(envio.xml, empresa.rut)
        envio.track_id = str(track_id)
        self._estado_envio(sesion, envio, 'enviado', {'track_id': str(track_id)})
        sesion.add(Tarea(tipo='consultar', envio_id=envio.id,
                         payload={'espera': self.settings.consulta_espera_inicial, 'desde': ahora().isoformat()},
                         ejecutar_en=ahora() + timedelta(seconds=self.settings.consulta_espera_inicial)))

    def consultar(self, sesion: Session, tarea: Tarea) -> None:
        envio = sesion.get(EnvioSii, tarea.envio_id)
        consulta = self.cliente_sii(envio.empresa).consultar_envio(envio.track_id, envio.empresa.rut,
                                                                   boletas=envio.tipo == 'boletas')
        detalle = {k: consulta[k] for k in ('estado', 'informados', 'aceptados', 'rechazados', 'reparos', 'detalle')}
        final = consulta['final'] if envio.tipo != 'libro' else consulta['estado'] not in LIBRO_EN_PROCESO
        if not final:
            desde = datetime.fromisoformat(tarea.payload.get('desde') or ahora().isoformat())
            if ahora() - desde > timedelta(hours=self.settings.consulta_plazo_horas):
                self._estado_envio(sesion, envio, 'error',
                                   {**detalle, 'error': 'El SII no terminó de procesar el envío en el plazo.'})
                return
            espera = min(int(tarea.payload.get('espera', 15)) * 2, self.settings.consulta_espera_maxima)
            envio.detalle_sii = detalle
            sesion.add(Tarea(tipo='consultar', envio_id=envio.id, payload={'espera': espera, 'desde': desde.isoformat()},
                             ejecutar_en=ahora() + timedelta(seconds=espera)))
            return
        if envio.tipo == 'libro':
            self._estado_envio(sesion, envio, estado_libro(consulta['estado']), detalle)
            return
        estado = estado_documento(consulta)
        self._estado_envio(sesion, envio, estado, detalle, estados_documentos=self._estados_documentos(
            sesion, envio, consulta, estado))

    def _estados_documentos(self, sesion: Session, envio: EnvioSii, consulta: Dict[str, Any],
                            estado_envio: str) -> Dict[str, str]:
        """Estado de cada documento del envío."""
        documentos = self.documentos(sesion, envio)
        if len(documentos) == 1 or estado_envio == 'aceptado' or consulta['estado'] != 'EPR':
            return {d.id: estado_envio for d in documentos}
        if envio.tipo == 'boletas':
            # La API de boletas detalla solo las boletas con reparos o rechazadas.
            problemas = {(int(d.get('tipo') or 0), int(d.get('folio') or 0)): d.get('estado')
                         for d in consulta['detalle']}
            return {d.id: {'RPR': 'aceptado_reparos', None: 'aceptado'}.get(problemas.get((d.tipo, d.folio)),
                                                                            'rechazado') for d in documentos}
        # QueryEstUp resume por tipo de documento: los tipos sin reparos ni rechazos quedan aceptados.
        por_tipo = {int(d.get('tipo_docto') or 0): d for d in consulta['detalle']}
        estados = {}
        for d in documentos:
            resumen = por_tipo.get(d.tipo)
            ok = resumen and not int(resumen.get('rechazados') or 0) and not int(resumen.get('reparos') or 0) \
                and int(resumen.get('aceptados') or 0) == int(resumen.get('informados') or 0)
            estados[d.id] = 'aceptado' if ok else 'revisar'
        return estados

    def webhook(self, sesion: Session, tarea: Tarea) -> None:
        if not self.settings.webhook_url:
            return
        cuerpo = json.dumps(tarea.payload, ensure_ascii=False, default=str).encode()
        respuesta = self._http_post(self.settings.webhook_url, data=cuerpo, timeout=15, headers={
            'Content-Type': 'application/json',
            'X-DTE-Evento': tarea.payload.get('evento', ''),
            'X-DTE-Firma': firmar_webhook(cuerpo, self.settings.webhook_secret),
        })
        if respuesta.status_code >= 300:
            raise ErrorReintentable(f'El webhook respondió HTTP {respuesta.status_code}.')

    # ------------------------------------------------------------------
    # Estados y avisos
    # ------------------------------------------------------------------

    def _estado_envio(self, sesion: Session, envio: EnvioSii, estado: str, detalle: Dict,
                      estados_documentos: Optional[Dict[str, str]] = None) -> None:
        anterior = envio.estado
        envio.estado, envio.detalle_sii = estado, detalle
        envio.actualizado = datetime.now(timezone.utc)
        documentos = self.documentos(sesion, envio)
        if self.settings.webhook_url and anterior != estado and (envio.tipo == 'libro' or len(documentos) > 1):
            sesion.add(Tarea(tipo='webhook', envio_id=envio.id, payload={
                'evento': 'envio.estado', 'estado_anterior': anterior, 'envio': envio_sii_a_dict(envio)}))
        for documento in documentos:
            nuevo = (estados_documentos or {}).get(documento.id, estado)
            if nuevo == 'pendiente':
                continue
            self._estado_documento(sesion, documento, nuevo, {**detalle, 'envio_id': envio.id})

    def _estado_documento(self, sesion: Session, documento: Documento, estado: str, detalle: Dict) -> None:
        anterior = documento.estado
        documento.estado = estado
        documento.detalle_sii = detalle
        documento.track_id = documento.envio.track_id if documento.envio else documento.track_id
        documento.actualizado = datetime.now(timezone.utc)
        if anterior != estado and self.settings.webhook_url:
            sesion.add(Tarea(tipo='webhook', documento_id=documento.id, payload={
                'evento': 'documento.estado', 'estado_anterior': anterior, 'documento': documento_a_dict(documento),
            }))
