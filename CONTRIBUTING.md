# Contributing

Contributions are welcome, particularly from people running bucket shapes this has
not seen: heavy version churn, unfinished large files, hundreds of prefixes, or
lifecycle rules configured differently. See the "what has not been exercised"
section of the README — that list is where the real gaps are.

## The workflow

`main` is protected. All changes land through a pull request.

1. Branch from `main`.
2. Make the change.
3. **Run `make check` locally.** This is the same gate the PR faces.
4. Open a PR.

## Run the checks before you push

Every check that runs in CI is defined in the `Makefile` and nowhere else — the
workflow calls the same targets. So this is not an approximation of CI, it is CI:

```bash
make setup    # one-off: create .venv and install
make check    # lint, tests, workflows, helm, docker
```

Individual gates, when you want a faster loop:

```bash
make lint            # ruff check, ruff format --check
make fmt             # apply formatting and autofixes
make test            # pytest
make actionlint      # workflow syntax, expressions, shellcheck on run: blocks
make actions-pinned  # every uses: is SHA-pinned with a version comment
make helm            # helm lint, template permutations, required values, kubeconform
make docker          # hadolint, image build, smoke test
make scan            # grype CVE scan (run make docker first)
```

Tools you do not have installed are reported as `SKIP` rather than failing, so
`make check` is useful on a laptop without Docker. CI sets `REQUIRE_ALL=1`, which
turns every skip into a failure — so a check that skipped locally will still run
against your PR.

Optional extras, if you want the full local gate:

```bash
brew install helm kubeconform hadolint grype actionlint
```

## What a good change looks like

- **Comments explain why, not what.** The existing code is commented at the points
  where a reader would otherwise wonder why something is the way it is. Match that,
  and skip narrating what the line plainly does.
- **Tests assert behaviour that would silently regress.** The valuable ones here are
  not the happy path — they are the memory-boundedness test, the ordering guard, and
  the cold-vs-stale distinction. Each exists because getting it wrong produces a
  plausible number rather than an error.
- **Fixtures come from real API output**, with record order preserved, and say so in
  the test module docstring. A fixture pins the format it was captured from and does
  not prove the live format still matches.

## Things to be careful with

**Never materialise the listing.** Memory is O(prefixes), not O(objects), and a test
asserts it. Storing the file list "just for debugging" is how a cache becomes a
memory leak, and it will pass every other test.

**Prefixes are configured, never discovered.** Deriving label values from object keys
makes cardinality a function of what someone uploads, which no pod restart undoes.

**Renaming or removing a metric is a major version bump.** It silently breaks
dashboards and alerting rules downstream.

**Adding a B2 API call changes the privilege surface.** See [SECURITY.md](SECURITY.md)
— a new read is a minor bump announced in the changelog; anything that writes is major
and is not a direction this project intends to take.
