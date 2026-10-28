## Summary

What this change does, in one or two sentences. One logical change per pull request.

## Motivation and related issue

Why it is needed, and what it links to: `Closes #123` / `Refs #123`.
Behavior, configuration, data-format, or topology changes should point at the issue
where they were discussed.

## Verification

Paste the commands you ran and their output. Gates must be green before review:

```bash
bash scripts/run_gates.sh          # repository gates plus the test suite
python3 -m pytest tests/ -q -rf    # faster loop while iterating
```

- [ ] `bash scripts/run_gates.sh` passes with no failures
- [ ] Any skip in the run is justified below, not used to hide a failure

Notes and output:

## Checklist

- [ ] The diff contains only what the stated change needs; no drive-by cleanups
- [ ] No secrets, keys, tokens, real records, or internal hostnames/IPs, in code, fixtures, logs, or docs
- [ ] Any new dependency or container image is OSI-approved where possible and added to `NOTICE` with its license; the lock file is in sync
- [ ] New behavior has a test; a bug fix has a test that fails before the fix
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]` if the change is user-visible
- [ ] Documentation is updated if the change is user-visible

See docs/opensource/CONTRIBUTING.md for the full list.
