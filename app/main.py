"""
Servicio DTE de RYP: API REST interna de facturación electrónica.

Solo la usa el sistema principal (token de servicio). Emite, timbra, firma,
envía al SII, consulta estados, genera PDF y anula documentos con el core
rypsystems-dte.

    uvicorn app.main:create_app --factory   # API
    python -m app.worker                    # envíos y consultas al SII
"""

from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from rypsystems_dte import BancoCentral
from rypsystems_dte.errors import RypDteError

from . import __version__
from .api import certificacion, documentos, empresas, recibidos
from .api.deps import autenticar
from .config import Settings, get_settings
from .db import Database
from .seguridad import Cifrador
from .certificacion import Certificacion
from .recibidos import Recibidos
from .servicio import Servicio, ServicioError


def banco_central(settings: Settings) -> Optional[BancoCentral]:
    if not (settings.bcch_token or (settings.bcch_usuario and settings.bcch_clave)):
        return None
    return BancoCentral(token=settings.bcch_token or None, usuario=settings.bcch_usuario or None,
                        clave=settings.bcch_clave or None, series=settings.bcch_series)


def create_app(settings: Optional[Settings] = None, database: Optional[Database] = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title='RYP Servicio DTE',
        version=__version__,
        description='API interna de facturación electrónica (SII de Chile). '
                    'Código bajo AGPL-3.0-or-later; ver /licencia.',
    )
    app.state.settings = settings
    app.state.db = database or Database(settings.database_url)
    app.state.servicio = Servicio(Cifrador(settings.master_key), settings.permitir_caf_falsos,
                                  banco_central=banco_central(settings))
    app.state.recibidos = Recibidos(app.state.servicio)
    app.state.certificacion = Certificacion(app.state.servicio)

    protegido = [Depends(autenticar)]
    app.include_router(empresas.router, prefix='/v1', dependencies=protegido)
    app.include_router(documentos.router, prefix='/v1', dependencies=protegido)
    app.include_router(recibidos.router, prefix='/v1', dependencies=protegido)
    app.include_router(certificacion.router, prefix='/v1', dependencies=protegido)

    @app.exception_handler(ServicioError)
    async def _servicio_error(request: Request, error: ServicioError):
        return JSONResponse({'detail': str(error)}, status_code=error.status)

    @app.exception_handler(RypDteError)
    async def _core_error(request: Request, error: RypDteError):
        return JSONResponse({'detail': str(error)}, status_code=422)

    @app.get('/salud', tags=['sistema'])
    def salud():
        return {'estado': 'ok', 'version': __version__}

    @app.get('/licencia', tags=['sistema'])
    def licencia():
        """AGPL-3.0, sección 13: dónde obtener el código fuente de este servicio."""
        return {'licencia': 'AGPL-3.0-or-later', 'codigo_fuente': settings.codigo_fuente}

    return app
