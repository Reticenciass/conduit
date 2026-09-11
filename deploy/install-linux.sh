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

# Install the exact Ligolo-ng release used by the routed-context contract.
# Distro packages are intentionally not used: Kali's package may report
# "dev" and cannot prove that it matches the adapter manifest.
ligolo_version="0.9.1"
ligolo_arch="$(uname -m)"
case "${ligolo_arch}" in
  x86_64) ligolo_platform="amd64" ;;
  aarch64|arm64) ligolo_platform="arm64" ;;
  *) echo "Arquitetura ${ligolo_arch} não suportada para o Ligolo-ng gerenciado." >&2; exit 1 ;;
esac
ligolo_root="/opt/ctfws/tools/ligolo-ng/${ligolo_version}"
ligolo_tmp="$(mktemp -d /tmp/ctfws-ligolo.XXXXXX)"
trap 'rm -rf -- "${ligolo_tmp}"' EXIT
ligolo_base="https://github.com/nicocha30/ligolo-ng/releases/download/v${ligolo_version}"
ligolo_checksums="ligolo-ng_${ligolo_version}_checksums.txt"
curl -fsSL --proto '=https' --tlsv1.2 "${ligolo_base}/${ligolo_checksums}" -o "${ligolo_tmp}/${ligolo_checksums}"
download_ligolo_asset() {
  local ligolo_role="$1"
  local ligolo_asset_platform="$2"
  local ligolo_asset="ligolo-ng_${ligolo_role}_${ligolo_version}_linux_${ligolo_asset_platform}.tar.gz"
  curl -fsSL --proto '=https' --tlsv1.2 "${ligolo_base}/${ligolo_asset}" -o "${ligolo_tmp}/${ligolo_asset}"
  ligolo_expected="$(awk -v asset="${ligolo_asset}" '$2 == asset { print $1 }' "${ligolo_tmp}/${ligolo_checksums}")"
  if [[ ! "${ligolo_expected}" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "Checksum oficial não encontrado para ${ligolo_asset}." >&2
    exit 1
  fi
  printf '%s  %s\n' "${ligolo_expected}" "${ligolo_tmp}/${ligolo_asset}" | sha256sum -c -
}
download_ligolo_asset agent amd64
download_ligolo_asset agent arm64
download_ligolo_asset proxy "${ligolo_platform}"
install -d -m 0755 "${ligolo_root}"
for ligolo_role in agent proxy; do
  ligolo_asset="ligolo-ng_${ligolo_role}_${ligolo_version}_linux_${ligolo_platform}.tar.gz"
  install -d -m 0755 "${ligolo_tmp}/${ligolo_role}"
  tar -xzf "${ligolo_tmp}/${ligolo_asset}" -C "${ligolo_tmp}/${ligolo_role}"
  install -m 0755 "${ligolo_tmp}/${ligolo_role}/${ligolo_role}" "${ligolo_root}/ligolo-${ligolo_role}"
done
for agent_platform in amd64 arm64; do
  install -d -m 0755 "${ligolo_tmp}/agent-${agent_platform}"
  tar -xzf "${ligolo_tmp}/ligolo-ng_agent_${ligolo_version}_linux_${agent_platform}.tar.gz" -C "${ligolo_tmp}/agent-${agent_platform}"
  install -d -m 0755 "${ligolo_root}/agents/linux-${agent_platform}"
  install -m 0755 "${ligolo_tmp}/agent-${agent_platform}/agent" "${ligolo_root}/agents/linux-${agent_platform}/ligolo-agent"
done
ligolo_agent_path="${ligolo_root}/ligolo-agent"
ligolo_proxy_path="${ligolo_root}/ligolo-proxy"
ligolo_agent_amd64_path="${ligolo_root}/agents/linux-amd64/ligolo-agent"
ligolo_agent_arm64_path="${ligolo_root}/agents/linux-arm64/ligolo-agent"
ligolo_manifest_path="/etc/ctfws/ligolo-compatibility.toml"
ligolo_agent_sha256="$(sha256sum "${ligolo_agent_path}" | awk '{ print $1 }')"
ligolo_proxy_sha256="$(sha256sum "${ligolo_proxy_path}" | awk '{ print $1 }')"
ligolo_agent_amd64_sha256="$(sha256sum "${ligolo_agent_amd64_path}" | awk '{ print $1 }')"
ligolo_agent_arm64_sha256="$(sha256sum "${ligolo_agent_arm64_path}" | awk '{ print $1 }')"
sed \
  -e "s|^agent_sha256 =.*|agent_sha256 = \"${ligolo_agent_sha256}\"|" \
  -e "s|^proxy_sha256 =.*|proxy_sha256 = \"${ligolo_proxy_sha256}\"|" \
  -e "s|^agent_linux_amd64_sha256 =.*|agent_linux_amd64_sha256 = \"${ligolo_agent_amd64_sha256}\"|" \
  -e "s|^agent_linux_arm64_sha256 =.*|agent_linux_arm64_sha256 = \"${ligolo_agent_arm64_sha256}\"|" \
  "${project_dir}/deploy/compatibility.toml" > "${ligolo_manifest_path}"
chown ctfws:ctfws "${ligolo_manifest_path}"
chmod 0640 "${ligolo_manifest_path}"

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

# The proxy is executed as the restricted ctfws account inside a private
# network namespace. CAP_NET_ADMIN is therefore attached to this managed
# binary, never granted to the main web motor.
if command -v setcap >/dev/null 2>&1; then
  setcap cap_net_admin+ep "${ligolo_proxy_path}"
else
  echo "setcap não encontrado; contextos roteados permanecerão indisponíveis." >&2
fi

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

append_env() {
  local key="$1"
  local value="$2"
  if ! grep -qE "^${key}=" /etc/ctfws/ctfws.env; then
    printf '%s=%s\n' "${key}" "${value}" >> /etc/ctfws/ctfws.env
  fi
}

append_env CTFWS_LIGOLO_VERSION "${ligolo_version}"
append_env CTFWS_LIGOLO_MANIFEST "${ligolo_manifest_path}"
append_env CTFWS_LIGOLO_AGENT "${ligolo_agent_path}"
append_env CTFWS_LIGOLO_PROXY "${ligolo_proxy_path}"
append_env CTFWS_LIGOLO_AGENT_SHA256 "${ligolo_agent_sha256}"
append_env CTFWS_LIGOLO_PROXY_SHA256 "${ligolo_proxy_sha256}"
append_env CTFWS_LIGOLO_AGENT_AMD64 "${ligolo_agent_amd64_path}"
append_env CTFWS_LIGOLO_AGENT_AMD64_SHA256 "${ligolo_agent_amd64_sha256}"
append_env CTFWS_LIGOLO_AGENT_ARM64 "${ligolo_agent_arm64_path}"
append_env CTFWS_LIGOLO_AGENT_ARM64_SHA256 "${ligolo_agent_arm64_sha256}"
# The feature still requires the separately reviewed helper service. Keeping
# this switch in the service environment makes the capability visible as soon
# as the administrator enables that component, without enabling root network
# operations during installation.
append_env CTFWS_ENABLE_ROUTED_CONTEXTS "1"
chmod 0600 /etc/ctfws/ctfws.env
chown ctfws:ctfws /etc/ctfws/ctfws.env

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
