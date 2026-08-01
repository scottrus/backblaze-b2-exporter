# backblaze-b2-exporter

Prometheus exporter for **Backblaze B2 bucket usage as billed** — counting non-current
versions and hidden files that current-object listings miss — with per-prefix attribution
and upload freshness.

## Why this exists

Generic S3 exporters build on `ListObjectsV2`, which returns **current objects only**: no
non-current versions, no hidden files. **Backblaze bills for all of them.**

That last sentence is the entire premise, so here is the receipt. From Backblaze's own
[lifecycle rules documentation](https://www.backblaze.com/docs/cloud-storage-lifecycle-rules):

> The hidden files are still available for download using their specific `file_id` and
> **continue to count as part of your Backblaze B2 storage amount.**

> **Until they are deleted**, the files are still available for download and count as part of
> your Backblaze B2 storage amount.

The API's own shape corroborates it: a lifecycle rule has *two* steps,
`daysFromUploadingToHiding` and `daysFromHidingToDeleting`. If hiding stopped the charges, a
separate delete step would be pointless. **Hiding makes a file invisible; deleting is what
frees storage.**

A file name can therefore have versions in three states, and a listing shows you one of them:

| State | In `ListObjectsV2` / `b2 ls` | Stored and billed |
|---|---|---|
| current — newest upload | yes | yes |
| non-current — superseded by a newer upload | **no** | yes |
| hidden — a hide marker sits on top | **no** | **yes, until deleted** |

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

---

## ⚠️ Read this before you deploy it

### This project was vibe-coded

It was written largely by an AI coding assistant, with a human reviewing and directing,
against a real Backblaze B2 bucket holding a real backup pipeline. That is disclosed here
so you can weigh it honestly rather than discovering it from the commit history.

**What is actually grounded:**

- The billing premise is quoted from Backblaze's own documentation, not assumed — see above.
  An earlier draft of this README cited an SDK issue that does not support the claim, and
  that was caught in review.
- The aggregation logic is tested against a **real captured listing**, including a hidden
  file with two superseded versions — the exact case a `ListObjectsV2` exporter cannot see.
- The container builds reproducibly, runs as uid 65532, and scans clean.

**What has not been exercised, stated plainly:**

- **It has never made a real B2 API call.** `client.py` — the b2sdk integration — has **no
  test coverage**. Everything proven above is proven against a fixture. The first real
  collection will be the first time that code path runs.
- **Version ordering across pagination boundaries is unverified.** Current-vs-superseded is
  decided by file-name transitions in a single pass, which relies on B2 returning versions
  grouped by name, newest first. That held in the one capture used here, over a single page.
  If it does not hold across page boundaries, `ListingOrderError` should fire rather than
  produce wrong numbers — but that guard is itself untested against real pagination.
- **One bucket shape.** The workloads behind the test data write uniquely-named objects, so
  non-current versions are rare and the dominant effect is lifecycle-hidden files. Buckets
  with heavy version churn, many unfinished large files, or hundreds of prefixes are handled
  generically and are far less exercised.
- The 50,000-object memory test uses a synthetic in-memory generator, not real paginated
  API responses.

Read the source before trusting it — it is deliberately short and heavily commented. Bug
reports from people running different bucket shapes are genuinely wanted.

### Do not give it a write-capable key

This is a network service that only ever needs to **list**. `listBuckets,listFiles` is
sufficient; it never downloads object content, so even `readFiles` is unnecessary. And do
not mint the key in the Backblaze web UI — its "Write Only" preset silently grants
`deleteFiles` **and** `bypassGovernance`, which is precisely backwards for a bucket whose
whole purpose is being hard to delete from.

---

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

### Metric naming, and why these are not `s3_*`

**There is no standard to conform to here.** OpenTelemetry's semantic conventions do not
cover object-storage bucket metrics, and Prometheus has no community spec for them.
[`ribbybibby/s3_exporter`](https://github.com/ribbybibby/s3_exporter) is the closest thing to
a de-facto convention, but its names are one project's choices rather than a contract.

The convention that *does* exist — Prometheus base units and `_bytes` / `_seconds` suffixes —
is followed here. Notably `s3_last_modified_object_date` does not follow it: no unit suffix,
and "date" where it means seconds. Copying that set wholesale would import the flaw.

More importantly, **only two of its metrics mean the same thing as anything here**:

| `s3_exporter` | Equivalent here | Same thing? |
|---|---|---|
| `s3_objects_size_sum_bytes` | `b2_bucket_current_bytes` | yes |
| `s3_objects` | `b2_bucket_current_objects` | yes |
| `s3_last_modified_object_date` | `b2_bucket_last_upload_timestamp_seconds` | close — ours spans all versions, not just current |
| `s3_list_success` | `b2_collection_success` | close — ours is per collection, not per prefix |
| `s3_list_duration_seconds` | `b2_collection_duration_seconds` | close — same caveat |
| `s3_biggest_object_size_bytes`, `s3_last_modified_object_size_bytes`, `s3_common_prefixes` | — | not computed |
| — | `b2_bucket_billed_bytes` and everything version-aware | **no S3 equivalent exists** |

**The headline metric has no counterpart at all**, which is the whole reason this exists.
Serving billed bytes under `s3_objects_size_sum_bytes` would make every existing dashboard
silently display a different quantity — the exact failure mode this project was built to
fix, reintroduced at the naming layer. Partial compatibility is worse than none when it is
silent.

If you are migrating, rewrite the two exact matches and treat the rest as new.

### Metric prefix

The default namespace is `b2`. It is configurable **solely to avoid collisions** — two
accounts, two buckets scraped by separate deployments, or this running alongside a fork.

**Every metric goes through it**, usage gauges and collector meta alike; a prefix covering
only some of them would be worse than none, because the uncovered ones are exactly what
collides. That is enforced by an exhaustive test, not a spot check.

```
b2                b2_bucket_billed_bytes,   b2_collection_success
acme_offsite      acme_offsite_bucket_billed_bytes,   acme_offsite_collection_success
"" (empty)        bucket_billed_bytes,      collection_success
```

An empty value degrades to unprefixed names rather than a leading underscore, and a trailing
underscore (`acme_`) is tolerated rather than doubled — both are what a ConfigMap key
eventually gets set to by someone in a hurry.

Renaming does not change meaning. Setting it to `s3` yields `s3_bucket_billed_bytes`, never
`s3_objects_size_sum_bytes`, and there is a test asserting exactly that.

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

The exporter needs **two capabilities and nothing else**: `listBuckets` to resolve the
bucket name, and `listFiles` to enumerate versions. It never downloads object content, so
**`readFiles` is not required** — a key for this exporter is strictly weaker than a restore
key.

### Creating the key

```bash
b2 key create --bucket example-backups b2-exporter listBuckets,listFiles
```

Capabilities are **one comma-separated positional argument**, and `--bucket` takes the
bucket **name**, not its ID.

Verify what you actually got, since a key is easy to over-grant and hard to notice:

```bash
b2 key list --long | grep b2-exporter
```

> ⚠️ **Do not create this key in the Backblaze web UI.** Its "Write Only" preset silently
> grants **`deleteFiles` *and* `bypassGovernance`** — the two capabilities that most defeat
> the point of an append-only, Object-Locked backup bucket. However the preset is labelled,
> a key minted through the UI is wrong for this purpose. Use the CLI (or the native
> `b2_create_key` API) where you state capabilities explicitly.

> ⚠️ **`b2 key create` prints the secret to stdout**, and it is the only time B2 will ever
> return it. Treat that terminal as sensitive: `applicationKeyId` is an identifier and safe
> to share, `applicationKey` is the credential and must not reach a transcript, a chat, a
> ticket, or shell history.

**`listAllBucketNames` will be rejected** when the key is bucket-scoped — it is
account-scoped and the two are mutually exclusive. `listBuckets` is the bucket-scoped
equivalent and is what you want.

### Restricting to a path — and why you probably shouldn't

`--name-prefix` scopes a key to keys beginning with a given string:

```bash
# Scoped to one path. Read the trade-off below before using this.
b2 key create --bucket example-backups --name-prefix etcd/ b2-exporter-etcd listBuckets,listFiles
```

That works, and for a shared bucket where you genuinely must not see other tenants' keys it
is the right call. But it costs you the two things this exporter is most useful for:

- **The bucket total disappears.** You can no longer answer "how full is the bucket," only
  "how big is this prefix" — and B2 bills, and enforces caps, per account.
- **The `other` series goes blind.** Anything written outside your configured prefixes is
  invisible, so a stray uploader or a misconfigured job stops being detectable.

So the default recommendation is **bucket-scoped without `--name-prefix`**, and let the
exporter's configured prefixes do the attribution. Reach for `--name-prefix` only when
something other than this exporter requires the isolation.

### Proving the key cannot write

"Read-only" is a property of the key, not of your intent, so it is worth checking once.

**Check the capabilities, do not attempt a write.** `b2 key list --long` shows each key's
bucket restriction and its full capability list, which is a direct assertion with no side
effects:

```bash
b2 key list --long | grep b2-exporter
```

You want exactly `listBuckets,listFiles` and the bucket name — nothing else, and in
particular never `deleteFiles` or `bypassGovernance`.

> ⚠️ **Do not "prove" it by trying to upload a file, if the bucket has Object Lock.**
>
> The obvious test — attempt a write, expect a refusal — has an asymmetric cost. If the key
> turns out to be over-granted, the write **succeeds**, and on a bucket with Object Lock in
> Compliance mode that test file **cannot then be deleted by anyone, including the account
> root, until its retention expires.** You would be stuck with it for the full retention
> window, and it would count toward billed storage and pollute per-prefix attribution the
> whole time.
>
> So the test only leaves residue in exactly the case where it tells you something — which
> is the worst possible trade. Read the capabilities instead.

### Optional: reporting Object Lock state

To also report retention, the key additionally needs `readFileRetentions` and
`readBucketRetentions`. Without them B2 returns `"mode": "unknown"` rather than an error —
which is a **permission gap and not evidence that Object Lock is off**, a distinction that
is very easy to misread as reassurance.

### Key expiry

`--duration SECONDS` creates an expiring key. That is good hygiene, with one caveat worth
knowing up front: when it expires the exporter stops collecting, and because the last good
snapshot is retained, the *usage* gauges keep reading plausibly. `b2_collection_success`
drops to 0 and `b2_last_collection_timestamp_seconds` stops advancing — **alert on those,
not on the usage metrics**, or an expired key looks exactly like a bucket that stopped
growing.

## Status

**Alpha. Runnable, not yet released.** The collector, CLI, background refresh loop, HTTP
server, container image and Helm chart are all implemented, and the full gate — lint,
tests on 3.11/3.13/3.14, workflow lint, `helm lint` with template permutations and
kubeconform, hadolint, image build and smoke test, and a CVE scan — passes on every push.

**No image is published yet.** `ghcr.io/scottrus/backblaze-b2-exporter` appears when a
`v*.*.*` tag is cut; until then, build locally with `make docker`.

**It has still never collected from a real bucket.** See the disclosure above — that is the
honest limit on everything else here.

Metric names may change before 1.0.

## License

Apache-2.0
