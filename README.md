# backblaze-b2-exporter

Prometheus exporter for **Backblaze B2 bucket usage as billed** — counting non-current
versions and hidden files that current-object listings miss — with per-prefix attribution
and upload freshness.

## Why this exists

Generic S3 exporters build on `ListObjectsV2`, which returns **current objects only**: no
non-current versions, no hidden files. **Backblaze bills for all of them.**

On any bucket with a lifecycle rule the gap is structural and permanent. Given a typical
retention setup:

```
daysFromUploadingToHiding: 21     object stops being visible to ListObjectsV2
daysFromHidingToDeleting:  1      object is still BILLED for another full day
```

...effective billed retention is **22 days while a current-objects collector sees 21**. It
reads low by a fixed margin, forever, and reports healthy while doing it. On an hourly
backup pipeline that is a permanent ~5% blind spot; when a large weekly artifact rotates,
it spikes far higher — precisely when you most need the number to be right.

Backblaze exposes no bucket-usage endpoint.
[b2-sdk-python#137](https://github.com/Backblaze/b2-sdk-python/issues/137), asking for
exactly these metrics, has been open since June 2020 with no resolution — the data is
visible in the B2 web UI but not available through the API. So usage has to be derived by
listing, and **listing versions is the only way to derive the number that gets billed.**

This exporter reports both, and lets you see the difference:

| Metric | Meaning |
|---|---|
| `b2_bucket_billed_bytes` | what Backblaze charges for |
| `b2_bucket_current_bytes` | what a `ListObjectsV2`-based exporter would have seen |

Their difference is the blind spot, quantified continuously rather than asserted in a
README.

## Metrics

| Metric | Labels | Notes |
|---|---|---|
| `b2_bucket_billed_bytes` | `bucket`, `prefix` | all versions, including hidden |
| `b2_bucket_current_bytes` | `bucket`, `prefix` | current, non-hidden only |
| `b2_bucket_billed_objects` | `bucket`, `prefix` | upload records across all versions |
| `b2_bucket_current_objects` | `bucket`, `prefix` | |
| `b2_bucket_hide_markers` | `bucket`, `prefix` | 0 bytes each — inflate counts, never cost |
| `b2_bucket_unfinished_large_files` | `bucket`, `prefix` | billed, and **not** cancelled by default |
| `b2_bucket_unknown_actions` | `bucket`, `prefix` | non-zero means B2 grew a new action type |
| `b2_bucket_last_upload_timestamp_seconds` | `bucket`, `prefix` | detects a sync that stopped advancing |
| `b2_bucket_quota_bytes` | `bucket` | your configured ceiling; alert on the **ratio** |
| `b2_collection_success` | | 1/0 for the most recent attempt |
| `b2_last_collection_timestamp_seconds` | | last **successful** collection |
| `b2_collection_duration_seconds` | | |

**Alert on the ratio to `b2_bucket_quota_bytes`, never on a hardcoded byte count.** Raising
your capacity tier then becomes a one-number edit, instead of leaving rules quietly warning
against a ceiling that no longer exists.

## Design notes

**Prefixes are configured, never discovered.** Deriving label values from object keys makes
series cardinality a function of what someone happens to upload — one writer using
per-date or per-UUID keys would permanently inflate cardinality in your TSDB, which no pod
restart undoes. Unmatched keys land in a single `other` series, which doubles as a useful
signal: anything writing outside your known workloads shows up there.

**Collection is timer-driven; scrapes never reach Backblaze.** If scrapes drove collection,
scrape rate would drive B2 transaction rate — a second Prometheus replica or one manual
`curl` would silently double the bill. `/metrics` only ever reads process state.

**Run one replica.** Scaling horizontally multiplies B2 transactions and produces duplicate
series for no availability gain: the exporter being down briefly loses nothing, because the
next collection re-reads absolute values rather than deltas.

**Nothing is cached to disk, and memory is O(prefixes) not O(objects).** The listing is
streamed and folded, never materialised. Each collection replaces the previous snapshot
wholesale — time-series history is your TSDB's job, and keeping a second copy in process
memory is how a cache becomes a memory leak.

**Cold and stale are different states, deliberately:**

| State | Usage gauges | Meta |
|---|---|---|
| never collected | **absent** | `b2_collection_success 0` |
| collected, latest attempt failed | last known values **retained** | `success 0`, age climbing |

Publishing `0 bytes` before the first successful collection would be a fabricated reading
that a threshold alert reads as healthy. Retaining known-good values after a *later* failure
is different — those bytes were true recently, and the age metric says exactly how recently.

## Credentials

A read-only application key with `listFiles` (and `listBuckets`) on the target bucket is
sufficient. Do not give this a write-capable key.

To also report Object Lock state, the key additionally needs `readFileRetentions` /
`readBucketRetentions`; without them B2 returns `"mode": "unknown"` rather than an error,
which is a **permission gap and not evidence that Object Lock is off**.

## Status

Alpha. Metric names may change before 1.0.

## License

Apache-2.0
