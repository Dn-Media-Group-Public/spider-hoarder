"""This replica's identity: the `rid` that ids and URLs carry so nginx can route to the replica that owns them."""

import os
import socket


def _own_rid() -> str:
    """This replica's IP on the network it shares with nginx, dashes for dots (ids and URLs carry it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((socket.gethostbyname("nginx"), 9))  # UDP connect: picks the route, sends nothing
        return s.getsockname()[0].replace(".", "-")


RID = os.environ.get("REPLICA_RID") or _own_rid()  # REPLICA_RID: tests / single-host runs without nginx
