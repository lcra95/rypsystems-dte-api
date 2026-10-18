FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

# Core rypsystems-dte (contexto adicional "core" en docker-compose). Solo el código:
# nada de output/, certificados ni entornos virtuales del repositorio del core.
COPY --from=core pyproject.toml README.md COPYING NOTICE /core/
COPY --from=core src /core/src
RUN pip install --no-cache-dir /core

COPY pyproject.toml COPYING NOTICE ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .

RUN useradd --system --uid 1000 dte
USER dte
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
