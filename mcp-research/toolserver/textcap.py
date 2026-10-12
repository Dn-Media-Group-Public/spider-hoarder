"""Cap the pure text nodes of an aria snapshot so navigation output stays small. No playwright import:
the gateway imports DEFAULT_TEXT_CAP from here."""

import re

DEFAULT_TEXT_CAP = 128  # chars; 98.5% of h1-h3 and 99.6% of link texts fit, paragraphs shrink to ~61%
MARK = "..."

# `- text: …` or `- paragraph [ref=e1]: …` (an optional quoted name is allowed); group 1 = up to ": ", 2 = the text.
# Quoted names (`link "…"`), attributes and `- /url: …` lines never match as text.
_TEXT = re.compile(r'^(\s*- [\w-]+(?: "[^"]*")?(?: \[[^\]]*\])*: )(.+)$')


def cap_snapshot(text: str, cap: int | None) -> str:
    if cap is None:
        return text
    def one(line: str) -> str:
        m = _TEXT.match(line)
        return m.group(1) + m.group(2)[:cap] + MARK if m and len(m.group(2)) > cap else line
    return "\n".join(one(l) for l in text.split("\n"))


def cap_found_line(line: str, match_span: tuple[int, int] | None, cap: int | None) -> str:
    """Like cap_snapshot for one line shown by browser_find: a long text is cut to a window of `cap`
    characters centered on the match (if it is inside the text), with `...` on each cut side."""
    m = _TEXT.match(line)
    if cap is None or not m or len(m.group(2)) <= cap:
        return line
    text, start = m.group(2), m.start(2)
    lo = 0
    if match_span and match_span[0] < start + len(text) and match_span[1] > start:
        mid = (max(match_span[0], start) + min(match_span[1], start + len(text))) // 2 - start
        lo = min(max(0, mid - cap // 2), len(text) - cap)
    hi = lo + cap
    return m.group(1) + (MARK if lo else "") + text[lo:hi] + (MARK if hi < len(text) else "")
