## What this changes

<!-- And why. If it fixes a bug, what was the wrong behaviour? -->

## Checks

- [ ] `make check` passes locally
- [ ] Tests cover the behaviour that would silently regress, not just the happy path

## Things that need a callout

- [ ] **Metric renamed or removed** — major version bump, and it breaks downstream
      dashboards and rules silently
- [ ] **New B2 API call** — privilege-surface change; update the table in
      [SECURITY.md](../SECURITY.md) in this same PR
- [ ] **Fixture re-captured** — note the date and the API/SDK version it came from
- [ ] None of the above

## Anything you could not verify

<!-- Genuinely useful. `client.py` has no test coverage, so anything touching the
     b2sdk path is unproven until someone runs it against a real bucket -- say so
     rather than implying otherwise. -->
