"""Config resolution and the refresh loop.

`resolve_config` takes argv and an environment mapping as arguments rather than reading
`sys.argv` / `os.environ`, so precedence can be tested exhaustively without mutating
process state or leaking a fake credential into the real environment.
"""

from __future__ import annotations

import pytest

from backblaze_b2_exporter.__main__ import (
    DEFAULT_PORT,
    DEFAULT_REFRESH_SECONDS,
    ConfigError,
    Refresher,
    resolve_config,
)
from backblaze_b2_exporter.client import FileVersion
from backblaze_b2_exporter.collector import DEFAULT_NAMESPACE, B2Collector

CREDS = {"B2_APPLICATION_KEY_ID": "keyid", "B2_APPLICATION_KEY": "secret"}


def env(**extra: str) -> dict[str, str]:
    return {**CREDS, **extra}


# --------------------------------------------------------------------- precedence


def test_flag_beats_environment():
    cfg = resolve_config(["--bucket", "from-flag"], env(B2_EXPORTER_BUCKET="from-env"))
    assert cfg.bucket == "from-flag"


def test_environment_beats_default():
    cfg = resolve_config([], env(B2_EXPORTER_BUCKET="b", B2_EXPORTER_METRIC_PREFIX="acme"))
    assert cfg.metric_prefix == "acme"


def test_default_applies_when_neither_is_set():
    cfg = resolve_config([], env(B2_EXPORTER_BUCKET="b"))
    assert cfg.metric_prefix == DEFAULT_NAMESPACE
    assert cfg.refresh_seconds == DEFAULT_REFRESH_SECONDS
    assert cfg.port == DEFAULT_PORT
    assert cfg.quota_bytes is None


def test_metric_prefix_from_configmap_style_env():
    """The ConfigMap case: a key becomes an env var becomes the metric namespace."""
    cfg = resolve_config([], env(B2_EXPORTER_BUCKET="b", B2_EXPORTER_METRIC_PREFIX="b2_archive"))
    assert cfg.metric_prefix == "b2_archive"


def test_empty_metric_prefix_is_honoured_not_replaced_by_the_default():
    """An intentionally empty prefix must survive.

    `pick(...) or DEFAULT` would silently restore `b2` here, which is the classic falsy
    bug: the user asked for no prefix and would get one anyway.
    """
    cfg = resolve_config([], env(B2_EXPORTER_BUCKET="b", B2_EXPORTER_METRIC_PREFIX=""))
    assert cfg.metric_prefix == ""


# ------------------------------------------------------------------- credentials


def test_credentials_are_required():
    with pytest.raises(ConfigError, match="credentials are required"):
        resolve_config(["--bucket", "b"], {})


def test_there_is_no_credential_flag():
    """Credentials must not be passable in argv -- ps and container specs expose it.

    Asserted as a property of the parser rather than a convention someone remembers.
    """
    with pytest.raises(SystemExit):  # argparse rejects the unknown option
        resolve_config(["--bucket", "b", "--application-key", "secret"], env())


def test_bucket_is_required():
    with pytest.raises(ConfigError, match="bucket is required"):
        resolve_config([], env())


# ------------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a/,b/", ("a/", "b/")),
        (" a/ , b/ ", ("a/", "b/")),
        ("a/,,b/", ("a/", "b/")),
        ("", ()),
        ("a/", ("a/",)),
    ],
)
def test_prefix_parsing(raw, expected):
    cfg = resolve_config([], env(B2_EXPORTER_BUCKET="b", B2_EXPORTER_PREFIXES=raw))
    assert cfg.prefixes == expected


def test_prefix_without_trailing_slash_warns_but_is_not_rewritten():
    """`etcd` also matches `etcdfoo/...`. Warn; do not silently edit someone's config."""
    cfg = resolve_config([], env(B2_EXPORTER_BUCKET="b", B2_EXPORTER_PREFIXES="etcd,ok/"))
    assert cfg.prefixes == ("etcd", "ok/")
    assert any("etcd" in w for w in cfg.warnings)
    assert not any("ok/" in w for w in cfg.warnings)


def test_non_integer_refresh_interval_is_rejected():
    with pytest.raises(ConfigError, match="refresh interval must be an integer"):
        resolve_config([], env(B2_EXPORTER_BUCKET="b", B2_EXPORTER_REFRESH_INTERVAL="soon"))


def test_zero_refresh_interval_is_rejected():
    """A zero interval would poll B2 as fast as the loop spins, which is a billing event."""
    with pytest.raises(ConfigError, match="must be positive"):
        resolve_config([], env(B2_EXPORTER_BUCKET="b", B2_EXPORTER_REFRESH_INTERVAL="0"))


# ------------------------------------------------------------------ refresh loop


class FakeClient:
    """Stands in for B2Client. Records calls; can be told to fail."""

    def __init__(self, versions, fail=False):
        self._versions = versions
        self.fail = fail
        self.calls = 0

    def iter_file_versions(self, bucket):
        self.calls += 1
        if self.fail:
            raise RuntimeError("B2 unreachable")
        yield from self._versions


VERSIONS = [FileVersion("etcd/a", 100, "upload", 1000)]


def _names(collector):
    return {f.name for f in collector.collect()}


def test_successful_collection_updates_the_collector():
    collector = B2Collector("bucket")
    refresher = Refresher(FakeClient(VERSIONS), collector, "bucket", ["etcd/"], 60)
    assert refresher.collect_once() is True
    families = {f.name: f for f in collector.collect()}
    assert families["b2_collection_success"].samples[0].value == 1
    assert "b2_bucket_billed_bytes" in families


def test_failure_never_raises_and_never_kills_the_loop():
    """A transient B2 error must not end the thread, or the exporter freezes silently."""
    collector = B2Collector("bucket")
    refresher = Refresher(FakeClient(VERSIONS, fail=True), collector, "bucket", ["etcd/"], 60)
    assert refresher.collect_once() is False  # returned, did not raise


def test_failure_before_any_success_publishes_no_usage_gauges():
    """Cold failure: a fabricated 0 would read as healthy to a threshold alert."""
    collector = B2Collector("bucket")
    Refresher(FakeClient(VERSIONS, fail=True), collector, "bucket", ["etcd/"], 60).collect_once()
    names = _names(collector)
    assert "b2_collection_success" in names
    assert not any("bucket_billed" in n for n in names)


def test_failure_after_success_retains_the_last_known_values():
    """Warm failure differs from cold: those bytes were true recently, and age says when."""
    collector = B2Collector("bucket")
    client = FakeClient(VERSIONS)
    refresher = Refresher(client, collector, "bucket", ["etcd/"], 60)
    refresher.collect_once()
    client.fail = True
    refresher.collect_once()

    families = {f.name: f for f in collector.collect()}
    assert families["b2_collection_success"].samples[0].value == 0
    billed = families["b2_bucket_billed_bytes"].samples
    assert any(s.value == 100 for s in billed), "previous values were dropped, not retained"


def test_stop_is_honoured_promptly_rather_than_after_a_full_interval():
    """Waits on an Event, not time.sleep -- otherwise SIGTERM waits up to 15 minutes.

    A long interval is used deliberately: if the loop slept, this test would hang.
    """
    collector = B2Collector("bucket")
    client = FakeClient(VERSIONS)
    refresher = Refresher(client, collector, "bucket", ["etcd/"], 3600)
    refresher.start()
    refresher.stop()
    refresher.join(timeout=5)
    assert not refresher.is_alive()
    assert client.calls == 1  # collected immediately on start, then exited


def test_exposition_renders_as_valid_prometheus_text():
    """End-to-end through the actual serialiser, not just the collector's objects.

    Catches what object-level assertions cannot: an invalid metric name, a label
    mismatch, or a family that fails to encode -- all of which surface only when
    something scrapes it.
    """
    from prometheus_client import CollectorRegistry, generate_latest

    registry = CollectorRegistry()
    collector = B2Collector("example-backups", quota_bytes=10_737_418_240, namespace="b2")
    registry.register(collector)
    Refresher(FakeClient(VERSIONS), collector, "example-backups", ["etcd/"], 60).collect_once()

    text = generate_latest(registry).decode()
    assert "# TYPE b2_bucket_billed_bytes gauge" in text
    assert 'b2_bucket_billed_bytes{bucket="example-backups",prefix="etcd/"} 100.0' in text
    assert 'b2_bucket_billed_bytes{bucket="example-backups",prefix="other"} 0.0' in text
    assert "b2_bucket_quota_bytes 1.073741824e+10" in text
    assert "b2_collection_success 1.0" in text
    # HELP text is how an operator learns what "billed" means without the README.
    assert "# HELP b2_bucket_billed_bytes" in text
