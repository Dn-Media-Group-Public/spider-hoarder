"""fsspec-backed blob storage: <job_uuid>/original|parsed/... + index.json manifest."""

import io
import json
import os

import fsspec

BLOB_URL = os.environ.get("RESEARCH_BLOB_URL", "file:///data")


def _fs():
    return fsspec.core.url_to_fs(BLOB_URL)


def _job_path(job_id: str, *parts: str) -> str:
    fs, root = _fs()
    return "/".join([root.rstrip("/"), job_id, *parts])


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
    fs, _ = _fs()
    manifest_path = _job_path(job_id, "index.json")
    if not fs.exists(manifest_path):
        return {"files": []}
    with fs.open(manifest_path, "r") as f:
        return json.load(f)


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
