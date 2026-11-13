import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import egress_rules  # noqa: E402
import start  # noqa: E402


def test_rules():
    flat = [" ".join(c) for c in egress_rules.rules(13, ["127.0.0.11", "10.1.2.3"], [("10.9.9.9", 8333)])]
    own = "-m owner --uid-owner 13"
    for ns in ("127.0.0.11", "10.1.2.3"):
        for p in ("udp", "tcp"):
            assert f"iptables -A OUTPUT {own} -d {ns} -p {p} --dport 53 -j ACCEPT" in flat
    assert f"iptables -A OUTPUT {own} -o lo -j ACCEPT" in flat and f"ip6tables -A OUTPUT {own} -o lo -j ACCEPT" in flat
    allow = flat.index(f"iptables -A OUTPUT {own} -d 10.9.9.9 -p tcp --dport 8333 -j ACCEPT")  # exactly that address and port ...
    rejects = [flat.index(f"iptables -A OUTPUT {own} -d {n} -j REJECT") for n in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "127.0.0.0/8")]
    assert allow < min(rejects)  # ... before the rejects, which are not weakened
    assert max(rejects) < flat.index(f"iptables -A OUTPUT {own} -j ACCEPT") < flat.index(f"ip6tables -A OUTPUT {own} -j REJECT")
    assert not any("-d 10.0.0.0/8 -j ACCEPT" in f for f in flat)
    assert flat[0] == "iptables -F OUTPUT" and not [f for f in egress_rules.rules(13, [], []) if "--dport 8333" in " ".join(f)]


def test_passwd_line_is_apr1():
    import subprocess
    line = start.passwd_line("t0k")
    user, h = line.split(":", 1)
    salt = h.split("$")[2]
    again = subprocess.run(["openssl", "passwd", "-apr1", "-salt", salt, "-stdin"], input=b"t0k", capture_output=True).stdout.decode().strip()
    assert user == "parse" and h == again and "t0k" not in line
