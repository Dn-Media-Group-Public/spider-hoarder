"""Descriptor of browser_i2p: plain data and stdlib only (it is loaded by deploy.sh in the deploy host's virtualenv, which has no
gateway dependencies). File paths are relative to this package; family build contexts are found with importlib.resources.
See docs/architecture/backend-plugins.md."""

from importlib import resources
from types import SimpleNamespace


def _family(sub):
    return str(resources.files("spider_hoarder_browser_sdk") / sub)


descriptor = SimpleNamespace(
    name="browser_i2p",
    title='I2P browser',
    description=(
        'Isolated browser sessions that reach only *.i2p addresses (eepsites, .b32.i2p), through an i2pd router; '
        'clearnet and .onion are not reachable. Slow: the first request on a fresh session can take a minute while '
        'tunnels are built, and eepsites are often offline.'),
    compose="docker-compose.yml",
    builds=[
        ("spider-hoarder-browser-base:local", _family("image")),
        ("spider-hoarder-browser-i2p-router:local", "router"),
        ("spider-hoarder-browser-i2p-seed:local", "seed"),
        ("spider-hoarder-browser-i2p:local", "image"),
    ],
    services=['browser-i2p-gateway', 'browser-i2p-router', 'browser-i2p-seed'],
    preflight=None,
    post_build=None,
    env=None,
    runtime="browser_i2p.runtime:driver",
)
