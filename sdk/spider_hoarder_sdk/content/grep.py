"""Regex search over job text (get_file_text / grep_file); the gateway's own, parsing lives in the parser service."""

import re


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
