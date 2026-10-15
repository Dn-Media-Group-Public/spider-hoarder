"""Turn raw bytes into Markdown: markitdown for everything (trafilatura additionally gives a
simplified main-content guess for HTML), stdlib zipfile to unpack archives (preserving the internal tree)."""

import io
import os
import re
import zipfile

import lxml.html
import trafilatura
from markdown_it import MarkdownIt
from markitdown import MarkItDown, StreamInfo

_md = MarkItDown()
_mdit = MarkdownIt("commonmark", {"linkify": True}).enable(["table", "linkify"])


def is_zip(content_type: str, filename: str) -> bool:
    return filename.lower().endswith(".zip") or content_type == "application/zip"


def absolutize(html_bytes: bytes, base_url: str) -> bytes:
    """Resolve relative href/src against the page URL (honouring <base href>) so saved Markdown links work."""
    if not base_url or not html_bytes.strip():
        return html_bytes
    try:
        doc = lxml.html.fromstring(html_bytes.decode("utf-8", errors="replace"), base_url=base_url)
        doc.make_links_absolute(base_url)
        return lxml.html.tostring(doc, encoding="utf-8")
    except Exception:  # ponytail: malformed HTML falls back to unresolved links
        return html_bytes


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


def grep_text(text: str, pattern: str, context: int = 2, max_matches: int = 50, width: int = 300, limit: int = 50_000) -> str:
    """Matching lines with `context` lines around them, as `line N (offset O)` blocks; overlapping blocks merge.
    `pattern` is a Python regex, or /pattern/i. Lines are trimmed to ~`width` chars around the match."""
    m = re.fullmatch(r"/(.*)/([a-z]*)", pattern, re.S)
    try:
        rx = re.compile(m.group(1) if m else pattern, re.I if m and "i" in m.group(2) else 0)
    except re.error as e:
        raise ValueError(f"invalid regex: {e}")
    lines = text.split("\n")
    offsets, o = [], 0
    for l in lines:
        offsets.append(o)
        o += len(l) + 1
    spans = {i: s.span() for i, l in enumerate(lines) if (s := rx.search(l))}
    if not spans:
        return f"No matches for {pattern}."
    shown = list(spans)[:max_matches]
    context = max(context, 0)
    blocks: list[list[int]] = []
    for h in shown:
        lo, hi = max(0, h - context), min(len(lines) - 1, h + context)
        if blocks and lo <= blocks[-1][1] + 1:
            blocks[-1][1] = hi
        else:
            blocks.append([lo, hi])

    def trim(i: int) -> str:
        l = lines[i]
        s, e = spans.get(i, (0, 0))
        if len(l) <= width:
            return l
        lo = min(max(0, (s + e) // 2 - width // 2), len(l) - width)
        return ("..." if lo else "") + l[lo:lo + width] + ("..." if lo + width < len(l) else "")

    out, size = [], 0
    for lo, hi in blocks:
        block = "\n".join(f"{i + 1}{':' if i in spans else '-'} {trim(i)}" for i in range(lo, hi + 1))
        block = f"-- line {lo + 1}, offset {offsets[lo]} --\n{block}"
        if size + len(block) > limit:
            break
        out.append(block)
        size += len(block)
    cut = len(shown) < len(spans) or len(out) < len(blocks)
    note = f"\n\n[showing {sum(h <= b[1] for b in blocks[:len(out)] for h in shown if h >= b[0])} of {len(spans)} matches]" if cut else ""
    return "\n\n".join(out) + note


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
