#!/bin/sh
# Crea deploy/.env con claves aleatorias a partir de env.prod.example.
#   ./generar-secretos.sh dte.midominio.cl admin@midominio.cl
set -eu
cd "$(dirname "$0")"
dominio=${1:?uso: ./generar-secretos.sh DOMINIO EMAIL}
email=${2:?uso: ./generar-secretos.sh DOMINIO EMAIL}
if [ -e .env ]; then
    echo "deploy/.env ya existe; no se sobrescribe (contiene la clave maestra)." >&2
    exit 1
fi
aleatorio() { openssl rand -hex "$1"; }
# Clave Fernet: 32 bytes en base64 URL-safe
maestra=$(openssl rand -base64 32 | tr '+/' '-_')
umask 077
sed -e "s|^DTE_DOMINIO=.*|DTE_DOMINIO=$dominio|" \
    -e "s|^ACME_EMAIL=.*|ACME_EMAIL=$email|" \
    -e "s|^DTE_SERVICE_TOKEN=.*|DTE_SERVICE_TOKEN=$(aleatorio 32)|" \
    -e "s|^DTE_MASTER_KEY=.*|DTE_MASTER_KEY=$maestra|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(aleatorio 24)|" \
    -e "s|^DTE_WEBHOOK_SECRET=.*|DTE_WEBHOOK_SECRET=$(aleatorio 32)|" \
    env.prod.example > .env
echo "Creado deploy/.env (permisos 600)."
echo "AHORA: copie DTE_MASTER_KEY y DTE_SERVICE_TOKEN a su gestor de contraseñas."
