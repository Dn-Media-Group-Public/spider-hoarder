"""One command to the emulator console (needs the auth token from the emulator's home, which only root can read:
`adb emu` runs as the svc uid and gets a trimmed command set). Prints the reply, exits 1 on KO.
usage: console.py <command...>   env: CONSOLE_PORT (5554), CONSOLE_TOKEN_FILE, CONSOLE_TIMEOUT (300)"""
import os
import socket
import sys


def run(cmd, port=5554, token_file="/home/emu/.emulator_console_auth_token", timeout=300.0):
    token = open(token_file).read().strip()
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    f = s.makefile("rw", newline="")

    def send(line):
        f.write(line + "\r\n")
        f.flush()

    def reply():
        out = []
        for line in f:
            line = line.rstrip("\r\n")
            if line in ("OK", "OK:") or line.startswith("OK"):
                return True, out
            if line.startswith("KO"):
                out.append(line)
                return False, out
            out.append(line)
        return False, out

    reply()  # banner, ends with the authentication prompt text and OK
    send("auth " + token)
    ok, out = reply()
    if not ok:
        return False, out
    send(cmd)
    ok, out = reply()
    try:
        send("quit")
    except OSError:
        pass
    s.close()
    return ok, out


if __name__ == "__main__":
    ok, out = run(" ".join(sys.argv[1:]), int(os.environ.get("CONSOLE_PORT", "5554")),
                  os.environ.get("CONSOLE_TOKEN_FILE", "/home/emu/.emulator_console_auth_token"),
                  float(os.environ.get("CONSOLE_TIMEOUT", "300")))
    print("\n".join(out))
    sys.exit(0 if ok else 1)
