"""CLI entry point: resolve config, start the refresh loop, serve /metrics.

CONFIG PRECEDENCE IS flag > environment > default, and the environment half is the
point: a Kubernetes ConfigMap becomes environment variables becomes configuration,
with flags still winning for a local run or a one-off debug container.

CREDENTIALS ARE ENVIRONMENT-ONLY, DELIBERATELY. There is no `--application-key` flag
and there should never be one: anything in argv is visible in `ps`, in a container's
`spec.containers[].args`, and in any crash dump that captures the command line. Keys
belong in a Secret, projected as env, and nowhere else.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import REGISTRY, make_wsgi_app

from . import __version__
from .client import B2Client
from .collector import DEFAULT_NAMESPACE, B2Collector, collect_snapshot

log = logging.getLogger("backblaze_b2_exporter")

ENV_PREFIX = "B2_EXPORTER_"
# Credentials use the names the official b2 tooling already uses, so a shell that can
# run `b2` can run this without a second set of variables to keep in sync.
ENV_KEY_ID = "B2_APPLICATION_KEY_ID"
ENV_KEY = "B2_APPLICATION_KEY"

# Not registered in the Prometheus port-allocation list. Chosen to sit clear of the
# common exporters rather than claimed; override it if it clashes with something local.
DEFAULT_PORT = 9944
DEFAULT_REFRESH_SECONDS = 900  # 15 min. See README: collection is timer-driven, never
# scrape-driven, so this -- and only this -- sets the B2 transaction rate.


class ConfigError(ValueError):
    """Configuration is unusable. Raised rather than exiting, so it is testable."""


@dataclass(frozen=True, slots=True)
class Config:
    bucket: str
    key_id: str
    application_key: str
    prefixes: tuple[str, ...] = ()
    metric_prefix: str = DEFAULT_NAMESPACE
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS
    quota_bytes: int | None = None
    listen_address: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    log_level: str = "INFO"
    warnings: tuple[str, ...] = field(default=())


def _parse_prefixes(raw: str | None) -> tuple[str, ...]:
    """Comma-separated, whitespace-tolerant, empties dropped.

    An empty configuration is legal: everything then lands in `other`, and
    `sum(b2_bucket_billed_bytes)` is still the exact bucket total, because the
    configured prefixes plus `other` are disjoint and exhaustive by construction.
    """
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _int_or_error(raw: str | None, name: str) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backblaze-b2-exporter",
        description="Prometheus exporter for Backblaze B2 bucket usage as billed.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    # Every default is None so that "was the flag given?" stays distinguishable from
    # "the flag equals the default" -- without that, a flag could never lose to an
    # environment variable and the precedence rule would be a fiction.
    parser.add_argument("--bucket", default=None, help=f"[{ENV_PREFIX}BUCKET]")
    parser.add_argument(
        "--prefixes",
        default=None,
        help=f"Comma-separated key prefixes to attribute usage to [{ENV_PREFIX}PREFIXES]",
    )
    parser.add_argument(
        "--metric-prefix",
        default=None,
        help=f"Metric namespace, default {DEFAULT_NAMESPACE!r} [{ENV_PREFIX}METRIC_PREFIX]",
    )
    parser.add_argument(
        "--refresh-interval",
        default=None,
        help=f"Seconds between collections, default {DEFAULT_REFRESH_SECONDS} "
        f"[{ENV_PREFIX}REFRESH_INTERVAL]",
    )
    parser.add_argument(
        "--quota-bytes",
        default=None,
        help=f"Capacity ceiling to publish for ratio alerting [{ENV_PREFIX}QUOTA_BYTES]",
    )
    parser.add_argument("--listen-address", default=None, help=f"[{ENV_PREFIX}LISTEN_ADDRESS]")
    parser.add_argument("--port", default=None, help=f"[{ENV_PREFIX}PORT]")
    parser.add_argument("--log-level", default=None, help=f"[{ENV_PREFIX}LOG_LEVEL]")
    return parser


def resolve_config(argv: Sequence[str], environ: Mapping[str, str]) -> Config:
    """Pure: takes argv and an environment mapping, returns Config or raises.

    Deliberately does not read `sys.argv` or `os.environ` itself, so precedence can be
    tested exhaustively without mutating process state.
    """
    args = build_parser().parse_args(list(argv))

    def pick(flag_value: str | None, env_suffix: str) -> str | None:
        if flag_value is not None:
            return flag_value
        return environ.get(ENV_PREFIX + env_suffix)

    bucket = pick(args.bucket, "BUCKET")
    if not bucket:
        raise ConfigError(f"bucket is required: pass --bucket or set {ENV_PREFIX}BUCKET")

    key_id = environ.get(ENV_KEY_ID)
    application_key = environ.get(ENV_KEY)
    if not key_id or not application_key:
        raise ConfigError(
            f"credentials are required and are read from the environment ONLY: "
            f"set {ENV_KEY_ID} and {ENV_KEY}. There is no flag for these on purpose -- "
            f"argv is visible in ps and in container specs."
        )

    prefixes = _parse_prefixes(pick(args.prefixes, "PREFIXES"))
    warnings: list[str] = [
        # Not corrected automatically: silently rewriting someone's configuration is
        # worse than telling them. `etcd` matches `etcdfoo/...` too, which is rarely
        # what anyone means.
        f"prefix {p!r} does not end with '/', so it will also match sibling keys "
        f"that merely start with it"
        for p in prefixes
        if not p.endswith("/")
    ]

    metric_prefix = pick(args.metric_prefix, "METRIC_PREFIX")
    refresh = _int_or_error(pick(args.refresh_interval, "REFRESH_INTERVAL"), "refresh interval")
    if refresh is not None and refresh <= 0:
        raise ConfigError("refresh interval must be positive")
    quota = _int_or_error(pick(args.quota_bytes, "QUOTA_BYTES"), "quota bytes")
    port = _int_or_error(pick(args.port, "PORT"), "port")

    return Config(
        bucket=bucket,
        key_id=key_id,
        application_key=application_key,
        prefixes=prefixes,
        # `is None` rather than `or`: an intentionally empty metric prefix is a valid
        # choice and must not silently fall back to the default.
        metric_prefix=DEFAULT_NAMESPACE if metric_prefix is None else metric_prefix,
        refresh_seconds=refresh or DEFAULT_REFRESH_SECONDS,
        quota_bytes=quota,
        listen_address=pick(args.listen_address, "LISTEN_ADDRESS") or "0.0.0.0",
        port=port or DEFAULT_PORT,
        log_level=(pick(args.log_level, "LOG_LEVEL") or "INFO").upper(),
        warnings=tuple(warnings),
    )


class Refresher(threading.Thread):
    """Timer-driven collection. Scrapes never reach Backblaze; this does.

    Collects once immediately so a fresh pod is useful within seconds rather than after
    a full interval, then waits on an Event rather than sleeping -- so SIGTERM is honoured
    promptly instead of after up to 15 minutes of an unkillable sleep.
    """

    def __init__(
        self,
        client: B2Client,
        collector: B2Collector,
        bucket: str,
        prefixes: Sequence[str],
        interval_seconds: int,
    ) -> None:
        super().__init__(name="b2-refresh", daemon=True)
        self._client = client
        self._collector = collector
        self._bucket = bucket
        self._prefixes = list(prefixes)
        self._interval = interval_seconds
        # NOT `self._stop`. threading.Thread has a PRIVATE `_stop()` method that CPython
        # calls from join() -> _wait_for_tstate_lock(); assigning an Event over it makes
        # join() raise "TypeError: 'Event' object is not callable". The threading
        # internals changed after 3.11, so this passes on 3.13/3.14 and fails only on the
        # floor version -- which is exactly why the CI matrix pins 3.11.
        self._stopping = threading.Event()

    def collect_once(self) -> bool:
        """One collection. Returns success; never raises.

        A transient B2 error must not kill the thread, or the exporter would serve a
        frozen snapshot forever with nothing to say it had stopped trying. On failure the
        previous values are RETAINED and success drops to 0 -- see B2Collector for why
        that differs from the cold-start case.
        """
        try:
            snapshot = collect_snapshot(
                self._bucket,
                self._client.iter_file_versions(self._bucket),
                self._prefixes,
            )
        except Exception:
            log.exception("collection failed for bucket %s", self._bucket)
            self._collector.mark_failure()
            return False
        self._collector.update(snapshot)
        log.info(
            "collected bucket=%s prefixes=%d in %.2fs",
            self._bucket,
            len(snapshot.per_prefix),
            snapshot.duration_seconds,
        )
        return True

    def run(self) -> None:
        while True:
            self.collect_once()
            if self._stopping.wait(self._interval):
                log.info("refresh loop stopping")
                return

    def stop(self) -> None:
        self._stopping.set()


def make_app(registry):
    """WSGI app dispatching /metrics and /health, and 404 for everything else.

    WHY NOT `start_http_server` ALONE. prometheus_client's WSGI app answers EVERY path
    with 200 and the metrics body -- `/health`, `/`, and `/nonsense` alike (verified
    against 0.26.0). A container HEALTHCHECK or a Kubernetes probe pointed at `/health`
    would therefore pass even if the path were a typo, which makes it a "the socket is
    open" check wearing a health check's clothes.

    Dispatching explicitly means a wrong path fails loudly, and `/health` means something
    narrow and honest: the HTTP server is up. It deliberately does NOT report collection
    state -- that is what `b2_collection_success` is for, and gating readiness on a
    successful collection would stop the scrape that carries the bad news.
    """
    metrics_app = make_wsgi_app(registry)

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "/")
        if path == "/metrics":
            return metrics_app(environ, start_response)
        if path == "/health":
            start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
            return [b"ok\n"]
        start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"not found\n"]

    return app


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """A scrape must never queue behind a slow one.

    Defined here from public stdlib pieces rather than importing prometheus_client's
    private `ThreadingWSGIServer`, which would break silently on a library refactor.
    """

    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    """Suppress per-request access logging: a line per scrape is pure noise."""

    def log_message(self, fmt, *args):
        pass


def serve(registry, address: str, port: int) -> WSGIServer:
    server = make_server(address, port, make_app(registry), _ThreadingWSGIServer, _QuietHandler)
    threading.Thread(target=server.serve_forever, name="b2-http", daemon=True).start()
    return server


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = resolve_config(sys.argv[1:] if argv is None else argv, os.environ)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for warning in config.warnings:
        log.warning(warning)

    collector = B2Collector(
        bucket=config.bucket,
        quota_bytes=config.quota_bytes,
        namespace=config.metric_prefix,
    )
    REGISTRY.register(collector)

    client = B2Client(config.key_id, config.application_key)
    refresher = Refresher(client, collector, config.bucket, config.prefixes, config.refresh_seconds)

    server = serve(REGISTRY, config.listen_address, config.port)
    log.info(
        "serving /metrics and /health on %s:%d, refreshing bucket %s every %ds, metric prefix %r",
        config.listen_address,
        config.port,
        config.bucket,
        config.refresh_seconds,
        config.metric_prefix,
    )
    refresher.start()

    stopping = threading.Event()

    def _handle(signum, _frame):
        log.info("received signal %s", signum)
        refresher.stop()
        stopping.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stopping.wait()
    server.shutdown()
    refresher.join(timeout=10)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
