"""fsspec-backed blob storage in one shared bucket (s3:// or gs://: no local or in-memory store, every replica must see the same
files). A job lives under the key `projects/<proj>/data/<session>/<job>` (see job_key), with original|parsed|... files and an
index.json manifest; projects/<proj>/access.json and users/<user>/<proj> sit beside it (gateway/projects.py). Every function takes a
key relative to the bucket root. $RESEARCH_BLOB_OPTIONS (a JSON object) is handed to fsspec as is: credentials, endpoint, ...
`creds` (more fsspec options, e.g. a per-job token from gateway/creds.py) selects a per-call filesystem with those credentials
instead of the gateway's own."""

import contextlib
import io
import json
import os

import fsspec

BLOB_URL = os.environ.get("RESEARCH_BLOB_URL", "")
BLOB_OPTIONS = json.loads(os.environ.get("RESEARCH_BLOB_OPTIONS") or "{}")
SCHEMES = ("s3", "s3a", "gs", "gcs")


def job_key(proj: str, session: str, job: str) -> str:
    """The one place a job's storage key is built (callers validate the three uuids)."""
    return f"projects/{proj}/data/{session}/{job}"


def _fs(creds: dict | None = None):
    """The bucket's filesystem. Directory listings are never cached (the shared bucket is written by other replicas: s3fs would
    keep a stale listing for the life of the process); a credentialed one is not kept in fsspec's instance cache either (one
    per minted credential would pile up)."""
    if fsspec.utils.get_protocol(BLOB_URL) not in SCHEMES:
        raise RuntimeError(f"RESEARCH_BLOB_URL must be s3://bucket or gs://bucket, not {BLOB_URL!r}")
    return fsspec.core.url_to_fs(BLOB_URL, use_listings_cache=False, **{**BLOB_OPTIONS, **({"skip_instance_cache": True, **creds} if creds else {})})


def _abs(key: str, creds: dict | None = None) -> tuple:
    fs, root = _fs(creds)
    return fs, "/".join([root.rstrip("/"), key])


def read_object(key: str) -> bytes | None:
    """A bucket object by key, None if missing. One plain GET: s3fs's open() pins the ETag and raises FileExpired when another writer
    replaces the object (access.json, index.json) between its HEAD and its reads."""
    fs, path = _abs(key)
    try:
        return fs.cat_file(path)
    except FileNotFoundError:
        return None


def write_object(key: str, data: bytes) -> None:
    fs, path = _abs(key)
    fs.makedirs(os.path.dirname(path), exist_ok=True)
    with fs.open(path, "wb") as f:
        f.write(data)


def delete_object(key: str) -> None:
    fs, path = _abs(key)
    with contextlib.suppress(FileNotFoundError):
        fs.rm(path)


def list_names(prefix: str) -> list[str]:
    """Names (last path component) of the entries directly under a key prefix."""
    fs, path = _abs(prefix)
    try:
        return [os.path.basename(p.rstrip("/")) for p in fs.ls(path, detail=False)]
    except FileNotFoundError:
        return []


def delete_tree(prefix: str) -> None:
    """Delete everything under a key prefix and verify it. s3fs's bulk delete drops the per-key `Errors` of
    DeleteObjects (the HTTP status is 200 even for failures), so what is left is listed afterwards and raised."""
    fs, path = _abs(prefix)
    with contextlib.suppress(FileNotFoundError):
        fs.rm(path, recursive=True)
    fs.invalidate_cache()
    # files only: SeaweedFS keeps an empty directory entry that no S3 client can see as an object
    if left := [p for p, i in fs.find(path, detail=True).items() if i["type"] == "file"]:
        raise OSError(f"delete of {prefix} left {len(left)} object(s), e.g. {left[0]}")


def _job_path(job_id: str, *parts: str) -> str:
    return _abs("/".join([job_id, *parts]))[1]


def write_stream(job_id: str, rel_path: str, fileobj, content_type: str) -> int:
    """Copy `fileobj` into the job in 1 MiB chunks (constant memory); returns the size written."""
    fs, _ = _fs()
    path = _job_path(job_id, rel_path)
    fs.makedirs(os.path.dirname(path), exist_ok=True)
    size = 0
    with fs.open(path, "wb") as f:
        while chunk := fileobj.read(1 << 20):
            f.write(chunk)
            size += len(chunk)
    _update_manifest(job_id, rel_path, size, content_type)
    return size


def write_bytes(job_id: str, rel_path: str, data: bytes, content_type: str) -> None:
    write_stream(job_id, rel_path, io.BytesIO(data), content_type)


def open_read(job_id: str, rel_path: str):
    fs, _ = _fs()
    return fs.open(_job_path(job_id, rel_path), "rb")


def read_bytes(job_id: str, rel_path: str) -> bytes:
    fs, _ = _fs()
    with fs.open(_job_path(job_id, rel_path), "rb") as f:
        return f.read()


def read_text(job_id: str, rel_path: str) -> str:
    return read_bytes(job_id, rel_path).decode("utf-8")


def exists(job_id: str, rel_path: str) -> bool:
    fs, _ = _fs()
    return fs.exists(_job_path(job_id, rel_path))


def get_manifest(job_id: str) -> dict:
    raw = read_object(f"{job_id}/index.json")
    return {"files": []} if raw is None else json.loads(raw)


def set_meta(job_id: str, keep_none: tuple = (), **kw) -> None:
    """Merge top-level keys (e.g. url, target) into index.json; None values are skipped, except keys in `keep_none`."""
    manifest = get_manifest(job_id)
    manifest.update({k: v for k, v in kw.items() if v is not None or k in keep_none})
    fs, _ = _fs()
    with fs.open(_job_path(job_id, "index.json"), "w") as f:
        json.dump(manifest, f)


def _update_manifest(job_id: str, rel_path: str, size: int, content_type: str) -> None:
    manifest = get_manifest(job_id)
    manifest["files"] = [e for e in manifest["files"] if e["path"] != rel_path]
    manifest["files"].append({"path": rel_path, "size": size, "content_type": content_type})
    fs, _ = _fs()
    with fs.open(_job_path(job_id, "index.json"), "w") as f:
        json.dump(manifest, f)
