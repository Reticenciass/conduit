#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -d -m 0750 /opt/ctfws /var/lib/ctfws /var/lib/ctfws/workspace /etc/ctfws
if ! id -u ctfws >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/ctfws --shell /usr/sbin/nologin ctfws
fi
python3 -m venv /opt/ctfws/.venv
/opt/ctfws/.venv/bin/python -m pip install --upgrade pip
/opt/ctfws/.venv/bin/python -m pip install -e "${project_dir}[web]"

install -d -m 0755 /usr/local/bin
for command_name in conduit ctfws; do
  command_link="/usr/local/bin/${command_name}"
  command_target="/opt/ctfws/.venv/bin/${command_name}"
  if [ -L "${command_link}" ] && [ "$(readlink -f "${command_link}")" = "${command_target}" ]; then
    continue
  fi
  if [ -e "${command_link}" ] || [ -L "${command_link}" ]; then
    echo "Já existe ${command_link}; não será substituído automaticamente." >&2
    exit 1
  fi
  ln -s "${command_target}" "${command_link}"
done

chown -R ctfws:ctfws /opt/ctfws /var/lib/ctfws
chmod 0750 /var/lib/ctfws /etc/ctfws

if [ ! -f /var/lib/ctfws/workspace/workspace.db ]; then
  runuser -u ctfws -- /opt/ctfws/.venv/bin/ctfws lab create workspace \
    --base-dir /var/lib/ctfws --platform linux
fi

if [ ! -s /etc/ctfws/ctfws.env ]; then
  bootstrap_token="$(/opt/ctfws/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(24))')"
  umask 0077
  printf 'CTFWS_BOOTSTRAP_TOKEN=%s\n' "${bootstrap_token}" > /etc/ctfws/ctfws.env
  chown ctfws:ctfws /etc/ctfws/ctfws.env
  echo "Código de bootstrap inicial (guarde-o; ele não será mostrado novamente): ${bootstrap_token}"
fi

install -m 0644 "${project_dir}/deploy/ctfws.service" /etc/systemd/system/ctfws.service
install -m 0644 "${project_dir}/deploy/ctfws-namespace-helper.service" /etc/systemd/system/ctfws-namespace-helper.service
systemctl daemon-reload
systemctl enable --now ctfws.service
echo "Namespace helper instalado, mas desabilitado por padrão; habilite-o somente após revisar o manifesto Ligolo e as permissões do host."
echo "Conduit instalado: execute 'conduit start' (compatibilidade: 'ctfws')."
echo "Use um encaminhamento SSH para 127.0.0.1:8765 para acessar pelo Windows."
