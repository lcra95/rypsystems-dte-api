"""
Certificación de una empresa ante el SII (ambiente de certificación).

Etapas que automatiza el servicio (las declaraciones de avance las hace el
representante de la empresa en el sitio del SII):

1. Set de pruebas: se carga el texto que entrega el SII; cada set se emite y
   se envía en su propio envío. Los libros (ventas, compras, guías) se
   generan con los documentos de los sets ya aceptados.
2. Simulación: el sistema principal entrega los casos (entre 20 y 100
   documentos de todos los tipos que se certifican, con datos de la
   operación real) y se emiten en un solo envío.
3. Intercambio: el XML que entrega el SII se carga como documento recibido y
   se generan las tres respuestas para subirlas al SII.
4. Documentos impresos: ZIP con un PDF por documento y por copia cedible.
"""

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from rypsystems_dte import EmisionError
from rypsystems_dte.certificacion import (
    SetPruebasError,
    emitir_set,
    leer_set_boletas,
    leer_set_pruebas,
    libro_compras,
    libro_guias,
    libro_ventas,
    muestras_impresas,
    zip_muestras,
)
from rypsystems_dte.emision import BOLETAS

from .models import Documento, Empresa, SetPrueba
from .servicio import Conflicto, DatosInvalidos, Servicio

# Receptores para los documentos del set ("utilice RUT distintos para las distintas facturas").
RECEPTORES_CERTIFICACION = [
    {'RUTRecep': '60803000-K', 'RznSocRecep': 'Servicio de Impuestos Internos', 'GiroRecep': 'Gobierno',
     'DirRecep': 'Teatinos 120', 'CmnaRecep': 'Santiago'},
    {'RUTRecep': '60805000-0', 'RznSocRecep': 'Tesoreria General de la Republica', 'GiroRecep': 'Gobierno',
     'DirRecep': 'Teatinos 28', 'CmnaRecep': 'Santiago'},
    {'RUTRecep': '60804000-5', 'RznSocRecep': 'Servicio Nacional de Aduanas', 'GiroRecep': 'Gobierno',
     'DirRecep': 'Plaza Sotomayor 60', 'CmnaRecep': 'Valparaiso'},
    {'RUTRecep': '61002000-3', 'RznSocRecep': 'Servicio de Registro Civil e Identificacion', 'GiroRecep': 'Gobierno',
     'DirRecep': 'Catedral 1772', 'CmnaRecep': 'Santiago'},
    {'RUTRecep': '60910000-1', 'RznSocRecep': 'Universidad de Chile', 'GiroRecep': 'Educacion superior',
     'DirRecep': "Av. Libertador Bernardo O'Higgins 1058", 'CmnaRecep': 'Santiago'},
]
LIBROS = ('libro_ventas', 'libro_compras', 'libro_guias')
ACEPTADOS = ('aceptado', 'aceptado_reparos')


class Certificacion:
    def __init__(self, servicio: Servicio):
        self.servicio = servicio

    # ------------------------------------------------------------------
    # Carga de sets
    # ------------------------------------------------------------------

    def cargar_sets(self, sesion: Session, empresa: Empresa, contenido: bytes,
                    tipos_cambio: Optional[Dict[str, float]] = None) -> List[SetPrueba]:
        """Registra los sets del archivo del SII (todos los sets, o el de boletas)."""
        try:
            texto = contenido.decode('utf-8', errors='ignore')
            if 'BOLETA ELECTRONICA' in texto.upper() and 'CASO-1' in texto:
                sets = [leer_set_boletas(contenido)]
            else:
                sets = leer_set_pruebas(contenido, tipos_cambio)
        except SetPruebasError as e:
            raise DatosInvalidos(f'No se pudo leer el set de pruebas: {e}') from e
        registrados = []
        for datos in sets:
            numero = datos.get('numero_atencion')
            previo = sesion.scalar(select(SetPrueba).where(
                SetPrueba.empresa_id == empresa.id, SetPrueba.tipo_set == datos['tipo_set'],
                SetPrueba.numero_atencion == numero if numero else SetPrueba.numero_atencion.is_(None)))
            if previo and previo.envio_id:
                registrados.append(previo)  # ya emitido: se conserva
                continue
            registro = previo or SetPrueba(empresa_id=empresa.id, numero_atencion=numero, tipo_set=datos['tipo_set'])
            registro.titulo = datos.get('titulo', datos['tipo_set'])[:120]
            registro.datos = datos
            sesion.add(registro)
            registrados.append(registro)
        sesion.commit()
        return registrados

    def cargar_simulacion(self, sesion: Session, empresa: Empresa, casos: List[Dict[str, Any]]) -> SetPrueba:
        documentos = len(casos)
        if not 20 <= documentos <= 100:
            raise DatosInvalidos(f'La simulación debe tener entre 20 y 100 documentos; tiene {documentos}.')
        if any(c.get('tipo') in BOLETAS for c in casos):
            raise DatosInvalidos('Las boletas se certifican aparte; no van en la simulación.')
        registro = SetPrueba(empresa_id=empresa.id, tipo_set='simulacion', titulo='SIMULACION',
                             datos={'tipo_set': 'simulacion', 'nombre': 'simulacion', 'referencia_set': False,
                                    'casos': casos})
        sesion.add(registro)
        sesion.commit()
        return registro

    # ------------------------------------------------------------------
    # Emisión
    # ------------------------------------------------------------------

    def emitir(self, sesion: Session, registro: SetPrueba,
               receptores: Optional[Iterable[Dict[str, Any]]] = None) -> SetPrueba:
        """Emite el set (o genera el libro) y agenda su envío al SII."""
        empresa = registro.empresa
        if empresa.ambiente != 'certificacion':
            raise Conflicto('La certificación solo se emite en el ambiente de certificación.')
        if registro.envio and registro.envio.estado not in ('rechazado', 'error'):
            raise Conflicto(f'El set ya se envió (estado {registro.envio.estado}). Solo se reemite si fue rechazado.')
        if registro.tipo_set in LIBROS:
            return self._emitir_libro(sesion, registro)

        certificado = self.servicio.certificado_empresa(empresa)
        emisor = {**empresa.emisor, 'RUTEmisor': empresa.rut}

        def asignar_folio(tipo):
            caf, folio = self.servicio.asignar_folio(sesion, empresa, tipo)
            return self.servicio.caf_core(caf), folio

        try:
            emitidos = emitir_set(registro.datos, emisor, certificado, asignar_folio,
                                  receptores=list(receptores or RECEPTORES_CERTIFICACION))
        except (EmisionError, Conflicto) as e:
            sesion.rollback()
            raise (e if isinstance(e, Conflicto) else DatosInvalidos(str(e))) from e
        documentos = [self.servicio.guardar_documento(sesion, empresa, e.documento, e.documento.datos,
                                                      set_id=registro.id, caso=e.caso) for e in emitidos]
        envio = self.servicio.encolar_envio(sesion, empresa, documentos)
        registro.envio = envio
        sesion.commit()
        return registro

    def _ultimo(self, sesion: Session, empresa: Empresa, *tipos_set: str) -> Optional[SetPrueba]:
        for tipo_set in tipos_set:
            registro = sesion.scalar(select(SetPrueba).where(SetPrueba.empresa_id == empresa.id,
                                                             SetPrueba.tipo_set == tipo_set)
                                     .order_by(SetPrueba.creado.desc()).limit(1))
            if registro:
                return registro
        return None

    def _documentos_aceptados(self, sesion: Session, registro: Optional[SetPrueba], nombre: str) -> List[Documento]:
        if registro is None or registro.envio is None:
            raise Conflicto(f'Primero emita el set {nombre}.')
        if registro.envio.estado not in ACEPTADOS:
            raise Conflicto(f'El set {nombre} aún no está aceptado por el SII (estado {registro.envio.estado}).')
        return documentos_del_envio(sesion, registro.envio_id)

    def _emitir_libro(self, sesion: Session, registro: SetPrueba) -> SetPrueba:
        empresa = registro.empresa
        certificado = self.servicio.certificado_empresa(empresa)
        comun = dict(certificado=certificado, rut_emisor=empresa.rut, fecha_resolucion=empresa.fecha_resolucion,
                     numero_resolucion=empresa.numero_resolucion)
        ventas = self._ultimo(sesion, empresa, 'basico', 'exenta')
        try:
            if registro.tipo_set == 'libro_ventas':
                docs = self._documentos_aceptados(sesion, ventas, 'básico (o de factura exenta)')
                xml = libro_ventas([d.xml for d in docs], folio_notificacion=registro.datos.get('folio_notificacion', 1),
                                   **comun)
            elif registro.tipo_set == 'libro_compras':
                docs = self._documentos_aceptados(sesion, ventas, 'básico (o de factura exenta)')
                xml = libro_compras(registro.datos, docs[0].fecha[:7], **comun)
            else:
                guias = self._documentos_aceptados(sesion, self._ultimo(sesion, empresa, 'guia'), 'de guía de despacho')
                # La factura que facturó la guía (el set no la indica): la primera del set básico.
                facturas = self._documentos_aceptados(sesion, ventas, 'básico') if ventas and ventas.envio else []
                factura = next((d for d in facturas if d.tipo == 33), None)
                xml = libro_guias([d.xml for d in guias], registro.datos, factura={
                    'TpoDocRef': 33, 'FolioDocRef': factura.folio} if factura else None, **comun)
        except EmisionError as e:
            raise DatosInvalidos(str(e)) from e
        envio = self.servicio.encolar_libro(sesion, empresa, xml)
        registro.envio = envio
        sesion.commit()
        return registro

    # ------------------------------------------------------------------
    # Muestras impresas
    # ------------------------------------------------------------------

    def muestras(self, sesion: Session, empresa: Empresa) -> bytes:
        """ZIP con los PDF de los documentos del set de pruebas y la simulación (sin boletas)."""
        dtes = []
        for registro in sesion.scalars(select(SetPrueba).where(SetPrueba.empresa_id == empresa.id,
                                                               SetPrueba.envio_id.is_not(None))):
            if registro.tipo_set in LIBROS or registro.tipo_set == 'boletas':
                continue
            dtes += [d.xml for d in documentos_del_envio(sesion, registro.envio_id)]
        if not dtes:
            raise Conflicto('No hay documentos emitidos del set de pruebas ni de la simulación.')
        return zip_muestras(muestras_impresas(dtes, empresa.fecha_resolucion, empresa.numero_resolucion))


def documentos_del_envio(sesion: Session, envio_id: str) -> List[Documento]:
    return list(sesion.scalars(select(Documento).where(Documento.envio_id == envio_id)
                               .order_by(Documento.creado, Documento.tipo, Documento.folio)))
