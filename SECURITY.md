# Security

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/scottrus/backblaze-b2-exporter/security/advisories/new)
rather than opening a public issue.

## The application key is the whole threat model

This exporter is a long-running network service holding a Backblaze B2
application key in memory. Unlike some APIs, **B2 keys are genuinely scopable**:
per-bucket, per-capability, optionally per-prefix. Least privilege is achievable
here, which means over-granting is a choice rather than a limitation.

Give it exactly this and nothing more:

```bash
b2 key create --bucket your-bucket b2-exporter listBuckets,listFiles
```

`readFiles` is **not** required — the exporter never downloads object content, only
metadata. A key for this is strictly weaker than a restore key.

### Do not create the key in the Backblaze web UI

Its **"Write Only" preset silently grants `deleteFiles` *and* `bypassGovernance`.**
Those are the two capabilities most worth withholding, and the preset's name gives
no hint that it includes them. Use the CLI or the native `b2_create_key` API, where
capabilities are stated explicitly and can be read back.

### Why the stakes are higher than "someone reads my metrics"

This exporter is typically pointed at a backup bucket, and backup buckets are
frequently protected by **Object Lock in Compliance mode** — the guarantee that
nothing, not even the account root, can delete an object before its retention
expires. That guarantee is what makes the bucket ransomware-resistant.

A key holding `bypassGovernance` is a hole straight through it. Granting one to a
metrics exporter would trade the entire point of the bucket for a number you could
have obtained with `listFiles`.

### What the exporter actually calls

Every B2 operation it performs, in full:

| Call | Purpose |
|---|---|
| `b2_authorize_account` | authenticate |
| `b2_list_buckets` | resolve the bucket name to an id |
| `b2_list_file_versions` | enumerate versions, including hidden ones |

All are reads. **The exporter has no code path that writes, deletes, or hides.**

This table is the exporter's privilege surface, and changes to it are announced
rather than slipped in:

- **A new read call** is a **minor** bump, called out under its own heading in the
  changelog, with this table updated in the same change.
- **Any call that writes, deletes or hides** would be a **major** bump, and is not
  something this project intends to do. Adding one changes what a key here can
  cost you.

Treat any build asking for capabilities beyond `listBuckets,listFiles` as suspect,
and check the diff.

## Handling the key

- **`applicationKeyId` is an identifier and safe to share. `applicationKey` is the
  credential** and must not reach a transcript, a ticket, or shell history.
- `b2 key create` prints the secret to stdout, and it is the **only** time B2 will
  return it. Treat that terminal as sensitive.
- Credentials are read from the environment **only**. There is deliberately no
  `--application-key` flag: anything in `argv` is visible in `ps`, in a container's
  `spec.containers[].args`, and in crash dumps that capture the command line. The
  test suite asserts the parser rejects such a flag.
- In Kubernetes, use a Secret — either rendered by the chart or referenced with
  `b2.existingSecret`. The exporter never logs credential values.

## What the exporter exposes

`/metrics` is unauthenticated, as exporters conventionally are. It publishes byte
counts, object counts and timestamps per configured prefix — **no object names, no
object content, no credentials**. Prefix *names* appear as label values, so treat
them as you would any label: they are visible to anything that can scrape the
endpoint.
