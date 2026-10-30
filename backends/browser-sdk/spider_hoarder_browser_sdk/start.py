"""Container start of a browser backend (as root, under tini): the session-uid firewall, then sessiond (root, narrow caps),
then the gateway (uid 1000, no caps). The gateway is restarted if it dies; sessiond on start and the gateway on start
both wipe every pool uid (crash recovery). `python -m spider_hoarder_browser_sdk.start`; needs $BACKEND, $GATEWAY_PORT."""

import os
import signal
import subprocess
import sys
import threading
import time

from spider_hoarder_browser_sdk import firewall

GW_COLLECT_GID = os.environ.get("GW_COLLECT_GID", "1500")


def watch_router(egress) -> None:
    """The rules pin the router's IP. If it changes (router task restarted), stop: Swarm restarts this replica and the
    rules are rebuilt. ponytail: sessions on the replica are lost, as on any restart."""
    while True:
        time.sleep(15)
        if egress.router_moved():
            print(f"{egress.router_host} moved, exiting", file=sys.stderr, flush=True)
            os.kill(os.getpid(), signal.SIGTERM)


def main() -> None:
    from spider_hoarder_sdk.gateway.driver import load
    egress = load().egress
    port = os.environ["GATEWAY_PORT"]
    router_ip = firewall.apply(egress)
    egress.router_pinned(router_ip)
    if egress.watch_router:
        threading.Thread(target=watch_router, args=(egress,), daemon=True).start()
    # no-new-privileges is set here (setpriv), since Swarm services cannot set security_opt.
    # sessiond: root, but only the caps it needs; NET_ADMIN is gone from here on
    subprocess.Popen(["setpriv", "--no-new-privs", "--bounding-set=-all,+setuid,+setgid,+chown,+kill,+dac_override,+fowner",
                      sys.executable, "-m", "spider_hoarder_browser_sdk.session_server.sessiond"])
    sock = os.environ.get("SESSIOND_SOCKET", "/run/sessiond.sock")
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.1)
    while True:  # gateway: uid 1000, no caps, restarted on exit
        subprocess.run(["setpriv", "--no-new-privs", "--reuid=1000", "--regid=1000", f"--groups={GW_COLLECT_GID}",
                        "--bounding-set=-all", "--inh-caps=-all",
                        "uvicorn", "spider_hoarder_sdk.gateway.app:app", "--host", "0.0.0.0", "--port", port])
        print("gateway exited, restarting", file=sys.stderr, flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
