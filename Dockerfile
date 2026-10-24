FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY pyproject.toml COPYING NOTICE ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
# Instala también el core desde el tag fijado en pyproject.toml
RUN pip install --no-cache-dir .

RUN useradd --system --uid 1000 dte
USER dte
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
