# RYP Servicio DTE

API REST **interna** de facturación electrónica para el SII de Chile. La usa solo el sistema principal de RYP, con un token de servicio. Emite, timbra y firma documentos, los envía al SII, consulta su estado, genera el PDF y los anula. Usa el core [`rypsystems-dte`](../rypsystems-dte).

No tiene lógica de negocio: los clientes, cobros, usuarios y reglas viven en el sistema principal, que llama a este servicio por HTTP.

Licencia **AGPL-3.0-or-later** (ver `COPYING` y `NOTICE`). `GET /licencia` informa dónde obtener el código fuente.

## Arquitectura

```
Sistema principal ──HTTP + Bearer──▶ API (FastAPI) ──▶ PostgreSQL ◀── worker ──▶ SII
        ▲                                                              │
        └──────────────── webhook firmado (HMAC-SHA256) ◀──────────────┘
```

- **La API** emite en forma síncrona. En una transacción asigna el folio (con `SELECT … FOR UPDATE`), normaliza, timbra, firma y valida el documento, y deja una tarea `enviar`. Responde `202`.
- **El worker** (`python -m app.worker`) procesa las tareas guardadas en la base de datos:
  - `enviar` sube el sobre: EnvioDTE, o EnvioBOLETA por la API REST de boletas.
  - `consultar` pregunta el estado con espera creciente, hasta que el SII termina.
  - `webhook` avisa al sistema principal.

  Los errores de comunicación se reintentan. Se pueden correr varios workers, porque cada uno toma tareas distintas con `SKIP LOCKED`.
- **Los secretos se guardan cifrados** con Fernet (`DTE_MASTER_KEY`): certificados .pfx, sus contraseñas y los CAF (que contienen la clave privada de timbraje).

## Puesta en marcha

```bash
cp .env.example .env            # completar DTE_SERVICE_TOKEN y DTE_MASTER_KEY
docker compose up --build       # PostgreSQL + API (127.0.0.1:8000) + worker
```

Sin Docker:

```bash
uv venv && uv pip install -e ../rypsystems-dte -e ".[test]"
export DTE_SERVICE_TOKEN=... DTE_MASTER_KEY=... DTE_DATABASE_URL=sqlite:///./dte.db
alembic upgrade head
uvicorn app.main:create_app --factory --reload
python -m app.worker
```

La documentación OpenAPI queda en `http://127.0.0.1:8000/docs`.

## Uso

Todas las rutas `/v1` exigen `Authorization: Bearer $DTE_SERVICE_TOKEN`.

### 1. Certificado, empresa y CAF

```bash
curl -X POST $API/v1/certificados -F archivo=@firma.pfx -F clave=****
# → {"id": "…", "rut": "25957199-5", "nombre": "…", "vence": "…"}

curl -X POST $API/v1/empresas -H 'Content-Type: application/json' -d '{
  "rut": "78532566-4",
  "emisor": {"RznSoc": "RYP AI SYSTEMS SPA", "GiroEmis": "…", "Acteco": 620900,
             "DirOrigen": "…", "CmnaOrigen": "San Miguel"},
  "fecha_resolucion": "2026-10-17", "numero_resolucion": 0,
  "ambiente": "certificacion", "certificado_id": "…"}'

curl -X POST $API/v1/empresas/78532566-4/cafs -F archivo=@FoliosSII…33….xml
curl $API/v1/empresas/78532566-4/folios
# Folios ya usados en otro sistema:
curl -X PATCH $API/v1/empresas/78532566-4/cafs/1 -d '{"siguiente": 31}' -H 'Content-Type: application/json'
```

Un mismo certificado puede servir a varias empresas, siempre que su titular esté autorizado en el SII para firmar y enviar por ellas. El CAF debe ser de la empresa y de su ambiente (certificación o producción).

### 2. Emitir

```bash
curl -X POST $API/v1/empresas/78532566-4/documentos -H 'Idempotency-Key: pedido-1234' \
  -H 'Content-Type: application/json' -d '{"documento": {
    "Encabezado": {"IdDoc": {"TipoDTE": 33, "FmaPago": 2, "FchVenc": "2026-11-17"},
                   "Receptor": {"RUTRecep": "60803000-K", "RznSocRecep": "…", "GiroRecep": "…",
                                "DirRecep": "…", "CmnaRecep": "…"}},
    "Detalle": [{"NmbItem": "Servicio", "QtyItem": 1, "PrcItem": 100000}]}}'
# 202 → {"id": "…", "tipo": 33, "folio": 31, "monto_total": 119000, "estado": "emitido", …}
```

- **Formato:** el documento usa el formato de LibreDTE (`Encabezado`, `Detalle`, `Referencia`, `DscRcgGlobal`…). Los totales se calculan desde el detalle.
- **Lo que pone el servicio:** el **emisor** y el **folio**. Si se envían, se rechazan o se reemplazan.
- **Boletas (39 y 41):** el receptor es opcional y los precios incluyen IVA.
- **Idempotency-Key:** si se repite la clave, se devuelve el documento ya emitido (`200`) en vez de emitir otro.

| Código | Cuándo |
|---|---|
| 401 | Token inválido |
| 404 | Empresa o documento no existe |
| 409 | Sin folios disponibles, empresa sin certificado o documento ya anulado |
| 422 | Datos del documento inválidos (esquema, RUT, montos…) |

### 3. Estado, XML y PDF

```bash
curl $API/v1/documentos/{id}                     # estado y detalle del SII
curl $API/v1/documentos/{id}/xml
curl "$API/v1/documentos/{id}/pdf?formato=carta&cedible=true"   # formato=80mm para térmica
curl "$API/v1/empresas/78532566-4/documentos?tipo=33&estado=aceptado"
```

Estados: `emitido` → `enviado` → `aceptado` | `aceptado_reparos` | `rechazado`, o `error` si no se pudo enviar o consultar. El detalle queda en `detalle_sii`.

### 4. Anular

```bash
curl -X POST $API/v1/documentos/{id}/anular -d '{"razon": "Error en el monto"}' -H 'Content-Type: application/json'
```

Emite la nota que anula el documento (CodRef 1, por el total):
- facturas y boletas: nota de crédito (61);
- notas de crédito: nota de débito (56);
- exportación: 112 o 111.

Las guías de despacho no se anulan con una nota.

### Webhooks

Con `DTE_WEBHOOK_URL`, cada cambio de estado se envía por `POST` con este cuerpo:

```json
{"evento": "documento.estado", "estado_anterior": "enviado", "documento": {"id": "…", "estado": "aceptado", …}}
```

Lleva las cabeceras `X-DTE-Evento` y `X-DTE-Firma: sha256=<HMAC-SHA256 del cuerpo con DTE_WEBHOOK_SECRET>`. Si el sistema principal no responde `2xx`, se reintenta con espera creciente.

## Tests

```bash
pytest                                            # SQLite temporal, SII simulado
DTE_TEST_DATABASE_URL=postgresql+psycopg://… pytest   # contra PostgreSQL
```

## Pendiente

- **Recibidos:** carga de EnvioDTE, respuestas de intercambio y aceptar o reclamar con `RegistroReclamoService`.
- **Certificación de nuevas empresas:** sets, simulación, intercambio y muestras impresas.
- **Varios documentos por envío:** hoy se manda uno por envío. Agruparlos reduciría las llamadas al SII.
- **Reenvío seguro:** si la conexión se corta justo después de que el SII recibió el sobre, el reintento lo vuelve a subir. El SII lo detecta como repetido; falta reconocer esa respuesta como éxito.
