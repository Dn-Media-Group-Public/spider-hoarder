"""Per-job store credentials for the parser service: this project's policy on top of the cadmus-parser minting helper
(docs/implemented-plans/parse-sandbox-library.md). A parse job may READ the project's data side (`projects/<proj>/data`: a parser may legitimately
correlate with earlier files of the same project) and WRITE one job's directory (`projects/<proj>/data/<session>/<job>`). The helper mints the
credential (S3: a role per store, deleted by `revoke`; GCS: a Credential Access Boundary); this module keeps a registry of what this replica
has minted, so that `revoke_project` can end every credential of a deleted project, and runs the sweep of roles a crashed gateway left behind.
Every failure raises: there is no fallback to the gateway's own credential."""

import logging
import threading

from cadmus_parser import minting

from spider_hoarder_sdk.content import storage

log = logging.getLogger("creds")
LIFETIME = minting.LIFETIME
_lock = threading.Lock()
active: dict[int, tuple[str, minting.Handle]] = {}  # id(handle) -> (project, handle) minted by this replica and not yet revoked


def urls(key: str) -> tuple[str, str, str]:
    """(project data url, input url, output url) of the job at storage `key` (`projects/<proj>/data/<session>/<job>`)."""
    proj = key.split("/")[1]
    root = storage.BLOB_URL.rstrip("/")
    return f"{root}/projects/{proj}/data", f"{root}/{key}/original", f"{root}/{key}"


def grants(key: str) -> list[tuple[str, str]]:
    """Project-wide read of the data side, write of the job's own directory (the output base, which also holds the originals' sibling `original/`)."""
    data, _, out = urls(key)
    return [(data, "read"), (out, "write")]


def mint(key: str) -> minting.Handle:
    h = minting.mint(grants(key), minting.LIFETIME, storage.BLOB_OPTIONS)
    track(key.split("/")[1], h)
    return h


def track(proj: str, h: minting.Handle) -> None:
    with _lock:
        active[id(h)] = (proj, h)


def release(h: minting.Handle) -> None:
    with _lock:
        active.pop(id(h), None)
    h.revoke()


def revoke_project(proj: str) -> int:
    """End every credential this replica minted for the project (S3: the roles are deleted). Other replicas' expire within LIFETIME."""
    with _lock:
        mine = [h for p, h in active.values() if p == proj]
    for h in mine:
        try:
            release(h)
        except Exception:  # noqa: BLE001
            log.exception("could not revoke a credential of project %s", proj)
    return len(mine)


def sweep(max_age: float = LIFETIME + 300) -> int:
    """Delete job roles older than any credential could still live: those a crashed gateway never released. S3 only."""
    if storage.BLOB_URL.split("://", 1)[0] not in ("s3", "s3a"):
        return 0
    return minting.sweep(max_age, storage.BLOB_OPTIONS)
