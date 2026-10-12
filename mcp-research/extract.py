"""Turn raw bytes into Markdown: markitdown for everything (trafilatura additionally gives a
simplified main-content guess for HTML), stdlib zipfile to unpack archives (preserving the internal tree)."""

import io
import os
import zipfile

import trafilatura
from markdown_it import MarkdownIt
from markitdown import MarkItDown, StreamInfo

_md = MarkItDown()
_mdit = MarkdownIt("commonmark", {"linkify": True}).enable(["table", "linkify"])


def is_zip(content_type: str, filename: str) -> bool:
    return filename.lower().endswith(".zip") or content_type == "application/zip"


def html_to_markdown(html_bytes: bytes) -> str:
    """Lossless conversion of everything in the HTML (the client picks the container via `target`)."""
    info = StreamInfo(extension=".html", charset="utf-8")  # always UTF-8 from the browser: no detection
    return _md.convert_stream(io.BytesIO(html_bytes), stream_info=info).text_content


def html_to_simplified(html_bytes: bytes) -> str:
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


def _pieces(children) -> list[tuple[bool, str]]:
    """Inline tokens -> [(is_link_or_image, text)]; styling flattened, links/images as Markdown."""
    out, i = [], 0
    while i < len(children):
        t = children[i]
        if t.type in ("text", "code_inline"):
            out.append((False, t.content))
        elif t.type in ("softbreak", "hardbreak"):
            out.append((False, " "))
        elif t.type == "image":
            alt = "".join(s for _, s in _pieces(t.children or []))
            out.append((True, f"![{alt}]({t.attrGet('src')})"))
        elif t.type == "link_open":
            j = i + 1
            while children[j].type != "link_close":  # links cannot nest
                j += 1
            text = "".join(s for _, s in _pieces(children[i + 1:j]))
            out.append((True, f"<{text}>" if t.markup in ("autolink", "linkify") else f"[{text}]({t.attrGet('href')})"))
            i = j
        i += 1
    return out


def terse(markdown: str) -> str:
    """Outline of a Markdown document: headings, plus one `* ` bullet per link/image outside headings."""
    lines, level = [], 0
    for tok in _mdit.parse(markdown):
        if tok.type == "heading_open":
            level = int(tok.tag[1:])
        elif tok.type == "heading_close":
            level = 0
        elif tok.type == "inline":
            pieces = _pieces(tok.children or [])
            if level:
                lines.append("#" * level + " " + "".join(s for _, s in pieces))
            else:
                lines += [f"* {s}" for is_link, s in pieces if is_link]
    return "\n".join(lines)
