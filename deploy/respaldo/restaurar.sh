#!/bin/sh
# Restaura un respaldo en la base del servicio DTE (REEMPLAZA su contenido).
#
#   restaurar.sh /respaldos/dte-2026-10-24.dump[.age] [/ruta/clave-age.txt]
#
# Detener antes la API y el worker (desplegar.sh lo explica). Para probar un
# respaldo sin tocar producción: PGDATABASE=dte_prueba restaurar.sh ...
set -eu
archivo=${1:?uso: restaurar.sh ARCHIVO [CLAVE_AGE]}
clave=${2:-}
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
case "$archivo" in
    *.age) age --decrypt --identity "${clave:?el respaldo está cifrado: indicar el archivo de la clave age}" \
               --output "$tmp" "$archivo" ;;
    *) cp "$archivo" "$tmp" ;;
esac
createdb "$PGDATABASE" 2>/dev/null || true
pg_restore --clean --if-exists --no-owner --dbname="$PGDATABASE" "$tmp"
echo "restaurado $archivo en la base $PGDATABASE:"
psql -At -c "select 'documentos: ' || count(*) from documentos" -c "select 'empresas: ' || count(*) from empresas"
