# Security Policy

## Reporting

Do not open public issues for vulnerabilities. Use GitHub's private vulnerability reporting ("Report a vulnerability" in the repository's **Security** tab). Include the affected version or commit, the impact, and minimal reproduction steps using synthetic data.

## Scope

In scope: authentication, sessions and ingestion keys; project isolation; the OTLP and JSON receivers; attribute and content capture policies; retention; OIDC SSO; and the web UI.

The development cluster created by `python -m datalog.run_local` trusts its private Unix socket and is not a production deployment target. Expose Datalog only behind TLS with `DATALOG_PUBLIC_ORIGIN` configured.

## Supported Versions

Only the latest release on `main` receives security fixes while the project is pre-1.0.
