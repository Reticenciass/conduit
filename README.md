# Conduit

**Conduit** is an operations workspace for CTFs, labs, and authorized security assessments. It
helps organize hosts, networks, SSH connections, terminals, tunnels, files, evidence, and
activity history in one place.

The goal is to turn a workflow spread across many terminals and ad-hoc commands into a visual,
explainable, and easy-to-resume workspace.

> Use Conduit only on systems you own or are explicitly authorized to assess. It does not perform
> automatic exploitation, brute force, persistence, or destructive actions.

## Start here

The operational engine should run on Kali Linux or another Linux server that can reach the
infrastructure. A Windows browser can be used as the interface.

### 1. Install

```bash
git clone https://github.com/Reticenciass/conduit.git
cd conduit

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[web]"
```

Main requirements:

- Python 3.12 or newer;
- Linux for the operational engine;
- `ssh` and `sftp` available on the engine host;
- Node.js only when developing or rebuilding the web interface.

On Windows PowerShell, activate the virtual environment with:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[web]"
```

### One-line installer on Linux

For a public copy of the repository, the standard bootstrap command is:

```bash
curl -fsSL --proto '=https' --tlsv1.2 \
  https://raw.githubusercontent.com/Reticenciass/conduit/main/install.sh | sudo bash
```

This repository is private, so GitHub will not serve the raw installer anonymously. Authenticate
the request with a short-lived or read-only repository token. The following keeps the token out of
shell history; use a token with repository Contents read access only:

```bash
read -rsp "GitHub token: " CONDUIT_GITHUB_TOKEN
printf '\n'
export CONDUIT_GITHUB_TOKEN
curl -fsSL --proto '=https' --tlsv1.2 \
  --header "Authorization: Bearer ${CONDUIT_GITHUB_TOKEN}" \
  https://raw.githubusercontent.com/Reticenciass/conduit/main/install.sh \
  | sudo --preserve-env=CONDUIT_GITHUB_TOKEN bash
unset CONDUIT_GITHUB_TOKEN
```

For a sensitive environment, download the script first, review it, and then execute it. For this
private repository, keep the authenticated environment from the previous example:

```bash
read -rsp "GitHub token: " CONDUIT_GITHUB_TOKEN
printf '\n'
export CONDUIT_GITHUB_TOKEN
curl -fsSL --proto '=https' --tlsv1.2 \
  --header "Authorization: Bearer ${CONDUIT_GITHUB_TOKEN}" \
  https://raw.githubusercontent.com/Reticenciass/conduit/main/install.sh -o /tmp/conduit-install.sh
less /tmp/conduit-install.sh
sudo --preserve-env=CONDUIT_GITHUB_TOKEN CONDUIT_REF=main bash /tmp/conduit-install.sh
unset CONDUIT_GITHUB_TOKEN
```

Replace `main` with a tag or commit SHA to pin the downloaded source. The bootstrap stores the
selected source under `/opt/ctfws/releases/` and delegates system setup to
`deploy/install-linux.sh`. This keeps the installed service independent of the temporary download
directory.

### 2. Create or select a workspace

```bash
conduit lab create pivot-lab --base-dir ~/ctf
```

This creates `~/ctf/pivot-lab`, which contains the lab database and workspace organization.

### 3. Start with one command

From inside the workspace:

```bash
cd ~/ctf/pivot-lab
conduit start
```

Or provide the path explicitly:

```bash
conduit --workspace ~/ctf/pivot-lab start
```

`conduit start` looks for a workspace in this order:

1. `--workspace`, when provided;
2. `./workspace.db` in the current directory;
3. the `CONDUIT_WORKSPACE` environment variable;
4. Conduit default directories and labs under `~/ctf` or `~/conduit`.

If more than one workspace is found, Conduit lists the candidates and asks you to choose one
explicitly. Use a different port when needed:

```bash
conduit --workspace ~/ctf/pivot-lab start --port 8876
```

The legacy `ctfws` command remains available as a compatibility alias.

### 4. Access it from Windows

By default, the engine listens only on `127.0.0.1`. From another Windows terminal, forward the
port over SSH:

```bash
ssh -N -L 8765:127.0.0.1:8765 kali@KALI_IP
```

Then open this address in your browser:

```text
http://127.0.0.1:8765
```

`conduit start` prints this forwarding pattern when it starts. Replace `KALI_IP` with the Kali
address and keep the forwarding terminal open while using the dashboard.

## Typical workflow

The normal workflow is intentionally short:

```text
Start Conduit
      ↓
Connect over SSH
      ↓
Inspect interfaces, routes, and services
      ↓
Choose a terminal, tunnel, or network context
      ↓
Work and record evidence
      ↓
Resume the workspace next time
```

In the web interface:

1. Click **Connect machine** and paste a command such as `ssh user@10.10.10.20`.
2. Review the destination, port, key, jump hosts, and authentication method.
3. Use **Connect and inspect** to collect read-only information from the machine.
4. Open one or more independent terminals over the same SSH connection.
5. Use **Tunnel** to create a reviewable forwarding plan. The tunnel starts only after you
   confirm the plan.
6. Use **Files** to upload or download tools and evidence with progress and hashing.
7. Register important items as evidence with a description, tags, and machine association.
8. In **Activity**, use **Resume environment** after restarting the engine. Resuming is manual:
   connections, tunnels, and commands are not silently re-executed.

Conduit always shows where an action will run: on the remote machine, on Kali, or inside a
routed network context.

Inspection results are not hidden in the background: open **Activity** to watch the job update,
expand the inspection, and read each collected command output. The same output is preserved in
the collection history and can be retrieved through `GET /api/v1/collections/{id}` (or the
workspace-scoped v2 equivalent). If a collection is partial, completed steps and their output
remain available alongside the failure details.

## Core concepts

| Concept | Purpose |
| --- | --- |
| Workspace | An isolated folder containing the database, notes, evidence, and history for one job. |
| SSH profile | Non-secret connection data: user, host, port, key, and jump hosts. |
| Connection | The current operational state and capabilities of an SSH profile. |
| Terminal | An interactive remote channel or a local terminal running on the Linux engine. |
| Tunnel | An SSH, Chisel, nc, or other transport plan started explicitly by the operator. |
| Context | A SOCKS or routed environment with its own network scope and dependencies. |
| Snapshot | A point-in-time collection that preserves complete and partial results. |
| Evidence | A file, observation, or important fact with provenance and workspace metadata. |

## Useful commands

All commands below can receive `--workspace PATH`. When you are inside the workspace, that option
can be omitted.

### Diagnostics and overview

```bash
conduit doctor
conduit map
conduit search "term"
conduit report --format all
```

### Hosts and notes

```bash
conduit host add 10.10.10.20 --name web01
conduit host list
conduit host show web01
conduit note add host 1 "Internal interface found"
conduit event list
```

### Import observed information

The importer records operator-provided output; it does not execute the contents of an input file.

```bash
conduit import ip web01 --file ip_addr.txt
conduit import route web01 --file ip_route.txt
conduit import ss web01 --file ss.txt
conduit import resolv web01 --file resolv.conf
```

To compare collection rounds:

```bash
conduit snapshot create "round-01" --purpose "initial inventory"
conduit import ip web01 --file ip_addr.txt --snapshot 1
conduit source list --host web01
conduit diff web01 --command "ip route"
```

### Profiles and tunnels

```bash
conduit profile parse "ssh user@10.10.10.20 -p 2222"
conduit profile add web01 "ssh user@10.10.10.20 -p 2222" --auth-method agent_or_key
conduit profile list

conduit forward add --name db --via web01 \
  --local-port 13306 --target-address 172.16.50.20 --target-port 3306 --tool ssh
conduit forward list
conduit forward check 1
conduit forward start 1 --yes
conduit forward stop 1
```

`forward add` creates a plan. `forward start` displays the operation and requires confirmation.
An active tunnel is not automatically treated as proof that the destination service is healthy;
transport, listener, and destination are checked separately.

### Terminals and contexts

```bash
conduit tui
conduit session list
conduit context list
conduit context plan --help
conduit resume-plan
```

The web terminal supports control keys, resizing, UTF-8, ANSI, tabs, split views, search, and
explicit view sharing. Only one observer controls input at a time.

## Security and important limits

- Use Conduit only on authorized infrastructure.
- The API listens on loopback by default; do not expose it directly without suitable
  authentication and firewall rules.
- Passwords and passphrases are temporary by default and are not placed in process arguments,
  history, or logs.
- Unknown and changed host keys are shown as different conditions; there is no silent acceptance.
- Tunnels, transfers, contexts, and commands have their own confirmation and lifecycle state.
- Conduit does not install persistence, perform brute force, or start automatic exploitation.
- `proxychains` is limited to programs compatible with TCP proxying; it is not full traffic
  isolation.
- The Ligolo-ng routed context is experimental and remains disabled when the adapter version is
  not pinned and validated.
- The privileged namespace helper accepts typed operations; it does not execute arbitrary shell
  commands.

## Linux service installation

For a dedicated installation, the installer creates the `ctfws` service user, the default
workspace, the systemd service, and `/usr/local/bin/conduit`:

```bash
sudo bash deploy/install-linux.sh
```

This command is safe to run from a checkout under a user's home directory: production installs
the package as a wheel, so the restricted service user does not depend on that checkout. If the
service is already running, the installer restarts it so the new release is actually loaded.

The installer prints the initial bootstrap code once. Store it securely and enter it in the
**Bootstrap code** field on the first login. This creates an initial administrator session; it does
not create a local account automatically. After login, open **Settings → Accounts**, choose a
username, a password of at least 12 characters, and a role, then click **Create account**. Use
**Operator** for normal authorized operations, **Observer** for read-only access, and **Administrator**
only for account and policy management. Public self-registration is intentionally disabled.

New SSH hosts are also handled interactively. Conduit stops before authentication, shows the host
key type and SHA-256 fingerprint, and asks for an explicit confirmation. Verify the fingerprint
out-of-band before choosing **Trust and continue**; the key is then recorded in the engine user's
`known_hosts` file and the connection test is retried. Changed keys remain a hard failure.

To inspect the service:

```bash
sudo systemctl status ctfws.service
sudo journalctl -u ctfws.service -f
```

The service keeps the name `ctfws.service` for compatibility, but runs the engine through the
official `conduit start` command.

When the installer is run through `sudo`, it also aligns `conduit` and `ctfws` in the invoking
user's `~/.local/bin` with the managed virtual environment. Any older launcher is moved to a
timestamped backup instead of being deleted. If the service is already running, `conduit start`
reports the existing service and does not create a competing engine instance.

## Persistence, backup, and recovery

Each workspace has its own SQLite database. Conduit preserves snapshots, partial results,
provenance, and historical evidence instead of silently replacing them with the current
projection.

```bash
conduit backup --full
conduit doctor
conduit resume-plan
```

Restarting the engine must not reopen connections or repeat commands automatically. Resources
that were lost are marked for review and only return after an explicit resume action.

## Development

Install development dependencies:

```bash
python -m pip install -e ".[dev,web]"
```

Run the main validations:

```bash
python -m pytest -q
python -m ruff check ctfws tests
python -m black --check ctfws tests
python -m mypy ctfws
```

To rebuild the React/xterm interface:

```bash
cd frontend
npm install
npm run build
```

The build is written to `ctfws/frontend/dist` and included in the Python package.

## Project layout

```text
ctfws/
├── cli/              official `conduit` CLI and `ctfws` alias
├── core/             paths, errors, logging, and time utilities
├── database/         SQLite, migrations, and repositories
├── models/           Pydantic contracts
├── services/         connections, sessions, tunnels, files, and evidence
├── pivot/            transport adapters and reviewable plans
├── web.py            FastAPI, SSE, WebSocket, and web fallback
└── tui/              Textual terminal interface
frontend/             React, TypeScript, Vite, and xterm.js
deploy/               systemd and Linux installation files
tests/                unit and integration tests
docs/                 architecture, operations, and requirements
```

## Further documentation

- [Daily operations](docs/operations.md)
- [Architecture](docs/architecture.md)
- [Requirements and limitations matrix](docs/requirements-matrix.md)

## License

MIT.
