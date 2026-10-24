#!/bin/sh
# Despliega (o actualiza) una versión publicada del servicio en este servidor.
#
#   ./desplegar.sh v0.1.0        # tag de github.com/lcra95/rypsystems-dte-api
#   ./desplegar.sh --local       # el código tal como está (pruebas)
#
# Hace: checkout del tag, respaldo previo (si ya hay base), build, migraciones
# y reinicio; termina cuando la API responde sana.
set -eu
cd "$(dirname "$0")"
[ -f .env ] || { echo "Falta deploy/.env: ejecute ./generar-secretos.sh" >&2; exit 1; }
version=${1:?uso: ./desplegar.sh TAG | --local}
compose="docker compose -f compose.prod.yml"

if [ "$version" != "--local" ]; then
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        echo "Hay cambios locales en el repositorio; no se despliega." >&2; exit 1
    fi
    git fetch --tags --quiet
    git -c advice.detachedHead=false checkout --quiet "$version"
else
    version=local
fi
export DTE_VERSION="$version"

if $compose ps --status running --services 2>/dev/null | grep -qx db; then
    echo "Respaldo previo al despliegue..."
    $compose run --rm --no-deps respaldo respaldar.sh ahora
fi

$compose build --pull
$compose up -d --remove-orphans
echo "Esperando que la API esté sana..."
for _ in $(seq 1 60); do
    estado=$($compose ps --format '{{.Health}}' api 2>/dev/null || true)
    [ "$estado" = "healthy" ] && break
    sleep 5
done
$compose ps
[ "$estado" = "healthy" ] || { echo "La API no quedó sana: $compose logs api" >&2; exit 1; }
echo "Desplegada la versión $version."
