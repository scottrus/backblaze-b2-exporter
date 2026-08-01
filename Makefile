# Every check that runs in CI is defined here and nowhere else.
#
# The CI workflow invokes these same targets, so `make check` locally is the same
# gate a pull request faces -- there is no second copy of the commands to drift.
#
# Tools that are not installed are skipped with a warning, so this is useful on a
# laptop without Docker. CI sets REQUIRE_ALL=1, which turns every skip into a
# failure, so a missing tool can never quietly pass in CI.

SHELL := /bin/sh

VENV    ?= .venv
PY      ?= $(VENV)/bin/python
PIP     ?= $(VENV)/bin/pip
CHART   ?= charts/backblaze-b2-exporter
IMAGE   ?= backblaze-b2-exporter:dev

# Read from the package rather than duplicated here, so the smoke test asserts the
# image really carries the version this working tree claims.
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/backblaze_b2_exporter/__init__.py)

# Values every `helm template` invocation needs to satisfy the chart's own guards.
HELM_MIN := --set bucket=example-backups --set b2.applicationKeyId=kid --set b2.applicationKey=secret

.DEFAULT_GOAL := help

define missing
if [ -n "$(REQUIRE_ALL)" ]; then \
  echo "ERROR: $(1) is required but not installed" >&2; exit 1; \
else echo "SKIP: $(2) ($(1) not installed)"; fi
endef

.PHONY: help
help:
	@echo "Local validation -- mirrors the PR checks exactly."
	@echo
	@echo "  make setup           create $(VENV) and install the package + dev deps"
	@echo "  make check           run everything below; the full PR gate"
	@echo
	@echo "  make lint            ruff check, ruff format --check"
	@echo "  make fmt             apply ruff formatting and autofixes"
	@echo "  make test            pytest"
	@echo "  make actionlint      validate workflow syntax, expressions, run: blocks"
	@echo "  make actions-pinned  every uses: is SHA-pinned with a version comment"
	@echo "  make helm            helm lint, template permutations, kubeconform"
	@echo "  make docker          hadolint, image build, smoke test"
	@echo "  make scan            grype CVE scan (run 'make docker' first)"
	@echo
	@echo "  REQUIRE_ALL=1        turn 'tool not installed' skips into failures"

$(VENV):
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

.PHONY: setup
setup: $(VENV)
	@$(PIP) install --quiet -e ".[dev]"

# ---------------------------------------------------------------- python ----

.PHONY: lint
lint: setup
	@echo "==> ruff check"
	@$(PY) -m ruff check --output-format=concise .
	@echo "==> ruff format --check"
	@$(PY) -m ruff format --check .

.PHONY: fmt
fmt: setup
	@$(PY) -m ruff format .
	@$(PY) -m ruff check --fix .

.PHONY: test
test: setup
	@echo "==> pytest"
	@$(PY) -m pytest -q

# --------------------------------------------------------------- workflows ----

.PHONY: actionlint
actionlint:
	@if ! command -v actionlint >/dev/null 2>&1; then $(call missing,actionlint,actionlint); \
		echo "     install with: brew install actionlint"; \
	else \
		echo "==> actionlint"; actionlint -color; echo "    workflows valid"; \
	fi

# Every `uses:` must be pinned to a 40-character commit SHA. A floating tag is
# mutable: the same workflow can run different code tomorrow.
.PHONY: actions-pinned
actions-pinned:
	@echo "==> action pinning"
	@unpinned=$$(grep -hoE "uses: +[^ ]+" .github/workflows/*.yml \
		| awk '{print $$2}' | grep -vE "@[0-9a-f]{40}$$" || true); \
	if [ -n "$$unpinned" ]; then \
		echo "FAIL: not pinned to a commit SHA:"; echo "$$unpinned" | sed 's/^/      /'; exit 1; \
	fi; \
	missing_comment=$$(grep -hE "uses: +[^ ]+@[0-9a-f]{40}" .github/workflows/*.yml \
		| grep -vE "# *v[0-9]" || true); \
	if [ -n "$$missing_comment" ]; then \
		echo "FAIL: pinned but missing a '# vX.Y.Z' comment:"; \
		echo "$$missing_comment" | sed 's/^ */      /'; exit 1; \
	fi; \
	echo "    all uses: are SHA-pinned with a version comment"

# ------------------------------------------------------------------ helm ----

.PHONY: helm
helm: helm-lint helm-template helm-required helm-schema

.PHONY: helm-lint
helm-lint:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,helm lint); else \
		echo "==> helm lint"; helm lint $(CHART) $(HELM_MIN); \
	fi

.PHONY: helm-template
helm-template:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,helm template); else \
		set -e; echo "==> helm template permutations"; \
		helm template t $(CHART) $(HELM_MIN) > /tmp/b2e-inline.yaml; \
		grep -q 'kind: Secret' /tmp/b2e-inline.yaml \
			|| { echo "FAIL: inline key did not render a Secret"; exit 1; }; \
		helm template t $(CHART) --set bucket=b --set b2.existingSecret=ext > /tmp/b2e-ext.yaml; \
		if grep -q 'kind: Secret' /tmp/b2e-ext.yaml; then \
			echo "FAIL: existingSecret must not render a Secret"; exit 1; fi; \
		helm template t $(CHART) $(HELM_MIN) --set serviceMonitor.enabled=true \
			--set vmServiceScrape.enabled=true > /tmp/b2e-scrapes.yaml; \
		grep -q 'kind: ServiceMonitor' /tmp/b2e-scrapes.yaml \
			|| { echo "FAIL: ServiceMonitor not rendered"; exit 1; }; \
		grep -q 'kind: VMServiceScrape' /tmp/b2e-scrapes.yaml \
			|| { echo "FAIL: VMServiceScrape not rendered"; exit 1; }; \
		grep -q 'enableServiceLinks: false' /tmp/b2e-inline.yaml \
			|| { echo "FAIL: enableServiceLinks must default to false -- a Service named b2-exporter would otherwise inject B2_EXPORTER_PORT and collide with the exporter's own port setting"; exit 1; }; \
		grep -q 'checksum/config' /tmp/b2e-inline.yaml \
			|| { echo "FAIL: no config checksum -- a ConfigMap edit would not restart the pod"; exit 1; }; \
		echo "==> values-FILE permutation (the path real deployments take)"; \
		helm template t $(CHART) -f tests/helm/values-file.yaml > /tmp/b2e-from-file.yaml; \
		grep -q 'B2_EXPORTER_QUOTA_BYTES: "10000000000"' /tmp/b2e-from-file.yaml \
			|| { echo "FAIL: a large integer from a VALUES FILE rendered wrong. Helm parses file"; \
			     echo "      values as float64 and --set as int64, so --set cannot catch this."; \
			     grep QUOTA_BYTES /tmp/b2e-from-file.yaml; exit 1; }; \
		grep -q 'B2_EXPORTER_REFRESH_INTERVAL: "900"' /tmp/b2e-from-file.yaml \
			|| { echo "FAIL: refreshInterval wrong from a values file"; exit 1; }; \
		grep -q 'B2_EXPORTER_PORT: "9944"' /tmp/b2e-from-file.yaml \
			|| { echo "FAIL: service.port wrong from a values file"; exit 1; }; \
		grep -q 'enableServiceLinks: false' /tmp/b2e-from-file.yaml \
			|| { echo "FAIL: enableServiceLinks missing on the values-file path"; exit 1; }; \
		grep -q 'checksum/config' /tmp/b2e-from-file.yaml \
			|| { echo "FAIL: config checksum missing on the values-file path"; exit 1; }; \
		grep -q 'automountServiceAccountToken: false' /tmp/b2e-from-file.yaml \
			|| { echo "FAIL: automountServiceAccountToken missing on the values-file path"; exit 1; }; \
		grep -q 'B2_EXPORTER_PREFIXES: "etcd/,postgres/"' /tmp/b2e-from-file.yaml \
			|| { echo "FAIL: list-valued prefixes did not join correctly from a values file"; \
			     grep PREFIXES /tmp/b2e-from-file.yaml; exit 1; }; \
		echo "    all permutations rendered as expected"; \
	fi

.PHONY: helm-required
helm-required:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,required-value guards); else \
		echo "==> required values are enforced"; \
		if helm template t $(CHART) --set b2.applicationKeyId=k --set b2.applicationKey=s >/dev/null 2>&1; then \
			echo "FAIL: chart rendered without bucket"; exit 1; fi; \
		if helm template t $(CHART) --set bucket=b >/dev/null 2>&1; then \
			echo "FAIL: chart rendered without a key"; exit 1; fi; \
		echo "    both guards fired"; \
	fi

.PHONY: helm-schema
helm-schema:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,kubeconform); \
	elif ! command -v kubeconform >/dev/null 2>&1; then \
		$(call missing,kubeconform,kubeconform); \
		echo "     install with: brew install kubeconform"; \
	else \
		echo "==> kubeconform"; \
		helm template t $(CHART) $(HELM_MIN) \
			| kubeconform -strict -summary -schema-location default \
				-skip ServiceMonitor,VMServiceScrape; \
	fi

# ---------------------------------------------------------------- docker ----

.PHONY: docker
docker: docker-lint docker-build docker-smoke

.PHONY: docker-lint
docker-lint:
	@if ! command -v hadolint >/dev/null 2>&1; then $(call missing,hadolint,hadolint); else \
		echo "==> hadolint"; hadolint --failure-threshold warning Dockerfile; \
	fi

.PHONY: docker-build
docker-build:
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,docker build); else \
		set -e; echo "==> docker build"; docker build $(DOCKER_BUILD_ARGS) -t $(IMAGE) .; \
	fi

.PHONY: docker-smoke
docker-smoke:
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,image smoke test); else \
		set -e; echo "==> image smoke test"; \
		docker image inspect $(IMAGE) >/dev/null \
			|| { echo "FAIL: $(IMAGE) not built -- run 'make docker-build' first"; exit 1; }; \
		docker run --rm $(IMAGE) --version | grep -q "$(VERSION)"; \
		echo "    reports version $(VERSION)"; \
		docker run --rm --entrypoint python $(IMAGE) -c \
			'import backblaze_b2_exporter as m; print(m.__version__)' >/dev/null; \
		echo "    package imports inside the image"; \
		docker run --rm --read-only --tmpfs /tmp $(IMAGE) --version >/dev/null; \
		echo "    starts with a READ-ONLY rootfs (+ tmpfs /tmp), as the chart deploys it"; \
		if docker run --rm --read-only $(IMAGE) --version >/dev/null 2>&1; then \
			echo "    read-only WITHOUT /tmp also works -- nothing needs scratch today"; \
		else \
			echo "    NOTE: read-only without /tmp fails, so the chart's tmpfs is load-bearing"; \
		fi; \
		out="$$(docker run --rm $(IMAGE) 2>&1 || true)"; \
		case "$$out" in \
			*"bucket is required"*) echo "    refuses to start unconfigured, with the expected message";; \
			*) echo "FAIL: unconfigured run said: $$out"; exit 1;; \
		esac; \
		if docker run --rm $(IMAGE) >/dev/null 2>&1; then \
			echo "FAIL: expected a non-zero exit with no configuration"; exit 1; fi; \
		echo "    runs as uid $$(docker run --rm --entrypoint python $(IMAGE) -c 'import os;print(os.getuid())')"; \
	fi

.PHONY: scan
scan:
	@if ! command -v grype >/dev/null 2>&1; then $(call missing,grype,grype); else \
		set -e; echo "==> grype"; \
		docker image inspect $(IMAGE) >/dev/null 2>&1 \
			|| { echo "FAIL: $(IMAGE) not built -- run 'make docker' first"; exit 1; }; \
		grype $(IMAGE) --only-fixed --fail-on high; \
	fi

# ----------------------------------------------------------------- gates ----

.PHONY: check
check: lint actionlint actions-pinned test helm docker
	@echo
	@echo "All available checks passed."
	@if [ -z "$(REQUIRE_ALL)" ]; then \
		echo "Note: anything reported as SKIP above was not run."; fi

.PHONY: clean
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
