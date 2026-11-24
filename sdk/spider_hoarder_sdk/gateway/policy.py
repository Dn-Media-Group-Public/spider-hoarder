"""Policy levels (admin env, project, session) for the media policy and the external parse features: settings, bounds, validation and
resolution. Pure stdlib with no package imports (scripts/spider_hoarder.py loads this file directly). See docs/implemented-plans/policy-levels.md.

A level is a dict {setting: value | ASK}; a missing key means "inherit" at project and session level and "the admin default" at admin level.
ASK is outside the narrowing order: the bound for a level is the nearest non-ASK value above it, else the setting's whole domain."""

import re

ASK = "ASK"
FEATURES = ("ocr", "asr", "caption")
SETTINGS = ("media", "allow", "deny", *FEATURES)
# the parser features a chain fills, in order: primary, then fallback. The parser has no OCR fallback (local Tesseract is the first step
# there), so an OCR chain is one entry; lifting the limits means lengthening these tuples (and the parser's `features`).
FEATURE_KEYS = {"ocr": ("ocr",), "asr": ("asr", "asr-fallback"), "caption": ("caption", "caption-fallback")}
# what an unset admin variable means; media / allow / deny stay ASK ("the level below chooses", with the driver's suggestion as the default)
ADMIN_DEFAULT = {"media": ASK, "allow": ASK, "deny": ASK, "ocr": [], "asr": [], "caption": []}
LEVELS = ("admin", "project", "session")
_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/(\*|[a-z0-9][a-z0-9.+-]*)$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def normalize(setting: str, v, profiles: dict | None = None):
    """The validated, canonical form of a value of `setting` (ASK stays ASK); ValueError on a malformed one. `profiles` ({feature: names}) is
    the deployment's table; None = unknown here (another backend's section): profile names are checked for shape only."""
    if v == ASK:
        return ASK
    if setting == "media":
        if not isinstance(v, bool):
            raise ValueError("media must be true, false or ASK")
        return v
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{setting} must be a list or ASK")
    if setting in ("allow", "deny"):
        v = [x.split(";")[0].strip().lower() for x in v]
        if not all(_TYPE.match(x) for x in v):
            raise ValueError(f"{setting} must be a list of MIME types such as 'image/svg+xml' or 'image/*'")
        return list(dict.fromkeys(v))
    if setting not in FEATURES:
        raise ValueError(f"unknown setting {setting!r}")
    if len(v) > len(FEATURE_KEYS[setting]) or len(set(v)) != len(v):
        raise ValueError(f"{setting} takes at most {len(FEATURE_KEYS[setting])} different profile(s)")
    for p in v:
        if not _NAME.match(p) or (profiles is not None and p not in profiles.get(setting, ())):
            raise ValueError(f"{setting}: unknown profile {p!r} (available: {', '.join(profiles.get(setting, ())) or 'none'})" if profiles is not None
                             else f"{setting}: invalid profile name")
    return v


def within(setting: str, v, bound) -> bool:
    """Is `v` (not ASK) inside `bound` (a value, or None = the whole domain)? The narrowing rules."""
    if bound is None:
        return True
    if setting == "media":
        return bound or v is False
    if setting == "deny":
        return set(bound) <= set(v)
    return set(v) <= set(bound)  # allow shrinks, a feature drops or reorders entries


def parse_env(setting: str, raw: str | None):
    """An admin variable's text: `true`/`false` (media), a comma-separated list (everything else) or ASK; unset or empty = the admin default."""
    raw = (raw or "").strip()
    if not raw:
        return ADMIN_DEFAULT[setting]
    if raw == ASK:
        return ASK
    if setting == "media":
        if raw.lower() not in ("true", "false"):
            raise ValueError("must be true, false or ASK")
        return raw.lower() == "true"
    return [x.strip() for x in raw.split(",") if x.strip()]


def admin_level(environ, profiles: dict, settings=SETTINGS) -> dict:
    """The admin level from `POLICY_<SETTING>` variables (the stack file maps `<BACKEND>_POLICY_<SETTING>` to these), validated like any level."""
    out = {}
    for s in settings:
        var = f"POLICY_{s.upper()}"
        try:
            out[s] = normalize(s, parse_env(s, environ.get(var)), profiles)
        except ValueError as e:
            raise ValueError(f"{var}: {e}")
    return out


def profile_names(environ) -> dict:
    """{feature: [names]}: the deployment's profile names, from `PROFILE_NAMES_<FEATURE>` (names only, never a token or key)."""
    return {f: [x for x in (environ.get(f"PROFILE_NAMES_{f.upper()}") or "").split(",") if x] for f in FEATURES}


def check(setting: str, v, bound, profiles: dict | None):
    """Validate a value a level is setting against `bound` = (value | None, source), e.g. resolve(levels above)[setting]'s: the canonical value,
    or ValueError naming the bound. Never clips."""
    v = normalize(setting, v, profiles)
    if v != ASK and not within(setting, v, bound[0]):
        raise ValueError(f"{setting}={v!r} is outside what the {bound[1]} policy allows ({bound[0]!r})")
    return v


def bound_of(resolved: dict, setting: str) -> tuple:
    return resolved[setting]["bound"], resolved[setting]["bound_source"]


def check_level(level, resolved_above: dict, profiles: dict | None, settings=SETTINGS) -> dict:
    """A whole project / session level (a backend's part of `access.json`'s policy): every setting checked against `resolve(levels above)`."""
    if not isinstance(level, dict):
        raise ValueError("a policy level is an object {setting: value}")
    if unknown := sorted(set(level) - set(SETTINGS)):
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    return {s: check(s, v, bound_of(resolved_above, s), profiles) for s, v in level.items() if s in settings}


def resolve(levels: list[tuple[str, dict]], profiles: dict | None = None, settings=SETTINGS) -> dict:
    """{setting: {value, source, bound, bound_source}} going down `levels` (admin, project, session; the last two may be {}). `bound` is what a
    level below the last could choose within: the nearest non-ASK value (None = the whole domain). A stored value outside its bound (a ceiling
    tightened since) or naming a profile that is gone is dropped, not applied. value ASK = unresolved."""
    out = {}
    for s in settings:
        eff, src, b = ASK, "admin", (None, "domain")
        for source, d in levels:
            v = d.get(s, ADMIN_DEFAULT[s] if source == "admin" else None)
            if v is not None:
                try:
                    v = check(s, v, b, profiles)
                except ValueError:
                    v = None  # stale: dropped
            if v is not None:
                eff, src = v, source
                if v != ASK:
                    b = (v, source)
        out[s] = {"value": eff, "source": src, "bound": b[0], "bound_source": b[1]}
    return out


def with_defaults(resolved: dict, defaults: dict) -> dict:
    """ASK settings that have a default (media: the driver's suggestion, clipped to the bound; allow / deny: empty) take it, source "default"."""
    out = {}
    for s, r in resolved.items():
        if r["value"] == ASK and s in defaults:
            d = defaults[s]
            if s == "media" and r["bound"] is False:
                d = False
            r = {**r, "value": d, "source": "default"}
        out[s] = r
    return out


def unresolved(resolved: dict) -> dict:
    """{setting: bound} still ASK: what open_session must refuse over."""
    return {s: r["bound"] for s, r in resolved.items() if r["value"] == ASK}


def view(resolved: dict) -> dict:
    """What tools show: each setting's value and source, and the unresolved ones with their bounds."""
    out = {"policy": {s: {"value": r["value"], "source": r["source"]} for s, r in resolved.items() if r["value"] != ASK}}
    if u := unresolved(resolved):
        out["ask"] = {s: ("any" if b is None else b) for s, b in u.items()}
    return out


def chains(resolved: dict) -> dict:
    """{feature: chain} of the resolved features (unresolved ones left out)."""
    return {f: r["value"] for f, r in resolved.items() if f in FEATURES and r["value"] != ASK}


def features(chain_by_feature: dict) -> dict:
    """The parser's `features` for resolved chains: {ocr: p, asr: p, asr-fallback: p2, ...}; None when nothing is on."""
    return {k: p for f, c in chain_by_feature.items() for k, p in zip(FEATURE_KEYS[f], c)} or None


def notes(resolved: dict, profiles: dict) -> list[str]:
    """One audit note per feature the deployment has profiles for: `ocr: lan (admin)`, `caption: off (session policy)`, `asr: off (unresolved)`."""
    out = []
    for f in FEATURES:
        if not profiles.get(f):
            continue
        r = resolved[f]
        out.append(f"{f}: off (unresolved)" if r["value"] == ASK
                   else f"{f}: {','.join(r['value'])} ({r['source']})" if r["value"] else f"{f}: off ({r['source']} policy)")
    return out
