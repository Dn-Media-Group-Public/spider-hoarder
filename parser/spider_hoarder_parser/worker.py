"""The parser process (run by the wrapper as a pool uid): `python -I -u -m spider_hoarder_parser.worker <jobdir>`.
<jobdir>/in/original/…  and  in/job.json  are the input; artefacts are written under <jobdir>/out/ and announced to the wrapper,
one JSON record per line, on a private dup of the stdout pipe. Every tool started from here writes to stderr instead."""

import os

PROTO = os.dup(1)  # before any import: nothing imported or spawned later can write to the protocol
os.dup2(2, 1)
os.set_inheritable(PROTO, False)

import json  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402

from spider_hoarder_parser import parse  # noqa: E402


class PipeSink(parse.Sink):
    def __init__(self, out: str):
        self.out, self.sent = out, set()  # a path is announced once: a second write could be deleted by the wrapper after its upload

    def _announce(self, rec: dict) -> None:
        line = json.dumps(rec, separators=(",", ":")).encode() + b"\n"
        assert len(line) <= 4096, "record too long"  # <= PIPE_BUF: one write is atomic
        os.write(PROTO, line)

    def _dest(self, rel: str) -> str:
        dest = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return dest

    def put_bytes(self, rel, data, ctype):
        if rel in self.sent:
            return
        self.sent.add(rel)
        with open(self._dest(rel), "wb") as f:
            f.write(data)
        self._announce({"path": rel, "content_type": ctype})

    def put_file(self, rel, src, ctype):
        if rel in self.sent:
            return os.unlink(src)
        self.sent.add(rel)
        shutil.copyfile(src, self._dest(rel))  # a copy, not a rename: the new file takes out/'s group, which is how the wrapper reads it
        os.unlink(src)
        self._announce({"path": rel, "content_type": ctype})

    def mark_text(self, rel):
        self._announce({"path": rel, "text": True})

    def note(self, text):
        self._announce({"note": text[:1000]})


def main(jobdir: str) -> None:
    job = json.load(open(os.path.join(jobdir, "in", "job.json")))
    work = os.path.join(jobdir, "work")
    os.chdir(work)  # here, as the pool uid: the wrapper itself cannot enter the directory (Popen's cwd= and preexec_fn run before the uid switch)
    parse.parse_job(PipeSink(os.path.join(jobdir, "out")), os.path.join(jobdir, "in"), job, work)


if __name__ == "__main__":
    main(sys.argv[1])
