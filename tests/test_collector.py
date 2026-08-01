"""Aggregation tests, replayed offline against a captured listing.

FIXTURE PROVENANCE. `fixtures/list_file_versions.json` is a trimmed capture of real
`b2 ls --recursive --json --versions b2://homelab-kukui/` output taken 2026-08-01,
with field names and record ORDER preserved exactly. It pins the format as it was on
that date; passing tests do NOT prove the live API still returns that shape. Re-capture
after any b2-sdk-python major bump or a B2 API revision.

Retained deliberately: `probe.txt`, whose latest version is a hide marker over two
superseded uploads. That single file is the whole reason this exporter exists -- it
contributes 38 BILLED bytes and 0 CURRENT bytes, which is exactly the quantity a
ListObjectsV2-based exporter cannot see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backblaze_b2_exporter.client import FileVersion
from backblaze_b2_exporter.collector import (
    OTHER_PREFIX,
    ListingOrderError,
    aggregate,
)

FIXTURE = Path(__file__).parent / "fixtures" / "list_file_versions.json"
TALOS = "talos-cluster-state/"

# Sum of the ten files in the 2026-07-31_231658 round, plus the stray p2.txt.
ROUND_BYTES = 8_052_245
P2_BYTES = 13


def load_fixture() -> list[FileVersion]:
    raw = json.loads(FIXTURE.read_text())
    return [FileVersion.from_api(r) for r in raw]


@pytest.fixture
def totals():
    return aggregate(load_fixture(), [TALOS])


def test_billed_bytes_include_superseded_and_hidden(totals):
    """The headline claim: billed > current, and the delta is the invisible bytes."""
    other = totals[OTHER_PREFIX]
    # probe.txt: two superseded uploads of 32 and 6 bytes, latest version is a hide
    # marker, so nothing is current.
    assert other.billed_bytes == 38
    assert other.current_bytes == 0
    assert other.billed_bytes - other.current_bytes == 38


def test_hide_markers_counted_but_weightless(totals):
    """Hide markers inflate object counts and must never inflate storage cost."""
    other = totals[OTHER_PREFIX]
    assert other.hide_markers == 2
    assert other.billed_objects == 2  # the two uploads; hides are not uploads
    # 38 bytes is the two uploads alone -- the two hide markers added nothing.
    assert other.billed_bytes == 32 + 6


def test_hidden_file_has_no_current_version(totals):
    """A file whose newest version is a hide marker is not current, at any size."""
    assert totals[OTHER_PREFIX].current_objects == 0


def test_prefix_attribution(totals):
    talos = totals[TALOS]
    assert talos.billed_objects == 11  # ten in the round, plus p2.txt
    assert talos.billed_bytes == ROUND_BYTES + P2_BYTES
    # Every name appears once, so all are current.
    assert talos.current_bytes == talos.billed_bytes
    assert talos.current_objects == talos.billed_objects
    assert talos.hide_markers == 0


def test_stray_object_lands_in_its_prefix_not_other(totals):
    """`p2.txt` sits loose inside a workload prefix -- it must be attributed there.

    This is the leftover that motivated per-prefix accounting: it pollutes a workload's
    numbers, and lumping it into `other` would hide that rather than surface it.
    """
    assert totals[TALOS].billed_bytes == ROUND_BYTES + P2_BYTES
    assert totals[OTHER_PREFIX].billed_objects == 2  # probe.txt only


def test_unconfigured_keys_go_to_other_not_a_new_series(totals):
    """Cardinality is capped by configuration, never grown by the data."""
    assert set(totals) == {TALOS, OTHER_PREFIX}


def test_freshness_is_newest_upload_ignoring_hide_markers(totals):
    assert totals[TALOS].last_upload_timestamp_ms == 1785541263711
    # probe.txt's newest *upload* is 1785533940010; its newer hide marker must not win.
    assert totals[OTHER_PREFIX].last_upload_timestamp_ms == 1785533940010


def test_out_of_order_listing_fails_loudly():
    """Ordering is load-bearing, so a violation must break the collection, not skew it.

    Current-vs-superseded is decided purely by file-name transitions in one pass. If B2
    ever stopped returning versions grouped and ordered, that logic would keep producing
    confident, wrong numbers -- the failure that looks like success.
    """
    versions = [
        FileVersion("b/two", 10, "upload", 2),
        FileVersion("a/one", 10, "upload", 1),
    ]
    with pytest.raises(ListingOrderError):
        aggregate(versions, ["a/", "b/"])


def test_longest_prefix_wins():
    versions = [FileVersion("a/b/thing", 5, "upload", 1)]
    totals = aggregate(versions, ["a/", "a/b/"])
    assert totals["a/b/"].billed_bytes == 5
    assert totals["a/"].billed_bytes == 0


def test_empty_listing_yields_zeroed_prefixes_not_missing_ones():
    """Configured prefixes always appear. A prefix with no data is a real, useful zero."""
    totals = aggregate([], [TALOS])
    assert totals[TALOS].billed_bytes == 0
    assert set(totals) == {TALOS, OTHER_PREFIX}


def test_memory_is_bounded_by_prefix_count_not_object_count():
    """The property that silently regresses the moment someone stores the file list.

    Feeds a large generator and asserts the accumulator stays proportional to configured
    prefixes. Also asserts the input was consumed lazily -- materialising the listing is
    how a cache becomes a memory leak.
    """
    consumed = 0

    def many():
        nonlocal consumed
        for i in range(50_000):
            consumed += 1
            yield FileVersion(f"{TALOS}{i:06d}", 1, "upload", i)

    totals = aggregate(many(), [TALOS])
    assert consumed == 50_000
    assert len(totals) == 2  # the configured prefix plus `other`
    assert totals[TALOS].billed_objects == 50_000


def test_unfinished_large_files_counted_separately():
    """Their parts are billed and nothing cancels them by default -- but the listing
    does not expose part sizes, so they are counted and never guessed at."""
    versions = [FileVersion(f"{TALOS}big", 0, "start", 1)]
    totals = aggregate(versions, [TALOS])
    assert totals[TALOS].unfinished_large_files == 1
    assert totals[TALOS].billed_bytes == 0


def test_unknown_action_is_surfaced_not_swallowed():
    versions = [FileVersion(f"{TALOS}x", 5, "someNewAction", 1)]
    totals = aggregate(versions, [TALOS])
    assert totals[TALOS].unknown_actions == 1
    assert totals[TALOS].billed_bytes == 0


def _names(collector) -> set[str]:
    return {family.name for family in collector.collect()}


def test_default_namespace_is_b2():
    from backblaze_b2_exporter.collector import B2Collector, collect_snapshot

    collector = B2Collector("example-backups", quota_bytes=10_737_418_240)
    collector.update(collect_snapshot("example-backups", load_fixture(), [TALOS]))
    names = _names(collector)
    assert "b2_bucket_billed_bytes" in names
    assert "b2_collection_success" in names


def test_namespace_is_configurable_for_collision_avoidance():
    """Two instances -- separate accounts, or this beside a fork -- in one TSDB."""
    from backblaze_b2_exporter.collector import B2Collector, collect_snapshot

    collector = B2Collector("example-backups", namespace="b2_archive")
    collector.update(collect_snapshot("example-backups", load_fixture(), [TALOS]))
    names = _names(collector)
    assert "b2_archive_bucket_billed_bytes" in names
    assert not any(n.startswith("b2_bucket_") for n in names)


def test_namespace_never_impersonates_s3_exporter_semantics():
    """Renaming does not change meaning. Even under an `s3` namespace the headline
    metric is billed bytes, which is NOT what `s3_objects_size_sum_bytes` means --
    so the stem stays distinct rather than colliding with someone else's contract."""
    from backblaze_b2_exporter.collector import B2Collector, collect_snapshot

    collector = B2Collector("example-backups", namespace="s3")
    collector.update(collect_snapshot("example-backups", load_fixture(), [TALOS]))
    names = _names(collector)
    assert "s3_bucket_billed_bytes" in names
    assert "s3_objects_size_sum_bytes" not in names
    assert "s3_objects" not in names


def test_cold_start_publishes_no_usage_gauges():
    """Never collected: meta metrics only. A fabricated 0 would read as healthy."""
    from backblaze_b2_exporter.collector import B2Collector

    names = _names(B2Collector("example-backups"))
    assert "b2_collection_success" in names
    assert not any("bucket_billed" in n for n in names)


def test_failure_after_success_retains_last_known_values():
    """Warm failure keeps the numbers and drops success -- age carries the staleness."""
    from backblaze_b2_exporter.collector import B2Collector, collect_snapshot

    collector = B2Collector("example-backups")
    collector.update(collect_snapshot("example-backups", load_fixture(), [TALOS]))
    collector.mark_failure()
    families = {f.name: f for f in collector.collect()}
    assert families["b2_collection_success"].samples[0].value == 0
    assert "b2_bucket_billed_bytes" in families  # values retained, not dropped


def test_every_metric_family_respects_the_prefix():
    """Exhaustive, not a spot check -- a half-applied prefix is worse than none.

    The metrics someone forgets to route through the namespace are precisely the ones
    that then collide, and a spot check on two names would not catch it.
    """
    from backblaze_b2_exporter.collector import B2Collector, collect_snapshot

    collector = B2Collector("example-backups", quota_bytes=10_737_418_240, namespace="acme")
    collector.update(collect_snapshot("example-backups", load_fixture(), [TALOS]))
    names = _names(collector)
    assert names, "no metrics emitted"
    unprefixed = {n for n in names if not n.startswith("acme_")}
    assert unprefixed == set(), f"metric families bypassing the namespace: {unprefixed}"


def test_empty_prefix_degrades_to_unprefixed_not_leading_underscore():
    """A ConfigMap key set to "" should give legible names, not `_bucket_billed_bytes`."""
    from backblaze_b2_exporter.collector import B2Collector, collect_snapshot

    collector = B2Collector("example-backups", namespace="")
    collector.update(collect_snapshot("example-backups", load_fixture(), [TALOS]))
    names = _names(collector)
    assert "bucket_billed_bytes" in names
    assert not any(n.startswith("_") for n in names)


def test_trailing_underscore_in_prefix_is_tolerated():
    """`B2_METRIC_PREFIX=acme_` from a ConfigMap must not yield `acme__bucket_...`."""
    from backblaze_b2_exporter.collector import B2Collector, collect_snapshot

    collector = B2Collector("example-backups", namespace="acme_")
    collector.update(collect_snapshot("example-backups", load_fixture(), [TALOS]))
    assert "acme_bucket_billed_bytes" in _names(collector)
