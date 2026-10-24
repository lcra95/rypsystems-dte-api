# Despliegue en producción

Un servidor con Docker corre todo el servicio: Caddy (HTTPS automático), la API, el worker, PostgreSQL y el respaldo diario. Solo Caddy publica puertos (80 y 443).

| Archivo | Para qué |
|---|---|
| `preparar-servidor.sh` | Una vez, en un Ubuntu 24.04 nuevo: Docker, firewall, actualizaciones de seguridad, fail2ban, swap y clon del repositorio en `/opt/rypsystems-dte-api`. |
| `generar-secretos.sh` | Una vez: crea `deploy/.env` con el token, la clave maestra y la clave de PostgreSQL aleatorios (permisos 600, fuera de git). |
| `desplegar.sh TAG` | Cada versión: checkout del tag, respaldo previo, build, migraciones, reinicio y espera hasta que la API esté sana. |
| `compose.prod.yml`, `Caddyfile` | Los servicios y el proxy. |
| `respaldo/` | Imagen de respaldo: `pg_dump` diario, cifrado con age, copia con rclone y aviso por ping. |
| `env.prod.example` | Todas las variables, comentadas. |

Requisitos: 2 vCPU, 4 GB de RAM y 40 GB o más de SSD. Un dominio con registro DNS `A` hacia el servidor.

## 1. Antes de crear el servidor

1. **Dominio**: crear el registro `A` de, por ejemplo, `dte.midominio.cl` hacia la IP del servidor. Caddy necesita que resuelva para obtener el certificado.
2. **Clave de los respaldos** (en tu equipo, no en el servidor):
   ```bash
   brew install age          # o apt install age
   age-keygen -o clave-respaldos-dte.txt
   ```
   Guarda ese archivo en tu gestor de contraseñas. Su línea `# public key: age1...` va al servidor; sin la clave privada nadie puede leer los respaldos, ni siquiera quien robe el bucket.
3. **Bucket para los respaldos** (Backblaze B2, Cloudflare R2 o S3), con una llave que solo pueda escribir en ese bucket. Reglas de ciclo de vida: `diarios/` 30 días y `mensuales/` 7 años. El SII exige conservar los documentos 6 años.
4. **Monitoreo** (opcional, gratis): un check en healthchecks.io para los respaldos, y otro de uptime (UptimeRobot, Better Stack) contra `https://dte.midominio.cl/salud`, que responde 503 si la base de datos no está disponible.

## 2. Preparar el servidor

```bash
ssh root@IP
curl -fsSL https://raw.githubusercontent.com/lcra95/rypsystems-dte-api/main/deploy/preparar-servidor.sh -o preparar.sh
less preparar.sh            # revisar antes de ejecutar
sh preparar.sh
```

Recomendado después: crear un usuario con sudo, entrar solo con llave SSH y desactivar el acceso de root por contraseña (`PasswordAuthentication no` en `/etc/ssh/sshd_config`).

## 3. Configurar y desplegar

```bash
cd /opt/rypsystems-dte-api/deploy
./generar-secretos.sh dte.midominio.cl admin@midominio.cl
nano .env                   # RESPALDO_AGE_DESTINATARIO, RESPALDO_DESTINO y RCLONE_CONFIG_*, RESPALDO_AVISO_URL,
                            # DTE_WEBHOOK_URL, DTE_IPS_PERMITIDAS, DTE_BCCH_TOKEN
./desplegar.sh v0.2.0
```

**En ese momento copia `DTE_MASTER_KEY` y `DTE_SERVICE_TOKEN` a tu gestor de contraseñas.** Sin la clave maestra, una base restaurada no sirve: los certificados y los CAF están cifrados con ella.

Comprobar:
```bash
curl https://dte.midominio.cl/salud                      # {"estado":"ok",...}
curl https://dte.midominio.cl/licencia                   # URL del código fuente (AGPL)
docker compose -f compose.prod.yml ps                    # api y worker "healthy"
docker compose -f compose.prod.yml exec respaldo respaldar.sh ahora
```

## 4. Primera empresa en producción

Con el token (`Authorization: Bearer ...`), igual que en certificación (ver el README principal):

1. `POST /v1/certificados`: el `.pfx` y su clave.
2. `POST /v1/empresas` con `"ambiente": "produccion"`, la resolución de producción (RYP: `numero_resolucion` 80, `fecha_resolucion` `2014-08-22`) y el `certificado_id`.
3. `POST /v1/empresas/{rut}/cafs`: los CAF descargados de **palena.sii.cl**. Los de certificación se rechazan.

## Operación

| Tarea | Comando (desde `deploy/`) |
|---|---|
| Actualizar a otra versión | `./desplegar.sh v0.X.Y` (respalda antes) |
| Volver a la versión anterior | `./desplegar.sh v0.anterior`. Las migraciones no se revierten solas: revisa el changelog antes. |
| Ver logs | `docker compose -f compose.prod.yml logs -f api worker` |
| Respaldo inmediato | `docker compose -f compose.prod.yml exec respaldo respaldar.sh ahora` |
| Probar un respaldo (sin tocar producción) | copiar la clave age a `respaldos/.clave` y `docker compose -f compose.prod.yml exec -e PGDATABASE=dte_prueba respaldo restaurar.sh /respaldos/dte-AAAA-MM-DD.dump.age /respaldos/.clave`; luego borrar `respaldos/.clave` |
| Restaurar producción | `docker compose -f compose.prod.yml stop api worker`, `restaurar.sh` como arriba pero sin `PGDATABASE`, y `start api worker` |

Haz una prueba de restauración al menos una vez al trimestre.

## Si el sistema principal corre en el mismo servidor

Únelo a la red `rypsystems-dte` y llama a `http://api:8000` sin pasar por internet:

```yaml
# compose del sistema principal
networks:
  default: {}
  dte: {name: rypsystems-dte, external: true}
services:
  web:
    networks: [default, dte]
```

## Probar este despliegue en un equipo propio

```bash
./generar-secretos.sh localhost tu@correo.cl
printf 'HTTP_PORT=8080\nHTTPS_PORT=8443\n' >> .env
./desplegar.sh --local
curl -k https://localhost:8443/salud
docker compose -f compose.prod.yml down -v && rm .env    # borra todo
```
