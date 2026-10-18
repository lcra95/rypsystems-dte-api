# RYP Servicio DTE

API REST **interna** de facturación electrónica para el SII de Chile. La usa solo el sistema principal de RYP, con un token de servicio. Emite, timbra y firma documentos, los envía al SII, consulta su estado, genera el PDF y los anula. También recibe los documentos de proveedores, responde el intercambio y registra en el SII su aceptación o reclamo. Usa el core [`rypsystems-dte`](../rypsystems-dte).

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

### 5. Documentos recibidos

```bash
# EnvioDTE recibido por correo del proveedor
curl -X POST $API/v1/empresas/78532566-4/recibidos -F archivo=@ENVIO_DTE_123.xml
# → {"envio": {"id": "…", "cod_envio": 1, "estado": 0, "glosa": "Envio Recibido Conforme"},
#    "documentos": [{"id": "…", "tipo": 33, "folio": 52679, "estado": "pendiente", …}], "repetidos": []}

curl "$API/v1/empresas/78532566-4/recibidos?estado=pendiente"
curl $API/v1/recibidos/{id}            # también /xml y /pdf

# Respuestas del intercambio para enviar por correo al proveedor (XML firmados, en base64)
curl -X POST $API/v1/envios-recibidos/{envio_id}/respuesta -H 'Content-Type: application/json' \
  -d '{"rechazar": ["<id de un recibido>"], "nombre_contacto": "Contabilidad", "mail_contacto": "…"}'
# → {"recepcion_envio": {…}, "resultado_dte": {…}, "recibo_mercaderias": {…}}

# Registro de aceptación o reclamo en el SII (Ley 20.956)
curl -X POST $API/v1/recibidos/{id}/aceptar -d '{"recibo_mercaderias": true}' -H 'Content-Type: application/json'
curl -X POST $API/v1/recibidos/{id}/reclamar -d '{"accion": "RCD"}' -H 'Content-Type: application/json'
curl $API/v1/recibidos/{id}/eventos     # eventos en el SII y fecha de recepción
```

**Validación al cargar:** se revisan el esquema, las firmas, el RUT receptor y los timbres. Los documentos que no pasan quedan `no_recibido` y se informan rechazados en `resultado_dte`.

**Estados:** `pendiente`, `aceptado` (ACD), `recibo_mercaderias` (ERM), `reclamado` (RCD, RFP o RFT) y `no_recibido`.

**Plazo:** el receptor tiene 8 días desde la recepción en el SII para reclamar. Pasado ese plazo, el documento se entiende aceptado.

**Respuestas del SII:**
- Si el SII no registra la acción (ya registrada, plazo vencido, factura al contado…), la API responde `409` con su glosa.
- `GET …/eventos` sincroniza el estado con lo registrado en el SII, por ejemplo si se aceptó desde su sitio web.

### 6. Certificación de una empresa nueva

La empresa se registra con `"ambiente": "certificacion"`, con su certificado y con los CAF de certificación que descarga en maullin.sii.cl. Las declaraciones de avance las hace su representante legal en el sitio del SII.

```bash
# 1. Set de pruebas: el archivo SIISetDePruebas<RUT>.txt que entrega el SII (o el set de boletas)
curl -X POST $API/v1/empresas/{rut}/certificacion/sets -F archivo=@SIISetDePruebas785325664.txt \
     -F 'tipos_cambio={"DOLAR USA": 950}'          # opcional, para exportación
curl $API/v1/empresas/{rut}/certificacion          # sets y estado de cada envío

# Emitir cada set (un envío por set); los libros, después de aceptado el set en que se basan
curl -X POST $API/v1/certificacion/sets/{set_id}/emitir
curl $API/v1/certificacion/sets/{set_id}           # documentos, track id y resultado del SII

# 2. Simulación: casos con datos de la operación real (formato de examples/sets/simulacion.yaml)
curl -X POST $API/v1/empresas/{rut}/certificacion/simulacion -H 'Content-Type: application/json' \
     -d '{"casos": [{"caso": "SIM-01", "tipo": 33, "detalle": [...]}, ...]}'
curl -X POST $API/v1/certificacion/sets/{simulacion_id}/emitir

# 3. Intercambio: XML que entrega el SII → tres respuestas (base64) para subir en maullin
curl -X POST $API/v1/empresas/{rut}/certificacion/intercambio -F archivo=@ENVIO_DTE_123.xml

# 4. Documentos impresos: ZIP con un PDF por documento y por copia cedible
curl -o muestras.zip $API/v1/empresas/{rut}/certificacion/muestras
```

**El set de pruebas:**
- **Lectura:** el texto del SII se convierte con `rypsystems_dte.certificacion.leer_set_pruebas`, que reproduce exactamente los sets que el SII aceptó a RYP AI SYSTEMS.
- **Emisión:** cada documento lleva la referencia al caso. Los receptores rotan entre organismos públicos, o entre los que se entreguen en `receptores`.
- **Reemisión:** un set rechazado se puede volver a emitir, con folios nuevos.

**Resultado de cada documento:** el SII informa el resultado de un envío resumido por tipo de documento. Los documentos de un tipo con reparos o rechazos quedan en `revisar`, y el detalle está en el envío (`GET /v1/envios/{id}`).

### Tipo de cambio en exportación

Los documentos de exportación (110, 111 y 112) informan los montos en pesos en `OtraMoneda`. Si el documento no trae `TpoCambio`, el servicio lo completa con el de la moneda de la operación a la fecha de emisión. Si es fin de semana o feriado, usa el último valor publicado.

- **Producción:** usa la API de la BDE del Banco Central (`DTE_BCCH_TOKEN`, o `DTE_BCCH_USUARIO` y `DTE_BCCH_CLAVE`).
  - El dólar observado (serie `F073.TCO.PRE.Z.D`) viene configurado.
  - Las demás monedas se agregan con `DTE_BCCH_SERIES`; las series se buscan con `BancoCentral(...).buscar_series("euro")`.
  - Sin Banco Central configurado, un documento de exportación sin `TpoCambio` se rechaza con `422`.
- **Certificación:** si no hay Banco Central configurado, usa los valores referenciales del set de pruebas.

```bash
curl "$API/v1/empresas/78532566-4/tipo-cambio?moneda=DOLAR%20USA&fecha=2026-10-19"
# → {"moneda": "DOLAR USA", "fecha": "2026-10-19", "tipo_cambio": 948.75, "fuente": "BancoCentral"}
```

### Webhooks

Con `DTE_WEBHOOK_URL`, cada cambio de estado de un documento (y de un envío de varios documentos o de un libro, evento `envio.estado`) se envía por `POST` con este cuerpo:

```json
{"evento": "documento.estado", "estado_anterior": "enviado", "documento": {"id": "…", "estado": "aceptado", …}}
```

Lleva las cabeceras `X-DTE-Evento` y `X-DTE-Firma: sha256=<HMAC-SHA256 del cuerpo con DTE_WEBHOOK_SECRET>`. Si el sistema principal no responde `2xx`, se reintenta con espera creciente.

## Tests

```bash
pytest                                            # SQLite temporal, SII simulado
DTE_TEST_DATABASE_URL=postgresql+psycopg://… pytest   # contra PostgreSQL
```

Con PostgreSQL además corre `tests/test_concurrencia.py`:
- 50 emisiones en paralelo, sin folios repetidos ni reintentos;
- 4 workers sobre la misma cola, sin envíos duplicados.

Para levantar un PostgreSQL de pruebas:

```bash
docker run -d --rm --name dte-test-pg -e POSTGRES_USER=dte -e POSTGRES_PASSWORD=dte -e POSTGRES_DB=dte_test \
  -p 127.0.0.1:55432:5432 postgres:16
DTE_TEST_DATABASE_URL=postgresql+psycopg://dte:dte@127.0.0.1:55432/dte_test pytest
```

## Pendiente

- **Varios documentos por envío en la emisión normal:** la certificación ya agrupa un set por envío, pero `POST /documentos` sigue mandando uno por envío. Agruparlos reduciría las llamadas al SII.
- **Reenvío seguro:** si la conexión se corta justo después de que el SII recibió el sobre, el reintento lo vuelve a subir. El SII lo detecta como repetido; falta reconocer esa respuesta como éxito.
