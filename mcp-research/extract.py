"""Turn raw bytes into Markdown: trafilatura for HTML, markitdown for everything
else, stdlib zipfile to unpack archives (preserving the internal tree)."""

import io
import os
import zipfile

import trafilatura
from markitdown import MarkItDown

_md = MarkItDown()


def is_zip(content_type: str, filename: str) -> bool:
    return filename.lower().endswith(".zip") or content_type == "application/zip"


def html_to_markdown(html_bytes: bytes) -> str:
    text = trafilatura.extract(html_bytes.decode("utf-8", errors="replace"), output_format="markdown")
    return text or ""


def file_to_markdown(data: bytes, filename: str) -> str:
    result = _md.convert_stream(io.BytesIO(data), file_extension=os.path.splitext(filename)[1])
    return result.text_content


def safe_zip_members(zf: zipfile.ZipFile) -> list[str]:
    """Reject zip-slip members (absolute paths or '..' segments) before extraction."""
    names = []
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        norm = os.path.normpath(name)
        if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
            continue
        names.append(name)
    return names


def unpack_zip(data: bytes) -> dict[str, bytes]:
    """Returns {relative_path: bytes} for every safe member in the zip."""
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in safe_zip_members(zf):
            out[name] = zf.read(name)
    return out
