# pg_dump (misma versión mayor que la base) + age (cifrado) + rclone (copia fuera del servidor)
FROM postgres:16-alpine
RUN apk add --no-cache age rclone curl tzdata
COPY respaldar.sh restaurar.sh /usr/local/bin/
ENTRYPOINT []
CMD ["respaldar.sh", "programado"]
