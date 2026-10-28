# Security Policy

## Supported versions

Security fixes are provided for the **latest release** and the **`main` branch**
only. Older releases do not receive backported fixes; if you are on an older
release, upgrade before reporting.

| Version | Supported |
|---|---|
| `main` branch | Yes |
| Latest tagged release | Yes |
| Older releases | No |

> This table is finalized at the first public release.

## Reporting a vulnerability

**Please do not report security issues in public issues, pull requests, or
discussions.** A public report exposes every deployment before a fix exists.

Use one of these private channels:

- **GitHub private vulnerability reporting** — repository **Security** tab →
  **Report a vulnerability** (preferred).
- **Email** — `<SECURITY-CONTACT-PLACEHOLDER>`
  *(maintainers: replace with the project security address before the first
  public release; remove this note afterwards)*.

A useful report includes:

- the affected version, commit, or image tag;
- the deployment shape (default Compose stack, or a customized one);
- a reproduction — commands, configuration, and the smallest input that triggers
  the issue;
- the impact you believe it has, and any proof-of-concept;
- whether the issue is already public anywhere.

## What to expect

- **Acknowledgement** within 7 days *(target, not a guarantee while the project
  has a single maintainer)*.
- An initial assessment with a severity judgement and a rough fix timeline.
- Credit in the release notes once a fix ships, unless you ask to stay anonymous.

Please give the project a reasonable window to ship a fix before public
disclosure. If a fix is not feasible in a reasonable time, we will say so
explicitly rather than let the report go silent.

## Scope

**In scope** — issues in this repository's own code and default configuration:

- authentication and authorization bypass (API key handling, admin surface,
  role checks);
- injection and input-handling flaws (SQL/DDL parsing paths, query parameters,
  template rendering);
- secret exposure (credentials logged, written to disk, or baked into images);
- insecure defaults in the shipped Compose files and gateway configuration;
- path traversal and file-write issues in admin upload/import paths.

**Out of scope** — please report these upstream instead:

- vulnerabilities in third-party base images, databases, gateways or libraries
  with no project-specific misuse (report to that project; we will still track
  the version bump);
- findings that require a deliberately weakened deployment (for example running
  with authentication disabled, or exposing an internal admin port to the
  internet) and that are documented as unsafe;
- missing hardening headers or best-practice suggestions with no demonstrated
  impact;
- results from automated scanners submitted without a reproduction.

## Handling secrets in a report

Never include live credentials. Replace them with placeholders. If a real secret
was exposed, rotate it first, then report.

## Project boundary

This project is a general-purpose data service platform. It contains no
healthcare-insurance business capability and is not intended for medical use.
Do not treat it as a compliance control for any regulated industry.

## License

This project is licensed under the Apache License, Version 2.0 (see `LICENSE`).
