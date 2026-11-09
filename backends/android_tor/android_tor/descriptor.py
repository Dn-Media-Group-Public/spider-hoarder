"""Descriptor of android_tor: plain data and stdlib only (it is loaded by deploy.sh in the deploy host's virtualenv, which has no
gateway dependencies). File paths are relative to this package; family Dockerfiles are found with importlib.resources.
See docs/architecture/backend-plugins.md."""

from importlib import resources
from types import SimpleNamespace


def _family(sub):
    return str(resources.files("spider_hoarder_android_sdk") / sub)


def _env(environ):
    """Slot total of this backend (gateway replicas x slots per replica) and each wrapper's slice of it (one wrapper per KVM node)."""
    replicas = int(environ.get("ANDROID_TOR_REPLICAS") or 1)
    total = replicas * int(environ.get("ANDROID_SLOTS_PER_REPLICA") or 2)
    nodes = int(environ.get("ANDROID_KVM_NODES") or 1)
    return {"ANDROID_TOR_SLOTS": str(total), "ANDROID_TOR_SLOTS_PER_WRAPPER": str(-(-total // nodes))}


descriptor = SimpleNamespace(
    name="android_tor",
    title='Android emulator (Tor)',
    description=(
        'A real Android emulator (Google Play image), one private device per conversation, wiped when the session ends; '
        "all its TCP traffic and DNS goes through Tor (no UDP/QUIC), so apps do not see this server's IP. Slow; many "
        'services block Tor exits, and Google sign-in / Play Store are often refused.'),
    compose="docker-compose.yml",
    builds=[
        ("spider-hoarder-android-base:local", _family("image/Dockerfile")),
        ("spider-hoarder-android-slot:local", _family("slot/Dockerfile")),   # ARGs of its Dockerfile are fed from the environment
        ("spider-hoarder-android-wrapper:local", _family("wrapper/Dockerfile")),
        ("spider-hoarder-android-tor-daemon:local", "daemon/Dockerfile"),
        ("spider-hoarder-android-tor:local", "image/Dockerfile"),
    ],
    services=['android-tor-gateway', 'android-tor-wrapper', 'android-tor-daemon'],
    preflight=_family("preflight.sh"),
    post_build=None,   # the golden snapshot is made by each wrapper on its own node
    pinned=[],   # the wrapper is placed by PIN_KVM
    env=_env,
    runtime="android_tor.runtime:driver",
)
