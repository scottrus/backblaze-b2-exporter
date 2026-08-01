"""B2 access: authorize, and stream file versions out of a bucket.

This module deliberately owns *only* the I/O. Everything that turns records into
numbers lives in `collector.py`, so the arithmetic can be tested against a captured
fixture without credentials, a network, or a single billed transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

log = logging.getLogger(__name__)

# B2 `action` values we care about. Anything else is counted as `OTHER_ACTION`
# rather than silently dropped -- see collector.aggregate().
ACTION_UPLOAD = "upload"
ACTION_HIDE = "hide"
ACTION_START = "start"


@dataclass(frozen=True, slots=True)
class FileVersion:
    """One row of `b2_list_file_versions`, reduced to what the collector needs.

    Kept as a plain dataclass rather than the SDK's own type so that tests can
    build them straight from a captured JSON fixture without importing b2sdk.
    """

    file_name: str
    size: int
    action: str
    upload_timestamp_ms: int

    @classmethod
    def from_api(cls, raw: dict) -> FileVersion:
        """Build from a raw `b2_list_file_versions` entry (or `b2 ls --json --versions`)."""
        return cls(
            file_name=raw["fileName"],
            # Hide markers carry size 0; `start` rows (unfinished large files) may
            # omit size entirely, and those bytes ARE billed until cancelled.
            size=int(raw.get("size") or 0),
            action=raw.get("action") or ACTION_UPLOAD,
            upload_timestamp_ms=int(raw.get("uploadTimestamp") or 0),
        )


class B2Client:
    """Thin wrapper over b2sdk, exposing exactly one operation: stream versions.

    NEVER materialises the listing. `iter_file_versions` is a generator all the way
    down so memory stays O(page) rather than O(bucket) -- an exporter that holds a
    copy of the bucket in RAM is a memory leak wearing a cache's clothes.
    """

    def __init__(self, key_id: str, application_key: str, realm: str = "production") -> None:
        self._key_id = key_id
        self._application_key = application_key
        self._realm = realm
        self._api = None

    def _connect(self):
        if self._api is not None:
            return self._api
        # Imported lazily so that importing this module -- which tests do, for
        # FileVersion -- does not require b2sdk to be installed.
        from b2sdk.v2 import B2Api, InMemoryAccountInfo

        info = InMemoryAccountInfo()
        api = B2Api(info)
        api.authorize_account(self._realm, self._key_id, self._application_key)
        self._api = api
        return api

    def iter_file_versions(self, bucket_name: str) -> Iterator[FileVersion]:
        """Yield every version in the bucket, including hide markers.

        ORDERING IS LOAD-BEARING. B2 returns versions grouped by file name in
        lexicographic order, newest version first within each name. `collector.aggregate`
        relies on that to tell a current object from a superseded one while streaming.
        The guard for it lives there, not here, so it is exercised by the fixture tests.
        """
        api = self._connect()
        bucket = api.get_bucket_by_name(bucket_name)
        for version, _folder in bucket.ls(latest_only=False, recursive=True):
            yield FileVersion(
                file_name=version.file_name,
                size=int(version.size or 0),
                action=version.action or ACTION_UPLOAD,
                upload_timestamp_ms=int(version.upload_timestamp or 0),
            )
