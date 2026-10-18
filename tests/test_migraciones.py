"""Las migraciones de Alembic crean exactamente el esquema de los modelos."""

import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def alembic(*args, url):
    return subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=RAIZ, capture_output=True, text=True,
                          env={'DTE_DATABASE_URL': url, 'PATH': ''})


def test_migraciones_al_dia(tmp_path):
    url = f'sqlite:///{tmp_path / "migraciones.db"}'
    subida = alembic('upgrade', 'head', url=url)
    assert subida.returncode == 0, subida.stderr
    revision = alembic('check', url=url)
    assert revision.returncode == 0, revision.stdout + revision.stderr
    bajada = alembic('downgrade', 'base', url=url)
    assert bajada.returncode == 0, bajada.stderr
