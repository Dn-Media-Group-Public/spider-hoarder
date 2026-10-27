"""Per-session SOCKS5 relay: Chromium -> here -> upstream SOCKS5 (Tor). Refuses
CONNECTs to blocklisted (cookie-consent vendor) hostnames; everything else is
relayed untouched with the hostname passed on, so Tor still resolves names.
Usage: filter_proxy.py LISTEN_PORT UPSTREAM_HOST UPSTREAM_PORT  (stdlib + cmp_rules only)"""

import asyncio
import os
import sys

import cmp_rules

BLOCKED = frozenset(cmp_rules.hosts())
ALLOW_SUFFIX = os.environ.get("ALLOW_SUFFIX", "")  # i2p: ".i2p" = refuse every other name and all raw IPs


def is_allowed(host: str, atyp: int) -> bool:
    return not ALLOW_SUFFIX or (atyp == 3 and host.lower().rstrip(".").endswith(ALLOW_SUFFIX))


def is_blocked(host: str, blocked=BLOCKED) -> bool:
    parts = host.lower().rstrip(".").split(".")
    return any(".".join(parts[i:]) in blocked for i in range(len(parts)))


async def _pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except OSError:
        pass
    finally:
        w.close()


async def handle(cr, cw, up_host, up_port):
    try:
        ver, n = await cr.readexactly(2)
        await cr.readexactly(n)
        cw.write(b"\x05\x00")  # no auth
        ver, cmd, _, atyp = await cr.readexactly(4)
        if atyp == 1:
            raw = await cr.readexactly(4); host = ".".join(map(str, raw))
        elif atyp == 3:
            ln = (await cr.readexactly(1))[0]; raw = bytes([ln]) + await cr.readexactly(ln); host = raw[1:].decode()
        elif atyp == 4:
            raw = await cr.readexactly(16); host = raw.hex()
        else:
            return
        port = await cr.readexactly(2)
        if cmd != 1 or (atyp == 3 and is_blocked(host)) or not is_allowed(host, atyp):
            cw.write(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")  # not allowed
            return
        ur, uw = await asyncio.open_connection(up_host, up_port)
        uw.write(b"\x05\x01\x00")
        if await ur.readexactly(2) != b"\x05\x00":
            return
        uw.write(bytes([5, 1, 0, atyp]) + raw + port)
        head = await ur.readexactly(4)
        if head[3] == 3:
            ln = await ur.readexactly(1)
            tail = ln + await ur.readexactly(ln[0] + 2)
        else:
            tail = await ur.readexactly(6 if head[3] == 1 else 18)
        cw.write(head + tail)
        if head[1] != 0:
            return
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))
    except (asyncio.IncompleteReadError, OSError):
        pass
    finally:
        cw.close()


async def main(listen, up_host, up_port):
    srv = await asyncio.start_server(lambda r, w: handle(r, w, up_host, up_port), "127.0.0.1", listen)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
