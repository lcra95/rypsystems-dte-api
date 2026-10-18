"""
Configuración del servicio desde variables de entorno (prefijo DTE_).

- DTE_DATABASE_URL: base de datos (PostgreSQL en producción).
- DTE_SERVICE_TOKEN: token que debe enviar el sistema principal (Authorization: Bearer ...).
- DTE_MASTER_KEY: clave Fernet con la que se cifran certificados, contraseñas y CAF.
  Generar con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
- DTE_WEBHOOK_URL / DTE_WEBHOOK_SECRET: aviso de cambios de estado al sistema principal (HMAC-SHA256).
- DTE_PERMITIR_CAF_FALSOS: acepta CAF falsos (IDK 666), solo para pruebas.
- DTE_CODIGO_FUENTE: URL del código fuente (AGPL, sección 13), informada en /licencia.
- DTE_BCCH_TOKEN (o DTE_BCCH_USUARIO y DTE_BCCH_CLAVE): API de la BDE del Banco Central,
  para el tipo de cambio de los documentos de exportación.
- DTE_BCCH_SERIES: JSON con la serie por moneda distinta del dólar, p. ej.
  {"EURO": "F072.CLP.EUR.N.O.D"} o {"EURO": {"paridad": "..."}}.
"""

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


def _bool(valor: Optional[str]) -> bool:
    return (valor or '').strip().lower() in ('1', 'true', 'si', 'sí', 'yes')


@dataclass(frozen=True)
class Settings:
    database_url: str = 'sqlite:///./dte.db'
    service_token: str = ''
    master_key: str = ''
    webhook_url: Optional[str] = None
    webhook_secret: str = ''
    permitir_caf_falsos: bool = False
    codigo_fuente: str = 'Solicitar a RYP AI SYSTEMS SPA'
    bcch_token: str = ''
    bcch_usuario: str = ''
    bcch_clave: str = ''
    bcch_series: Optional[dict] = None
    # Espera entre consultas de estado al SII (segundos): primera y máxima.
    consulta_espera_inicial: int = 15
    consulta_espera_maxima: int = 600
    # Tiempo máximo consultando un envío antes de marcarlo con error.
    consulta_plazo_horas: int = 48
    tareas_max_intentos: int = 8

    @classmethod
    def desde_entorno(cls) -> 'Settings':
        env = os.environ
        return cls(
            database_url=env.get('DTE_DATABASE_URL', cls.database_url),
            service_token=env.get('DTE_SERVICE_TOKEN', ''),
            master_key=env.get('DTE_MASTER_KEY', ''),
            webhook_url=env.get('DTE_WEBHOOK_URL') or None,
            webhook_secret=env.get('DTE_WEBHOOK_SECRET', ''),
            permitir_caf_falsos=_bool(env.get('DTE_PERMITIR_CAF_FALSOS')),
            codigo_fuente=env.get('DTE_CODIGO_FUENTE', cls.codigo_fuente),
            bcch_token=env.get('DTE_BCCH_TOKEN', ''),
            bcch_usuario=env.get('DTE_BCCH_USUARIO', ''),
            bcch_clave=env.get('DTE_BCCH_CLAVE', ''),
            bcch_series=json.loads(env['DTE_BCCH_SERIES']) if env.get('DTE_BCCH_SERIES') else None,
        )

    def validar(self) -> None:
        faltan = [n for n, v in (('DTE_SERVICE_TOKEN', self.service_token), ('DTE_MASTER_KEY', self.master_key))
                  if not v]
        if faltan:
            raise RuntimeError(f'Faltan variables de entorno: {", ".join(faltan)}')
        if self.webhook_url and not self.webhook_secret:
            raise RuntimeError('DTE_WEBHOOK_URL requiere DTE_WEBHOOK_SECRET.')


@lru_cache
def get_settings() -> Settings:
    settings = Settings.desde_entorno()
    settings.validar()
    return settings
