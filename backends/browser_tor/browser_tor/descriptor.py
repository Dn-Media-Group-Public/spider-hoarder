"""Descriptor of browser_tor: plain data and stdlib only (it is loaded by deploy.sh in the deploy host's virtualenv, which has no
gateway dependencies). File paths are relative to this package; family build contexts are found with importlib.resources.
See docs/architecture/backend-plugins.md."""

from importlib import resources
from types import SimpleNamespace


def _family(sub):
    return str(resources.files("spider_hoarder_browser_sdk") / sub)


descriptor = SimpleNamespace(
    name="browser_tor",
    title='Tor browser',
    description=(
        'Isolated browser sessions whose traffic goes through the Tor network only, so the destination does not see '
        "this server's IP. For .onion sites and for research where the request origin must not be traceable. Slower; "
        'many sites show CAPTCHAs or block Tor exits.'),
    compose="docker-compose.yml",
    builds=[
        ("spider-hoarder-browser-base:local", _family("image")),
        ("spider-hoarder-browser-tor-daemon:local", "daemon"),
        ("spider-hoarder-browser-tor:local", "image"),
    ],
    services=['browser-tor-gateway', 'browser-tor-daemon'],
    preflight=None,
    post_build=None,
    env=None,
    runtime="browser_tor.runtime:driver",
)
