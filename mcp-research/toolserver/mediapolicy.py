"""No-media session policy, shared by the toolserver (browser layers A + D), fetch.py and the gateway.
Pure stdlib. See docs/implemented-plans/no-media-sessions.md."""

import re
from dataclasses import dataclass, field

# layer A: Playwright resource types that are never requested when media=false
BLOCKED_RESOURCE_TYPES = ["image", "media", "object", "imageset", "font"]
# layer D / fetch.py: with media=false only these content types pass (plus the user's `allow`)
TEXT_ALLOWED = ["text/*", "application/json", "application/xml", "application/*+json", "application/*+xml",
                "application/xhtml+xml"]
_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/(\*|[a-z0-9][a-z0-9.+-]*)$")


def _norm(header: str | None) -> str:
    return (header or "").split(";")[0].strip().lower()


def _match(pattern: str, ctype: str) -> bool:
    """Exact type, `type/*`, or `application/*+json` style suffix wildcard."""
    if pattern == ctype:
        return True
    head, _, tail = pattern.partition("/")
    if not ctype or tail != "*" and not tail.startswith("*+"):
        return False
    c_head, _, c_tail = ctype.partition("/")
    return head == c_head and (tail == "*" or c_tail.endswith(tail[1:]))


def _types(name: str, v) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and _TYPE.match(_norm(x)) for x in v):
        raise ValueError(f"{name} must be a list of MIME types such as 'image/svg+xml' or 'image/*'")
    return [_norm(x) for x in v]


@dataclass(frozen=True)
class Policy:
    media: bool
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    @classmethod
    def make(cls, egress: str, media=None, allow=None, deny=None) -> "Policy":
        """Validated policy; `media` defaults to blocked on tor / i2p, allowed on clear."""
        if media is not None and not isinstance(media, bool):
            raise ValueError("media must be true or false")
        return cls(egress == "clear" if media is None else media, _types("allow", allow), _types("deny", deny))

    def to_json(self) -> dict:
        return {"media": self.media, "allow": self.allow, "deny": self.deny}

    @classmethod
    def from_json(cls, d: dict) -> "Policy":
        return cls(d["media"], _types("allow", d["allow"]), _types("deny", d["deny"]))

    @property
    def active(self) -> bool:
        """Anything to enforce at all?"""
        return not self.media or bool(self.deny)

    def resource_blocked(self, resource_type: str) -> bool:
        return not self.media and resource_type in BLOCKED_RESOURCE_TYPES

    def content_type_blocked(self, header: str | None) -> bool:
        """Precedence: deny, allow, default text allowlist, block. media=true blocks only `deny`."""
        ctype = _norm(header)
        if any(_match(p, ctype) for p in self.deny):
            return True
        if self.media or any(_match(p, ctype) for p in self.allow + TEXT_ALLOWED):
            return False
        return True  # includes a missing header

    def summary(self, egress: str, opened: bool) -> dict:
        return {"egress": egress, "opened": opened, "media": self.media,
                "blocked_resource_types": [] if self.media else BLOCKED_RESOURCE_TYPES,
                "allowed_content_types": ["*/*"] if self.media else TEXT_ALLOWED + self.allow,
                "denied_content_types": self.deny}


def blocked_message(header: str | None) -> str:
    return f"blocked: media disabled for this session ({_norm(header) or 'no content type'})"
