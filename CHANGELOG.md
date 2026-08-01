# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Renaming or removing a metric is a major version bump** — it silently breaks
dashboards and alerting rules downstream.

**Adding a B2 API call is a privilege-surface change.** A new *read* is a minor bump,
called out under its own heading with [SECURITY.md](SECURITY.md) updated in the same
change. Anything that *writes*, deletes or hides would be major, and is not a direction
this project intends to take.

## [Unreleased]

## [0.1.0] - 2026-08-01

### Added

- Initial implementation. Reports Backblaze B2 bucket usage **as billed** — counting
  non-current versions and hidden files that `ListObjectsV2` cannot see — with
  per-prefix attribution and upload freshness.
- `b2_bucket_billed_bytes` alongside `b2_bucket_current_bytes`, so the shortfall a
  current-objects exporter would report is visible as a number rather than described in
  prose.
- Configurable metric prefix, applied to **every** metric family, for collisions between
  instances. Readable from a Kubernetes ConfigMap via the environment.
- Helm chart with ServiceMonitor and VMServiceScrape, `enableServiceLinks: false`, and a
  `checksum/config` annotation so a ConfigMap edit actually restarts the pod.
- Chainguard-based image, non-root, with a real dispatched `/health` route.

### Known limitations at first release

Stated here rather than discovered later — the same list appears in the README:

- **`client.py` has no test coverage and has never made a real B2 API call.** Every
  other claim is proven against a captured fixture.
- **Version ordering across pagination boundaries is unverified.** Current-vs-superseded
  is decided by file-name transitions in one pass. `ListingOrderError` should fire rather
  than produce wrong numbers if the guarantee does not hold, but that guard is itself
  untested against real pagination.
- Unfinished large files are counted, never sized — the listing does not expose part
  sizes, and a guessed number would be worse than a count.
- Tested against one bucket shape, where objects have unique names and the dominant
  effect is lifecycle-hidden files.
