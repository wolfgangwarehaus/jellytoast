# Security Policy

jellytoast handles credentials (OS keyring + an AES-GCM-encrypted local
blob) and opens a local network port (the cast proxy), so security reports
are taken seriously even though this is an alpha hobby project.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x (current) | ✅ |
| < 0.1 | ❌ |

The project is pre-1.0; fixes land on `main` and ship in the next release.

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report privately via either:

- GitHub's **private vulnerability reporting** ("Report a vulnerability"
  under the repository's Security tab), or
- email **augustvontrips@gmail.com** with `SECURITY` in the subject.

Include what you can: affected version/commit, reproduction steps, and
impact. You'll get an acknowledgement on a best-effort basis (this is a
single-maintainer project); please allow reasonable time for a fix before
any public disclosure.

## Areas of particular interest

- **Credential storage** — the dual-store (keyring + AES-GCM blob) and its
  machine-key derivation (`modules/settings.py`).
- **The cast proxy** — a local HTTP relay that serves stream URLs and
  downloaded blobs to cast devices (`modules/cast_proxy.py`): binding
  scope, the bearer token, path containment for `file://` serving, and
  upstream TLS handling.
- **Token leakage** — tokens in logs, query strings, or persisted state.
