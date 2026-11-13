#!/usr/bin/env bash
# Instalador de Broker Papel. Uso: bash instalar.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
echo "== Instalando en $DIR =="
if ! python3 -m venv --help >/dev/null 2>&1 || ! dpkg -s python3-venv >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq python3-venv
fi
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
[ -f config/config.toml ] || cp config/config.ejemplo.toml config/config.toml
[ -f config/secretos.toml ] || cp config/secretos.ejemplo.toml config/secretos.toml
chmod 600 config/secretos.toml
mkdir -p data
sed "s#/root/broker_papel#$DIR#g" deploy/broker-papel.service > /etc/systemd/system/broker-papel.service
ln -sf "$DIR/broker" /usr/local/bin/broker
systemctl daemon-reload
systemctl enable broker-papel >/dev/null
echo "== Probando los cálculos =="
venv/bin/python -m pytest -q tests
echo
echo "Listo. Ahora:"
echo "  1) nano $DIR/config/secretos.toml   (pega el token de tu bot)"
echo "  2) broker arrancar"
