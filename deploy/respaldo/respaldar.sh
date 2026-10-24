#!/bin/sh
# Respaldo de PostgreSQL del servicio DTE.
#
#   respaldar.sh programado   # bucle: un respaldo diario desde RESPALDO_HORA (hora de TZ)
#   respaldar.sh ahora        # un respaldo inmediato
#
# Variables (deploy/.env):
#   RESPALDO_HORA=3                    hora local del respaldo diario
#   RESPALDO_DIAS=14                   días que se guardan en el servidor
#   RESPALDO_AGE_DESTINATARIO=age1...  clave pública age; el respaldo se cifra
#                                      (la clave privada se guarda FUERA del servidor)
#   RESPALDO_DESTINO=remoto:bucket/dte destino rclone (config. con RCLONE_CONFIG_*);
#                                      sube a diarios/ y, el día 1, a mensuales/
#   RESPALDO_AVISO_URL=https://hc-ping.com/...  ping al terminar (/fail si falla)
set -eu
umask 077

DIR=/respaldos
HORA=${RESPALDO_HORA:-3}
DIAS=${RESPALDO_DIAS:-14}

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') respaldo: $*"; }

avisar() {
    [ -n "${RESPALDO_AVISO_URL:-}" ] || return 0
    curl -fsS -m 10 --retry 3 "${RESPALDO_AVISO_URL}$1" >/dev/null || log "no se pudo avisar a RESPALDO_AVISO_URL"
}

respaldar() {
    hoy=$(date +%F)
    archivo="$DIR/dte-$hoy.dump"
    log "iniciando $archivo"
    pg_dump --format=custom --compress=9 --no-owner --file="$archivo.tmp"
    pg_restore --list "$archivo.tmp" >/dev/null   # el archivo se puede leer
    if [ -n "${RESPALDO_AGE_DESTINATARIO:-}" ]; then
        age --recipient "$RESPALDO_AGE_DESTINATARIO" --output "$archivo.age.tmp" "$archivo.tmp"
        rm -f "$archivo.tmp"
        archivo="$archivo.age"
        mv "$archivo.tmp" "$archivo"
    else
        mv "$archivo.tmp" "$archivo"
        log "AVISO: respaldo sin cifrar (definir RESPALDO_AGE_DESTINATARIO)"
    fi
    if [ -n "${RESPALDO_DESTINO:-}" ]; then
        rclone copy --no-traverse "$archivo" "$RESPALDO_DESTINO/diarios/"
        if [ "$(date +%d)" = "01" ]; then
            rclone copy --no-traverse "$archivo" "$RESPALDO_DESTINO/mensuales/"
        fi
        log "copiado a $RESPALDO_DESTINO"
    else
        log "AVISO: sin copia fuera del servidor (definir RESPALDO_DESTINO)"
    fi
    find "$DIR" -name 'dte-*.dump*' -mtime +"$DIAS" -delete
    log "listo: $archivo ($(du -h "$archivo" | cut -f1))"
}

intentar() {
    if respaldar; then
        avisar ""
    else
        rm -f "$DIR"/*.tmp
        log "ERROR: el respaldo falló"
        avisar "/fail"
        return 1
    fi
}

case "${1:-ahora}" in
    ahora) intentar ;;
    programado)
        log "programado a diario desde las ${HORA}:00 ($(date +%Z)), se guardan $DIAS días"
        while true; do
            if [ "$(date +%H | sed 's/^0//')" -ge "$HORA" ] && ! ls "$DIR/dte-$(date +%F).dump"* >/dev/null 2>&1; then
                intentar || true
            fi
            sleep 300
        done ;;
    *) echo "uso: $0 [ahora|programado]" >&2; exit 2 ;;
esac
