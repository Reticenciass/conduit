#!/usr/bin/env bash
set -Eeuo pipefail

# Conduit bootstrap installer.
# It downloads a selected repository revision to a stable release directory and
# delegates the actual system setup to deploy/install-linux.sh.

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root, for example: curl ... | sudo bash" >&2
  exit 1
fi

repository="${CONDUIT_REPOSITORY:-Reticenciass/conduit}"
revision="${CONDUIT_REF:-main}"
github_token="${CONDUIT_GITHUB_TOKEN:-${GITHUB_TOKEN:-}}"

for required_command in curl tar find mktemp date install mv tr bash; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Missing required command: ${required_command}" >&2
    exit 1
  fi
done

temporary_dir="$(mktemp -d -t conduit-install.XXXXXX)"
cleanup() {
  rm -rf -- "${temporary_dir}"
}
trap cleanup EXIT

archive="${temporary_dir}/conduit.tar.gz"
extract_dir="${temporary_dir}/source"
archive_url="https://api.github.com/repos/${repository}/tarball/${revision}"

curl_options=(
  --fail
  --silent
  --show-error
  --location
  --retry 3
  --retry-delay 1
  --proto "=https"
  --tlsv1.2
  --header "Accept: application/vnd.github+json"
)
if [[ -n "${github_token}" ]]; then
  curl_options+=(--header "Authorization: Bearer ${github_token}")
fi

echo "Downloading Conduit ${repository}@${revision}..."
curl "${curl_options[@]}" "${archive_url}" --output "${archive}"

install -d -m 0750 "${extract_dir}"
tar -xzf "${archive}" -C "${extract_dir}"
source_dir="$(find "${extract_dir}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
if [[ -z "${source_dir}" || ! -f "${source_dir}/deploy/install-linux.sh" ]]; then
  echo "Downloaded archive does not contain deploy/install-linux.sh." >&2
  exit 1
fi

safe_revision="$(printf '%s' "${revision}" | tr -c 'A-Za-z0-9._-' '-')"
timestamp="$(date -u +%Y%m%d%H%M%S)"
release_dir="/opt/ctfws/releases/${safe_revision}-${timestamp}"
install -d -m 0750 "/opt/ctfws/releases"
mv -- "${source_dir}" "${release_dir}"

echo "Installing from ${release_dir}..."
bash "${release_dir}/deploy/install-linux.sh"
echo "Conduit installation completed. Run: conduit start"
