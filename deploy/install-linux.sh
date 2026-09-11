#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -d -m 0755 /opt/ctfws /opt/ctfws/releases
install -d -m 0750 /var/lib/ctfws /var/lib/ctfws/workspace /etc/ctfws
if ! id -u ctfws >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/ctfws --shell /usr/sbin/nologin ctfws
fi
python3 -m venv /opt/ctfws/.venv
/opt/ctfws/.venv/bin/python -m pip install --upgrade pip
# Install a wheel, not an editable checkout.  The systemd service runs as the
# restricted ctfws user and must not depend on a clone under an operator's
# home directory.
/opt/ctfws/.venv/bin/python -m pip install "${project_dir}[web]"

if ! /opt/ctfws/.venv/bin/python -c 'import ctfws, ctfws.reports; from ctfws.main import app; print(ctfws.__file__)'; then
  echo "Conduit foi instalado, mas o pacote não pôde ser importado." >&2
  exit 1
fi
/opt/ctfws/.venv/bin/conduit --version >/dev/null
/opt/ctfws/.venv/bin/ctfws --version >/dev/null

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

install_user_launchers() {
  local invoking_user="${SUDO_USER:-}"
  if [[ -z "${invoking_user}" || "${invoking_user}" == "root" ]]; then
    return
  fi

  local user_home
  local user_group
  user_home="$(getent passwd "${invoking_user}" | cut -d: -f6)"
  user_group="$(id -gn "${invoking_user}")"
  if [[ -z "${user_home}" || ! -d "${user_home}" ]]; then
    echo "Não foi possível localizar o home de ${invoking_user}; mantendo apenas /usr/local/bin." >&2
    return
  fi

  local user_bin="${user_home}/.local/bin"
  install -d -m 0755 -o "${invoking_user}" -g "${user_group}" "${user_bin}"
  for command_name in conduit ctfws; do
    local command_link="${user_bin}/${command_name}"
    local command_target="/opt/ctfws/.venv/bin/${command_name}"
    if [ -L "${command_link}" ] && [ "$(readlink -f "${command_link}")" = "${command_target}" ]; then
      continue
    fi
    if [ -e "${command_link}" ] || [ -L "${command_link}" ]; then
      local backup_link="${command_link}.before-conduit-$(date -u +%Y%m%d%H%M%S)"
      local backup_index=0
      while [ -e "${backup_link}" ] || [ -L "${backup_link}" ]; do
        backup_index=$((backup_index + 1))
        backup_link="${command_link}.before-conduit-$(date -u +%Y%m%d%H%M%S)-${backup_index}"
      done
      mv -- "${command_link}" "${backup_link}"
      chown "${invoking_user}:${user_group}" "${backup_link}"
      echo "Launcher antigo movido para ${backup_link}."
    fi
    ln -s "${command_target}" "${command_link}"
    chown -h "${invoking_user}:${user_group}" "${command_link}"
  done
}

install_user_launchers

chown -R ctfws:ctfws /opt/ctfws /var/lib/ctfws
chmod 0755 /opt/ctfws /opt/ctfws/releases
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
if systemctl is-active --quiet ctfws.service; then
  systemctl restart ctfws.service
else
  systemctl enable --now ctfws.service
fi
if ! systemctl is-active --quiet ctfws.service; then
  echo "O serviço ctfws.service não ficou ativo; a instalação foi interrompida." >&2
  systemctl --no-pager --full status ctfws.service || true
  exit 1
fi
echo "Namespace helper instalado, mas desabilitado por padrão; habilite-o somente após revisar o manifesto Ligolo e as permissões do host."
echo "Conduit instalado: execute 'conduit start' (compatibilidade: 'ctfws')."
echo "Use um encaminhamento SSH para 127.0.0.1:8765 para acessar pelo Windows."
