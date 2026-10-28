# Contributing to OneServe

Thanks for taking the time to contribute. This document covers how to report a
problem, how to propose and submit a change, and what a change has to pass
before it is merged.

By participating you agree to the [Code of Conduct](#code-of-conduct).

- New to the project? Start with the quickstart in `README.md`.
- Found a security issue? **Do not open an issue.** Follow `SECURITY.md`.

---

## 1. Reporting a problem

1. **Search first.** Check open and closed issues for the same symptom.
2. **Open one issue per problem.** A reproducible report with a minimal example
   gets fixed; a report without one usually does not.

A useful report contains:

- **What you ran** — the exact commands, copy-pasted, not paraphrased.
- **What you expected vs. what happened** — including the actual error text.
- **Environment** — OS and CPU architecture (`uname -a`), Docker and Compose
  versions, the commit or release you are on, and whether you used the default
  Compose file or a customized one.
- **Relevant logs** — the service container log tail around the failure.

**Redact before you paste:** API keys, tokens, passwords, phone numbers, real
records, internal hostnames and IP addresses. Replace them with placeholders
such as `<redacted-api-key>`. Reports that leak credentials will be edited or
deleted.

## 2. Proposing a change

- **Small, well-scoped fixes** (typo, doc fix, obvious bug): open a pull request
  directly.
- **Anything that changes behavior, configuration surface, data format, or
  deployment topology**: open an issue first and describe the intended change
  and its alternatives. Large pull requests that arrive without prior discussion
  may be asked to restart from an issue.

One pull request should contain **one logical change**. Unrelated cleanups in
the same diff make review slower and are usually sent back.

## 3. Branch naming

Use a lowercase, hyphenated branch name, prefixed by the change type:

```
feat/<short-description>     # new capability
fix/<short-description>      # bug fix
docs/<short-description>     # documentation only
```

Examples: `feat/datasource-hbase-discovery`, `fix/kv-timeout-not-retried`,
`docs/quickstart-x86`.

Keep the description short (a few words). Do not use personal names, ticket
identifiers from a private tracker, or dates in branch names.

## 4. Commit messages

Follow the Conventional Commits shape:

```
<type>(<scope>): <subject>

<body: why this change, wrapped at 72 columns>

<footer: refs>
```

- **type** — one of `feat`, `fix`, `docs`, `refactor`, `perf`, `test`,
  `build`, `ci`, `chore`.
- **scope** — optional, the area touched: `api`, `gateway`, `kv`, `portal`,
  `audit`, `deploy`, `docs`.
- **subject** — imperative mood, no trailing period, about 72 characters or less.
  Write `fix kv retry loop`, not `fixed kv retry loop`.
- **body** — explain *why*, not *what*; the diff already says what.
- **footer** — reference issues with `Refs #123` / `Closes #123`.

Keep each commit buildable. If a reviewer cannot understand a commit in
isolation, it is probably two commits.

## 5. Before you open a pull request

Run the project gates locally and make sure they are **green**:

```bash
# Full gate: repository assertions plus the test suite.
bash scripts/run_gates.sh

# Test suite alone, if you want a faster loop while iterating.
python -m pytest -q
```

A pull request is not ready for review until:

- [ ] `bash scripts/run_gates.sh` passes with no failures.
- [ ] New behavior has a test; bug fixes have a test that fails before the fix.
- [ ] Skipped tests are not used to hide a failure. A skip must be justified in
      the pull request description.
- [ ] No secrets, keys, tokens, real phone numbers, real records, or internal
      hostnames/IPs are added anywhere, including fixtures, logs and docs.
- [ ] Documentation is updated if the change is user-visible
      (README, `NOTICE` for new third-party dependencies or images).
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]`.
- [ ] The diff contains nothing unrelated to the stated change.

## 6. Review and merge

- At least one maintainer approval is required.
- Maintainers may push small fixups to your branch or ask for a follow-up commit;
  either is normal.
- Merges are squashed, so the pull request title becomes the commit subject —
  write it as a valid Conventional Commit.
- If a review sits idle for a week, a polite ping in the thread is fine.

## 7. Adding a dependency or an image

New runtime dependencies and container images are a licensing decision, not only
a technical one:

- Prefer OSI-approved licenses. A dependency under a non-OSI license
  (for example source-available licenses such as RSALv2 or SSPL) blocks the
  change unless the project explicitly decides otherwise in an ADR.
- Add the new package or image to `NOTICE` in the same pull request, with its
  license.
- Keep the lock file in sync with the declared requirements; the lock file is the
  authority for what is actually distributed.

## 8. Code of Conduct

Be respectful and assume good faith. Harassment, personal attacks, and
discriminatory language are not acceptable in issues, pull requests, or any
project space. Maintainers may edit, lock, or remove contributions that violate
this, and may block repeat offenders. Report conduct problems privately to the
maintainers through the contact listed in `SECURITY.md`.

## 9. License of contributions

This project is licensed under the Apache License, Version 2.0 (see `LICENSE`).
Unless you explicitly state otherwise, any contribution you intentionally submit
for inclusion is licensed under the same terms, without additional conditions.
