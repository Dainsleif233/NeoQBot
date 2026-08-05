<div align="center">

<img src="src/neoqbot/web/img/neoqbot-logo.svg" alt="NeoQBot logo" width="112">

# NeoQBot

**An auditable orchestration control plane for bots, groups, and knowledge resources**

[![Python](https://img.shields.io/badge/Python-3.11%2B-111111?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/github/license/LYOfficial/NeoQBot?color=111111)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.4.0-111111)](https://github.com/LYOfficial/NeoQBot)

[简体中文](README.md) · English · [日本語](README.ja.md)

[Quick start](#quick-start) · [Orchestration](#resource-orchestration) · [Deployment](#docker-deployment) · [Security](#security-boundaries) · [Documentation](#documentation)

</div>

NeoQBot is a self-hosted operations platform for bots and communities. It connects to QQ through
OneBot 11, integrates with Feishu through configurable CLI adapters, and represents bots, groups,
and knowledge bases as a visual, many-to-many resource graph.

The project began as tooling for a specific community and has evolved into a general-purpose
multi-platform control plane. Protocol adapters handle external systems, services implement
approval, recording, analysis, and synchronization workflows, and the web console provides
configuration, auditing, and orchestration.

> [!IMPORTANT]
> NeoQBot is conservative by default. Model output should be treated as operational evidence, not
> an irreversible decision. Review platform terms, account risk, and data-compliance requirements
> before connecting unofficial QQ clients or protocol implementations.

## Highlights

- **Visual resource graph** for QQ bots, Feishu bots, QQ groups, Feishu groups, and knowledge bases.
- **Many-to-many ownership** where one bot can manage several groups and several bots can cooperate
  on the same group.
- **Per-group task assignments** on every Bot-to-group edge for join management, record-only
  ingestion, analysis, handling, and announcement synchronization.
- **Group workbench** for messages, announcements, join requests, moderation reports, and managers.
- **Auditable control plane** backed by SQLite records for jobs, configuration changes, login events,
  and conflicts.
- **Safe configuration updates** with secret masking, environment injection, optimistic revisions,
  and atomic writes.
- **Desktop-style interface** inspired by Windows 11, Codex, and VS Code, with dark/light themes and
  a command palette.
- **Replaceable adapters** for OneBot, Feishu CLI tools, and OpenAI-compatible model providers.

## Architecture

```mermaid
flowchart LR
    QQ["QQ / NapCat"] -->|"OneBot 11"| API["NeoQBot API"]
    FS["Feishu CLI"] <--> API
    LLM["OpenAI-compatible LLM"] <--> API
    API --> RUNTIME["Task Runtime"]
    RUNTIME --> DB["SQLite + Audit Log"]
    GUI["Web Control Plane"] <--> API
    GUI --> GRAPH["Bot · Group · Knowledge Graph"]
```

Platform-specific behavior remains behind adapter boundaries. Replacing an OneBot implementation,
a Feishu command-line client, or a model provider should normally require configuration or adapter
changes rather than workflow rewrites.

## Resource orchestration

The orchestration canvas supports five node types:

| Node | Purpose |
| --- | --- |
| QQ Bot | OneBot / NapCat account, QR login, and connection settings |
| Feishu Bot | CLI session, archiving, and search capabilities |
| QQ Group | Messages, announcements, reviews, analysis, ownership, and Bot-specific tasks |
| Feishu Group | Collaboration, notification, and knowledge-flow target |
| Knowledge Base | Policies, playbooks, answers, or external knowledge resources |

Edges use the relations `manages`, `observes`, `archives_to`, `searches`, and `syncs`. The graph is
the only canonical source for QQ group ownership and task assignments. A `tasks` block on each
`manages` or `observes` edge means “what this Bot does in this group,” so one Bot can use different
switches, schedules, thresholds, and archive targets in different groups. See the
[orchestration guide](docs/ORCHESTRATION.md) for details.

## Quick start

### Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) is recommended
- Optional: Docker Compose, NapCatQQ, a Feishu CLI, and an OpenAI-compatible model endpoint

### Local installation

```bash
git clone https://github.com/LYOfficial/NeoQBot.git
cd NeoQBot
uv sync --extra dev
cp config.example.yaml config.yaml
uv run neoqbot init-secrets --secret-dir data/secrets
uv run neoqbot --config config.yaml init-db
uv run neoqbot --config config.yaml serve
```

On Windows PowerShell:

```powershell
Copy-Item config.example.yaml config.yaml
uv run neoqbot init-secrets --secret-dir data/secrets
uv run neoqbot --config config.yaml init-db
uv run neoqbot --config config.yaml serve
```

Open <http://127.0.0.1:8080/gui/>. The initial username is `admin`; read the random password from
`data/secrets/gui-bootstrap-password`. A password change is required on first login. Never commit
this file.
Administrators can create child users and assign initial passwords from **User management**. Child
users must also change their password on first login. They can manage bots, groups, knowledge bases,
and platform settings, but cannot create, reset, or delete other users.

## Docker deployment

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose logs -f neoqbot
docker compose exec neoqbot sh -c 'cat /app/data/secrets/gui-bootstrap-password'
```

The last command prints the random bootstrap password. Compose publishes the console on
`0.0.0.0:6688` by default, so it can be reached through the server IP. Plain public HTTP exposes
credentials in transit: restrict source addresses at the firewall and deploy HTTPS as soon as
possible. Set `NEOQBOT_GUI_BIND_IP=127.0.0.1` in `.env` when public access is unnecessary. NapCat
port `6099` and OneBot port `3000` remain inside the Compose network and must not be published.

On the first start, Compose creates the persistent configuration from `config.example.yaml` embedded
in the image. No host bind mount is required, so Git-based platforms and remote Docker daemons work
without an untracked `config.yaml`. Apply deployment-specific overrides through `.env` or platform
environment variables, and never bake secrets into the image.

Useful checks:

```bash
docker compose ps
docker compose exec neoqbot neoqbot --config /app/data/config.yaml doctor
curl http://127.0.0.1:6688/healthz
curl http://127.0.0.1:6688/readyz
```

## Configuration

Copy [config.example.yaml](config.example.yaml) to `config.yaml`. Environment variables use the
`NEOQBOT_` prefix and double underscores for nested fields:

```bash
NEOQBOT_APP__ADMIN_API_TOKEN=replace-with-a-long-random-token
NEOQBOT_LLM__API_KEY=replace-with-provider-key
```

Prefer environment variables or read-only secret files for credentials. Never commit real tokens,
cookies, QR codes, databases, or `config.yaml`. Store QQ and Feishu Bot credentials in each Bot's
read-only secret files. NeoQBot reads application configuration only from variables using the
`NEOQBOT_` prefix.

## CLI

```text
neoqbot --config config.yaml serve
neoqbot --config config.yaml init-db
neoqbot --config config.yaml doctor
neoqbot --config config.yaml show-config
neoqbot --config config.yaml run-moderation
neoqbot --config config.yaml sync-announcements
neoqbot --config config.yaml prune
neoqbot init-secrets
neoqbot init-napcat
```

## Security boundaries

- Keep `app.dry_run: true` during initial integration testing.
- Use independent random secrets for the admin API, OneBot, NapCat WebUI, and GUI bootstrap login;
  rotate all affected secrets immediately after a disclosure.
- Compose publishes the console on `0.0.0.0:6688` by default. Public deployments require firewall
  source restrictions and HTTPS; set `NEOQBOT_GUI_BIND_IP=127.0.0.1` when public access is not needed.
  NapCat WebUI and OneBot remain internal-only and must not receive public host mappings.
- Behind a trusted HTTPS proxy, set `app.require_https: true`, `gui.secure_cookie: true`, and configure
  `app.allowed_hosts`, `app.forwarded_allow_ips`, and `app.management_allowed_networks` precisely.
- Never set `app.forwarded_allow_ips` to `*`; forged proxy headers can defeat source-address controls.
- OpenAPI is disabled by default, and high-risk connection and secret settings are locked in the GUI.
- NeoQBot never stores QQ passwords; QR codes are produced by the associated NapCat instance.
- Roll out automatic approval, rejection, notification, and account actions to a minimal scope first.
- Protect and back up SQLite data, message archives, Feishu sessions, and NapCat session state.
- See [SECURITY.md](SECURITY.md) for the full hardening checklist, proxy requirements, and incident
  response procedure.

## Repository layout

```text
src/neoqbot/                         NeoQBot Python package and CLI entry point
  adapters/                          QQ, Feishu, and LLM adapters
  web/                               Control-plane frontend
  app.py                             FastAPI application and management API
  config.py                          Configuration and orchestration models
  database.py                        SQLite persistence and audit layer
  services.py                        Join, moderation, and announcement workflows
  runtime.py                         Scheduling, queueing, and lifecycle
integrations/astrbot_plugin_neoqbot_control/
assets/branding/                      Logo source image, SVG, and brand notes
docs/                                Architecture and integration guides
```

The project name, Python package, distribution, CLI, service, and environment namespace are all NeoQBot.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check src
uv run python -m unittest discover -s tests -v
node --check src/neoqbot/web/app.js
```

Changes should include relevant documentation. This repository intentionally ships without GitHub
Actions; maintainers should run the checks above in a controlled environment before merging.

## Documentation

- [Architecture and workflows](docs/ARCHITECTURE.md)
- [Resource orchestration](docs/ORCHESTRATION.md)
- [Feishu CLI integration](docs/FEISHU_CLI.md)
- [Brand assets](assets/branding/README.md)
- [Security policy and deployment checklist](SECURITY.md)
- [简体中文 README](README.md)
- [日本語 README](README.ja.md)

## Contributing

Use [Issues](https://github.com/LYOfficial/NeoQBot/issues) for bugs, adapter requests, and design
proposals. Pull requests are welcome. Changes involving platform writes or security boundaries should
document their risk and verification steps.

## License

NeoQBot is released under the [GNU General Public License v3.0](LICENSE).
