"""
Tareas con el SII y webhooks, procesadas por el worker.

- enviar: arma el sobre del documento, lo sube al SII y agenda la consulta.
- consultar: consulta el estado del envío hasta que el SII termina de
  procesarlo (espera creciente) y actualiza el documento.
- webhook: avisa al sistema principal el cambio de estado (HMAC-SHA256 en
  la cabecera X-DTE-Firma).

Los errores de comunicación se reintentan con espera creciente. Si se agotan
los intentos, el documento queda en estado "error" con el detalle.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional, Tuple

import requests
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rypsystems_dte import ClienteSii, SiiEnvironment, armar_envio
from rypsystems_dte.errors import RypDteError

from .config import Settings
from .models import Documento, Tarea, ahora
from .schemas import documento_a_dict
from .seguridad import firmar_webhook
from .servicio import Servicio

logger = logging.getLogger(__name__)

AMBIENTES_SII = {'certificacion': SiiEnvironment.CERTIFICACION, 'produccion': SiiEnvironment.PRODUCCION}
BLOQUEO = timedelta(minutes=5)


class ErrorReintentable(Exception):
    """Falla temporal: la tarea se reintenta más tarde."""


def estado_documento(consulta: Dict) -> str:
    """Estado del documento según la consulta del envío (un documento por envío)."""
    # Estados finales distintos de EPR: rechazo del envío (esquema, firma, carátula o códigos de error).
    if consulta['estado'] != 'EPR' or consulta['rechazados']:
        return 'rechazado'
    if consulta['reparos']:
        return 'aceptado_reparos'
    if consulta['aceptados']:
        return 'aceptado'
    return 'rechazado'


class Procesador:
    """Ejecuta tareas. clientes_sii permite reemplazar la comunicación con el SII en pruebas."""

    def __init__(self, settings: Settings, servicio: Servicio,
                 cliente_sii: Optional[Callable[..., ClienteSii]] = None,
                 http_post: Optional[Callable[..., requests.Response]] = None):
        self.settings = settings
        self.servicio = servicio
        self._fabrica_cliente = cliente_sii or ClienteSii
        self._http_post = http_post or requests.post
        self._clientes: Dict[Tuple[str, str], ClienteSii] = {}
        self._lock = threading.Lock()

    def cliente_sii(self, documento: Documento) -> ClienteSii:
        empresa = documento.empresa
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
                if tarea.documento_id and tarea.tipo in ('enviar', 'consultar'):
                    documento = sesion.get(Documento, tarea.documento_id)
                    self._cambiar_estado(sesion, documento, 'error', {'error': tarea.error, 'etapa': tarea.tipo})
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

    def enviar(self, sesion: Session, tarea: Tarea) -> None:
        documento = sesion.get(Documento, tarea.documento_id)
        if documento.track_id:
            return  # ya enviado (tarea repetida tras una caída)
        empresa = documento.empresa
        cliente = self.cliente_sii(documento)
        envio = armar_envio([documento.xml], cliente.certificado, empresa.fecha_resolucion,
                            empresa.numero_resolucion)
        track_id = cliente.enviar(envio, empresa.rut)
        documento.track_id = str(track_id)
        self._cambiar_estado(sesion, documento, 'enviado', {'track_id': str(track_id)})
        sesion.add(Tarea(tipo='consultar', documento_id=documento.id,
                         payload={'espera': self.settings.consulta_espera_inicial, 'desde': ahora().isoformat()},
                         ejecutar_en=ahora() + timedelta(seconds=self.settings.consulta_espera_inicial)))

    def consultar(self, sesion: Session, tarea: Tarea) -> None:
        documento = sesion.get(Documento, tarea.documento_id)
        consulta = self.cliente_sii(documento).consultar_envio(
            documento.track_id, documento.empresa.rut, boletas=documento.tipo in (39, 41))
        detalle = {k: consulta[k] for k in ('estado', 'informados', 'aceptados', 'rechazados', 'reparos', 'detalle')}
        if not consulta['final']:
            desde = datetime.fromisoformat(tarea.payload.get('desde') or ahora().isoformat())
            if ahora() - desde > timedelta(hours=self.settings.consulta_plazo_horas):
                self._cambiar_estado(sesion, documento, 'error',
                                     {**detalle, 'error': 'El SII no terminó de procesar el envío en el plazo.'})
                return
            espera = min(int(tarea.payload.get('espera', 15)) * 2, self.settings.consulta_espera_maxima)
            documento.detalle_sii = detalle
            sesion.add(Tarea(tipo='consultar', documento_id=documento.id,
                             payload={'espera': espera, 'desde': desde.isoformat()},
                             ejecutar_en=ahora() + timedelta(seconds=espera)))
            return
        self._cambiar_estado(sesion, documento, estado_documento(consulta), detalle)

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

    def _cambiar_estado(self, sesion: Session, documento: Documento, estado: str, detalle: Dict) -> None:
        anterior = documento.estado
        documento.estado = estado
        documento.detalle_sii = detalle
        documento.actualizado = datetime.now(timezone.utc)
        if anterior != estado and self.settings.webhook_url:
            sesion.add(Tarea(tipo='webhook', documento_id=documento.id, payload={
                'evento': 'documento.estado', 'estado_anterior': anterior, 'documento': documento_a_dict(documento),
            }))
