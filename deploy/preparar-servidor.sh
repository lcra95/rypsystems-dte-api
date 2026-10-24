#!/bin/sh
# Prepara un servidor Ubuntu 24.04 recién creado (ejecutar como root, una vez):
#   curl -fsSL https://raw.githubusercontent.com/lcra95/rypsystems-dte-api/main/deploy/preparar-servidor.sh -o preparar.sh
#   less preparar.sh && sh preparar.sh
#
# Instala Docker, activa el firewall (SSH, 80 y 443), las actualizaciones de
# seguridad automáticas y clona el servicio en /opt/rypsystems-dte-api.
set -eu
[ "$(id -u)" = 0 ] || { echo "Ejecutar como root." >&2; exit 1; }
. /etc/os-release
[ "$ID" = ubuntu ] || echo "AVISO: probado en Ubuntu 24.04; este sistema es $PRETTY_NAME."

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl git openssl ufw unattended-upgrades fail2ban

# Docker desde su repositorio oficial
if ! command -v docker >/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

# Firewall: Docker publica 80/443 por su cuenta; aquí se cierra todo lo demás.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now fail2ban

# Swap de 2 GB si no hay (margen para builds en servidores de 2–4 GB)
if [ "$(swapon --show | wc -l)" = 0 ]; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if [ ! -d /opt/rypsystems-dte-api ]; then
    git clone https://github.com/lcra95/rypsystems-dte-api.git /opt/rypsystems-dte-api
fi
echo
echo "Listo. Siguiente:"
echo "  cd /opt/rypsystems-dte-api/deploy"
echo "  ./generar-secretos.sh dte.midominio.cl admin@midominio.cl"
echo "  ./desplegar.sh v0.1.0"
