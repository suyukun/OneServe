---
name: Bug report
about: Report a reproducible defect in OneServe
title: "bug: "
labels: bug
---

<!-- Security vulnerability? Do not open an issue. Follow docs/opensource/SECURITY.md. -->

## Description

A short, concrete description of what is wrong and where.

## Steps to reproduce

1. ...
2. ...

If the bug appears on a fresh checkout, paste the quickstart commands you actually ran
and say which stack you used (minimal service, full Compose, or the self-test):

```bash
bash scripts/up.sh              # full 9-service Compose stack
bash scripts/seed_demo.sh       # demo data and a first query
```

## Expected vs actual

- **Expected:** ...
- **Actual:** ... (paste the exact error text or response body)

## Environment

- OS and architecture (`uname -a`): e.g. macOS 15 arm64, Ubuntu 24.04 x86_64
- Python version (`python3 -V`): e.g. 3.13.x
- Docker and Compose (`docker version`, `docker compose version`): e.g. Docker 27.x, Compose v2.x
- Commit or release: `git rev-parse --short HEAD`, or the image tag
- Stack: the default Compose files, or a customized one (attach your diff if customized)

## Minimal reproduction

The smallest input, configuration, or fixture that triggers the bug, plus the relevant
container log output around the failure.

Redact credentials, tokens, real records, and internal hostnames or IP addresses before posting.
