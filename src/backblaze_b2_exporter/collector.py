"""Fold a stream of B2 file versions into per-prefix usage, and expose it.

WHY THIS EXPORTER EXISTS. Every generic S3 exporter builds on `ListObjectsV2`, which
returns *current* objects only -- no non-current versions, no hidden files. B2 bills
for all of them. On a bucket with a lifecycle rule the gap is structural and permanent:

    daysFromUploadingToHiding: 21     object stops being visible to ListObjectsV2
    daysFromHidingToDeleting:  1      object is still BILLED for another full day

so a current-objects collector reads low by a fixed margin, forever, while reporting
healthy. This module therefore reports both numbers and lets the gap be seen:

    b2_bucket_billed_bytes      what Backblaze charges for
    b2_bucket_current_bytes     what a ListObjectsV2-based exporter would have seen

Their difference is the blind spot, quantified continuously rather than asserted in a
README.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from .client import ACTION_HIDE, ACTION_START, ACTION_UPLOAD, FileVersion

log = logging.getLogger(__name__)

OTHER_PREFIX = "other"

# Metric name prefix. Configurable only to avoid collisions between instances --
# never to impersonate another exporter's metric names.
DEFAULT_NAMESPACE = "b2"


class ListingOrderError(RuntimeError):
    """B2 returned versions out of file-name order.

    Raised loudly rather than tolerated. `aggregate()` distinguishes a current object
    from a superseded one purely by detecting file-name transitions in a single pass;
    if the ordering guarantee ever changes upstream, that distinction silently inverts
    and every current/billed number becomes wrong in a plausible-looking way. Failing
    the collection is strictly better than publishing that.
    """


@dataclass(slots=True)
class PrefixUsage:
    """Accumulator for one prefix. Fixed size -- never grows with object count."""

    billed_bytes: int = 0
    billed_objects: int = 0
    current_bytes: int = 0
    current_objects: int = 0
    hide_markers: int = 0
    unfinished_large_files: int = 0
    unknown_actions: int = 0
    last_upload_timestamp_ms: int = 0


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """One complete collection. Replaces its predecessor wholesale; never appended to.

    The exporter holds exactly one of these. Time-series history is VictoriaMetrics'
    job, and keeping a second copy of it in process memory is how a cache becomes a
    leak.
    """

    bucket: str
    per_prefix: dict[str, PrefixUsage] = field(default_factory=dict)
    collected_at: float = 0.0
    duration_seconds: float = 0.0
    success: bool = False


def _match_prefix(file_name: str, ordered_prefixes: Sequence[str]) -> str:
    """Longest configured prefix that `file_name` starts with, else OTHER_PREFIX.

    Prefixes are CONFIGURED, never discovered from the data. Discovery would make label
    cardinality a function of what someone happens to upload -- one writer using
    per-date or per-UUID keys would permanently inflate series count in the TSDB, which
    no pod restart undoes.
    """
    for prefix in ordered_prefixes:
        if file_name.startswith(prefix):
            return prefix
    return OTHER_PREFIX


def aggregate(
    versions: Iterable[FileVersion],
    prefixes: Sequence[str],
) -> dict[str, PrefixUsage]:
    """Stream versions into per-prefix totals in a single pass.

    Memory is O(len(prefixes)), not O(objects): each record is folded and dropped. The
    input is consumed as an iterator and is never listed.

    CURRENT-OBJECT DETECTION. B2 groups versions by file name in lexicographic order,
    newest first within a name. So the first record seen for a given name is that file's
    latest version: if it is an upload the file is current, and if it is a hide marker
    the file has no current version at all. Every later record for the same name is
    superseded -- still billed, not current.
    """
    # Longest-first so that a nested prefix wins over its parent.
    ordered = sorted(prefixes, key=len, reverse=True)
    totals: dict[str, PrefixUsage] = {p: PrefixUsage() for p in prefixes}
    totals[OTHER_PREFIX] = PrefixUsage()

    previous_name: str | None = None
    for version in versions:
        if previous_name is not None and version.file_name < previous_name:
            raise ListingOrderError(
                f"file names went backwards: {version.file_name!r} after {previous_name!r}"
            )
        is_latest_version = version.file_name != previous_name
        previous_name = version.file_name

        usage = totals[_match_prefix(version.file_name, ordered)]

        if version.action == ACTION_UPLOAD:
            usage.billed_bytes += version.size
            usage.billed_objects += 1
            usage.last_upload_timestamp_ms = max(
                usage.last_upload_timestamp_ms, version.upload_timestamp_ms
            )
            if is_latest_version:
                usage.current_bytes += version.size
                usage.current_objects += 1
        elif version.action == ACTION_HIDE:
            # Hide markers are 0 bytes: they inflate object count, never storage cost.
            # A hide marker as the latest version means the file is not current.
            usage.hide_markers += 1
        elif version.action == ACTION_START:
            # Unfinished large files. Their uploaded parts ARE billed, and this bucket's
            # lifecycle has daysFromStartingToCancelingUnfinishedLargeFiles unset, so
            # nothing ever cancels them -- a failed multipart upload leaks storage
            # permanently. Counted, but NOT added to billed_bytes: the listing does not
            # expose part sizes, and inventing a number would be worse than reporting
            # the count and letting the operator go look.
            usage.unfinished_large_files += 1
        else:
            usage.unknown_actions += 1

    return totals


def collect_snapshot(
    bucket: str,
    versions: Iterable[FileVersion],
    prefixes: Sequence[str],
) -> UsageSnapshot:
    """Run one aggregation and stamp it. Raises nothing it cannot explain."""
    started = time.time()
    per_prefix = aggregate(versions, prefixes)
    return UsageSnapshot(
        bucket=bucket,
        per_prefix=per_prefix,
        collected_at=started,
        duration_seconds=time.time() - started,
        success=True,
    )


class B2Collector(Collector):
    """Serves the last snapshot. Does no I/O -- a scrape must never reach Backblaze.

    Collection runs on its own timer (see `__main__`). If scrapes drove collection, then
    scrape rate would drive B2 transaction rate, and a second vmagent replica or one
    manual `curl` would silently double the bill.

    TWO FAILURE STATES, HANDLED DIFFERENTLY ON PURPOSE:

      cold   never collected     usage gauges ABSENT, success=0
      warm   collected, then failed   last known values RETAINED, success=0, age climbing

    Publishing `0 bytes` before the first successful collection would be a fabricated
    reading that a threshold alert reads as healthy. Retaining known-good values after a
    later failure is different: those bytes were true recently, and the age metric says
    exactly how recently.
    """

    def __init__(
        self,
        bucket: str,
        quota_bytes: int | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        self._snapshot = UsageSnapshot(bucket=bucket)
        self._quota_bytes = quota_bytes
        self._bucket = bucket
        self._namespace = namespace.rstrip("_")

    def _name(self, suffix: str) -> str:
        """Prefix a metric name with the configured namespace.

        Exists so two instances -- separate B2 accounts, or this alongside a fork --
        can coexist in one TSDB without colliding. It does NOT provide s3_exporter
        compatibility: see the migration table in README.md. Renaming a metric does
        not change what it measures, and `s3_objects_size_sum_bytes` means current
        objects while this exporter's headline number is billed bytes. Serving one
        under the other's name would be the failure this project exists to fix.
        """
        return f"{self._namespace}_{suffix}"

    def update(self, snapshot: UsageSnapshot) -> None:
        self._snapshot = snapshot

    def mark_failure(self) -> None:
        """Keep the previous numbers, but stop claiming the collection succeeded."""
        self._snapshot = replace(self._snapshot, success=False)

    def collect(self):
        snap = self._snapshot
        labels = ["bucket", "prefix"]

        yield GaugeMetricFamily(
            self._name("collection_success"),
            "1 if the most recent collection attempt succeeded, 0 otherwise",
            value=1 if snap.success else 0,
        )
        yield GaugeMetricFamily(
            self._name("last_collection_timestamp_seconds"),
            "Unix time of the last SUCCESSFUL collection; 0 if none has ever succeeded",
            value=snap.collected_at,
        )
        yield GaugeMetricFamily(
            self._name("collection_duration_seconds"),
            "Wall-clock duration of the last successful collection",
            value=snap.duration_seconds,
        )
        if self._quota_bytes is not None:
            yield GaugeMetricFamily(
                self._name("bucket_quota_bytes"),
                "Configured capacity ceiling for this bucket. Alert on the RATIO to this, "
                "never on a hardcoded byte count, so raising the tier is a one-number edit",
                value=self._quota_bytes,
            )

        # Cold start: no usage gauges at all. See the class docstring.
        if not snap.per_prefix:
            return

        families = [
            (
                "bucket_billed_bytes",
                "Bytes Backblaze bills for: all versions, including non-current and hidden",
                "billed_bytes",
            ),
            (
                "bucket_current_bytes",
                "Bytes a ListObjectsV2-based exporter would see. The "
                "shortfall against billed_bytes is its blind spot",
                "current_bytes",
            ),
            (
                "bucket_billed_objects",
                "Count of upload records across all versions",
                "billed_objects",
            ),
            (
                "bucket_current_objects",
                "Count of current, non-hidden objects",
                "current_objects",
            ),
            (
                "bucket_hide_markers",
                "Hide markers. Zero bytes each; they inflate object count but never storage cost",
                "hide_markers",
            ),
            (
                "bucket_unfinished_large_files",
                "Unfinished multipart uploads. Their parts "
                "are billed and no lifecycle rule cancels "
                "them by default",
                "unfinished_large_files",
            ),
            (
                "bucket_unknown_actions",
                "Version records with an action this exporter does "
                "not model. Non-zero means B2 grew a new one",
                "unknown_actions",
            ),
        ]
        for suffix, doc, attr in families:
            family = GaugeMetricFamily(self._name(suffix), doc, labels=labels)
            for prefix, usage in snap.per_prefix.items():
                family.add_metric([snap.bucket, prefix], getattr(usage, attr))
            yield family

        freshness = GaugeMetricFamily(
            self._name("bucket_last_upload_timestamp_seconds"),
            "Unix time of the newest upload under this prefix. Detects a sync that has "
            "silently stopped advancing",
            labels=labels,
        )
        for prefix, usage in snap.per_prefix.items():
            freshness.add_metric([snap.bucket, prefix], usage.last_upload_timestamp_ms / 1000.0)
        yield freshness
