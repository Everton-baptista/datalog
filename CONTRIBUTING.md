# Contributing to Datalog

Open an issue or submit a focused pull request describing the workflow you want to improve, the expected outcome and how it was verified. Use synthetic data only.

## Repository Layout

| Path | Contents |
| --- | --- |
| `datalog/` | Python package: domain stores and workers (`app.py`), HTTP transport and dispatcher (`server.py`), declarative API routes (`routes.py`), OTLP receivers, AI, data observability, access control and SDK (`sdk.py`) |
| `datalog/web/` | Buildless web UI: JavaScript modules, token-based CSS, i18n dictionaries and fonts |
| `datalog/migrations/` | Numbered SQL migrations applied once in order |
| `.github/` | CI, Dependabot, issue and PR templates, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `CODEOWNERS` |
| `examples/`, `benchmarks/`, `collector/`, `reporters/` | [Examples and acceptance fixtures](examples/README.md), load and pilot benchmarks, [Collector configs](collector/README.md) and [CI reporters](reporters/README.md) |
| `docs/` | [Documentation index](docs/README.md), [architecture](docs/architecture.md), [v1 contract](docs/api-v1.md), guides and benchmarks |
| `pyproject.toml` | Package metadata, dependencies and optional extras (`sso`, `browser`, `collector`, `lineage`, `warehouses`, `ai`, `benchmark`, `dev`) |

## Workflow

1. Install the package and development tools with `python -m pip install -e ".[dev]"`, then `pre-commit install` (optional, runs the linters on every commit).
2. Start a local instance with `make run` (PostgreSQL 18 must be installed) and exercise your change in the UI or API.
3. Run the checks:
   ```sh
   ruff check . && ruff format --check .
   npx --yes @biomejs/biome@2.6.5 lint
   python -m compileall -q datalog examples benchmarks collector
   node --check datalog/web/*.js datalog/profiling.cjs reporters/jest.cjs reporters/vitest.mjs reporters/summary.mjs examples/*_acceptance.js
   ```
4. For ingestion changes, run the [Collector verification scenario](docs/api-v1.md#reproduction). For performance changes, compare against the [100k-span pilot](docs/benchmark-100k.md) under the same conditions.

Shortcut: `make install`, `make lint`, `make format`, `make check` and `make run` wrap these commands.

## Pull requests

- Keep each pull request focused on one change and describe how you verified it (the [template](.github/pull_request_template.md) lists the checks).
- CI must pass: lint (ruff, Biome), install and import checks on Python 3.10, 3.12 and 3.13, and the Docker build.
- Describe how you verified behavior changes and update [CHANGELOG.md](CHANGELOG.md) under *Unreleased*.
- Commits use short imperative subjects (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).

## Good first contributions

- **Alert destinations** (`datalog/alerts.py`): add a destination type such as Microsoft Teams, PagerDuty or e-mail next to `webhook` and `slack`.
- **Translations**: API messages live in `datalog/web/i18n-api-pt.js`; UI strings in `datalog/web/i18n-en.js`. New languages are welcome.
- **Docs**: deployment recipes (Caddy, nginx, Kubernetes) for the [self-hosting guide](README.md#self-hosting-in-production).

Issues labeled `good first issue` are a good place to start; comment on an issue before starting larger work.

## Adding an API route

Endpoints are declared in [`datalog/routes.py`](datalog/routes.py); nothing else needs to change. Append a `Route` to the domain function that owns it (`dashboard_routes`, `data_routes`, ...):

```python
(
    post(
        "/api/monitors/mute",
        lambda c: s.mute_monitor(c.project, c.body, c.user),
        permission="write",
        status=200,
        audit=("monitor.mute", "id"),
    ),
)
```

The dispatcher in `datalog/server.py` then applies origin and body-size checks, the `project` parameter, authentication (`auth`: session, ingestion key or project token via `capability`), the role in `permission`, quota reservation (`quota=True`) and auditing of failed attempts (`audit`). Handlers receive a `Context` (`c.project`, `c.params`, `c.body`, `c.user`) and return JSON data; raise `Invalid` (400) or `AccessError(status, message)` for errors. Unexpected exceptions become a `500` without leaking internals.

## Conventions

- **Database**: every schema change goes into a new numbered migration `datalog/migrations/NNN_description.sql`. Never edit published migrations.
- **API contract**: document client-facing format changes in [docs/api-v1.md](docs/api-v1.md) and preserve existing aliases.
- **Language**: code, comments, tests, docs and every message the API returns are written in English. Error responses are `{"error": "<English message>", "code": "<stable code>"}` (codes such as `invalid_request`, `not_found`, `rate_limited`); clients should key on `code`. Add the Portuguese translation of any new API message to `datalog/web/i18n-api-pt.js`.
- **Web UI**: the UI markup is still authored in Portuguese, with English entries in `datalog/web/i18n-en.js`. Colors, radii and fonts must come from CSS tokens in `datalog/web/style.css`.
- **Style**: Python is linted and formatted by `ruff` (config in `pyproject.toml`); JavaScript is linted by Biome (`biome.json`). The UI files are classic browser scripts that share globals, so `noUnusedVariables` is disabled there.
- **Data hygiene**: review fixtures and logs to ensure no real secrets, personal data or credentials are committed.
- **Licensing**: code is licensed under [Apache-2.0](LICENSE). Declare third-party dependencies in [THIRD_PARTY.md](THIRD_PARTY.md) and respect [TRADEMARKS.md](TRADEMARKS.md).

By participating, you agree to the [Code of Conduct](.github/CODE_OF_CONDUCT.md). Security reports follow the [Security Policy](.github/SECURITY.md).
