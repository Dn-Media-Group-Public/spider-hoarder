# Bug report: name resolution fails inside containers under load; concurrent cold-session fetches of dn.no fail

Status: **open, root cause not established.** Found on 2026-11-13 while testing the parser service live
([Plan: parser wrapper](../implemented-plans/parser-wrapper.md)). Nothing here is fixed. This report says what was seen, what was measured, what is
only a hypothesis, and what to do next.

## Symptom

1. One tool call out of five concurrent `ingest_page` calls for `https://www.dn.no/` returned a tool error (the gateway's one-line report of an
   unexpected exception inside a tool):

   ```
   gaierror: [Errno -2] Name or service not known
   ```

   The other four returned complete jobs. Setup: `browser-clear` gateway, one browser session, the parser pool limited to one slot
   (`PARSE_POOL_SIZE=1`), all five calls started together. A later script printed the results only after all five had finished, so it is not known
   when during the run the failure happened.

2. Later, on a daemon I believed to be quiet, the same five-way test failed **completely** three times in a row:
   - run 1 and run 2: all five calls returned `Page.goto: Timeout 30000ms exceeded` (the browser's own 30 s navigation limit, after ~92 s overall);
   - run 3: all five returned `gaierror: [Errno -3] Temporary failure in name resolution` after ~9 s.

   Right after these runs `/proc/loadavg` read 116 / 134 / 79 (1, 5, 15 minutes) on a 27-core VM, while `vmstat` a minute later showed 99% idle CPU and no
   D-state processes.

3. A last measurement run (five concurrent ingests with load sampling) hit the 200 s timeout with no result.

## Where it does *not* come from

- **Not the parser.** The error occurs before parsing: `ingest_page` fails in the gateway's fetch, and no parser request is made for a failed call.
  The parser client turns transport problems into a note (`not parsed (...)`) and never raises, so it cannot produce a raw `gaierror` tool error.
- **Not storage.** s3fs/aiobotocore/boto3 wrap DNS errors in their own exception types; a bare `socket.gaierror` is not what they raise.

## The one resolution point in the gateway

`backends/browser-sdk/spider_hoarder_browser_sdk/netguard.py` `check_host()` calls `socket.getaddrinfo(host, None, type=SOCK_STREAM)` for every hop of
the preflight request (`fetch.content_type` → `guarded_request`); dn.no redirects, so there is more than one hop per page. This is existing code,
unchanged by the parser work. A `gaierror` from there propagates out of the tool. In containers, resolution goes to Docker's embedded resolver:

```
nameserver 127.0.0.11        # served by dockerd
options ndots:0
# ExtServers: [10.0.2.3]     # the VM's upstream resolver
```

## What was measured

All measurements: `getaddrinfo` for `www.dn.no` / `dn.no` from a thread pool, run inside the gateway container as uid 1000 (same network namespace
and resolver as the gateway), compared with the host.

| Condition | Result |
|---|---|
| Host (direct to `10.0.2.3`), 60 workers × 300 lookups | 2.3 s, 300 ok |
| Container, daemon idle, 60 workers × 200 lookups | 4.7 s, 200 ok; recovers instantly, single lookups 0.0 s |
| Container, 8–40 workers × 80 lookups, shortly after a heavy log scan | all ok, but slow: p50 160–810 ms, max up to 1.6 s |
| Container, sequential 300 lookups, same period | 14.2 s (about 50 ms each) |
| Container, 60 workers × 300 lookups, same period | **all 300 failed `gaierror -3` after 41.9 s** |
| Container, while `dockerd` streams two large `docker service logs` | 5 workers: 4.8 s / 200 ok; 20 workers: 12.2 s / 200 ok; **60 workers: all 200 failed `gaierror -3` after 33.9 s**; a follow-up run was still slow |
| Container, 1–3 s after the log streams ended | 20 workers × 100: 0.7 s, 3.8 s, 3.2 s, all ok |

Conclusions that follow from the numbers:
- The upstream resolver is fast and healthy. The slowness and the collapse are in the path through Docker's embedded resolver inside the container.
- The collapse is **reproducible when `dockerd` is busy** (here: streaming service logs). It is not a fixed concurrency limit, because the same 60-worker
  burst succeeded when the daemon was idle.
- The failure code reproduced is **-3** (`EAI_AGAIN`, temporary failure), not the **-2** (`EAI_NONAME`) seen in the first incident. **-2 has never been
  reproduced.** The first incident is therefore not shown to be this mechanism.

## Hypotheses (not proven)

1. **`dockerd` was loaded by my own diagnostics during the first incident.** While the first concurrent test ran, I was issuing many `docker exec`
   calls in parallel (a loop over `/proc` entries to find the gateway's pid for `py-spy`) and `docker service logs` reads. That matches the
   reproduced mechanism, but it does not explain error -2.
2. **Cold-session thundering herd.** The failing script (`busy.py`) starts five `ingest_page` calls on a *fresh* browser session with no earlier
   navigation; each preflights over several redirect hops and loads a full news front page (about 900 KB, many third-party requests) in the same
   Chromium. The e2e script's six concurrent ingests passed three times (about 14 s) but run *after* a navigate has warmed the session. Page loads
   from the browser also resolve through the same `127.0.0.11` (the session uid firewall allows it), so five heavy page loads plus preflights could
   saturate the embedded resolver or the VM.
3. **Host pressure.** Load average above 100 with an idle CPU a minute later means many threads were blocked (I/O or scheduling). The root
   filesystem is **97% full (14 GB free)**, which was not examined as a factor.

## Evidence that does not fit a single cause

- Sequence of results for the five-way test: 4 ok + 1 `gaierror -2` (first incident) → 5 × goto timeout → 5 × goto timeout → 5 × `gaierror -3`.
  Different errors across runs, mostly all-or-nothing, so the failure behaves like a shared resource (resolver or CPU/IO) giving way, not like a
  per-call race.
- The full live e2e (`tests/live/e2e_parser.py`, 39 checks, including six concurrent ingests of dn.no) passed on the same deployment before and after
  these runs (last pass: 39/39 on the final deploy).

## Impact

- A tool call (`ingest_page`, and any other path that resolves in the gateway) can fail with a raw `gaierror` when the host or `dockerd` is loaded.
  The message is not actionable for the MCP client.
- Anything that resolves names through Docker's embedded resolver is exposed: the gateway's preflight, Chromium in every session, and, new with the
  parser service, the wrapper (`blob-sidecar`, `seaweedfs`/bucket host through the proxy) and the proxy itself (squid resolves destinations).
  A DNS failure there fails a parse (recorded as `not parsed (...)`, original kept), never a job.
- The parser pool uids resolve nothing themselves (no route out); this affects the parser only through the wrapper and the proxy.

## Not yet done

- The gateway's traceback for the first failure was not captured: the gateway task was replaced by later deploys and its logs went with it.
- No load trace for the concurrent run: my sampler raced with its own cleanup and captured two samples (load 64.9 and 67.7, threads 2408 → 3486).
- Not run: the five-way test with the parser disabled (`PARSE_URL` unset), the five-way test on a warm session, a single `ingest_page` repeated
  serially on the idle VM, the root disk usage during a run.
- No check whether `ndots:0`, the resolver's `ExtServers`, or the VM's slirp-style `10.0.2.3` upstream (the VM's NAT resolver) matter.

## Suggested next steps

1. Re-run the five-way test with **nothing else touching Docker**, sampling `/proc/loadavg`, thread count, `ps` D-state processes and `iostat`
   every second to a file; capture the gateway's tool traceback (`docker service logs`, read once, afterwards).
2. Repeat it (a) with `PARSE_URL` unset, (b) on a warm session, (c) with concurrency 2 and 3 to find where it starts failing.
3. Free disk space on the root filesystem and repeat, to rule that out.
4. If the embedded resolver is the bottleneck, options to evaluate (none decided): a resolver cache or retry around `check_host` in the gateway
   (`getaddrinfo` retried on `EAI_AGAIN`/`EAI_NONAME` with a short backoff), turning a `gaierror` into a clear tool error ("name resolution failed,
   retry"), limiting concurrent `ingest_page` fetches per gateway, and a per-service `dns:` setting pointing at an external resolver where the
   platform allows it.

## Reproduction

Resolver collapse (inside the running gateway container, needs write access to `/tmp` there):

```
# lookups.py: ThreadPoolExecutor(N) over ["www.dn.no", "dn.no"] * 150, counting socket.gaierror errno
docker cp lookups.py <gateway container>:/tmp/ && docker exec -u 1000 <gateway container> python /tmp/lookups.py
# in another shell, load dockerd:
docker service logs --no-trunc spider-hoarder_browser-clear-gateway > /dev/null
```

Concurrent cold-session fetch: open a session with `open_session`, then call `ingest_page` for `https://www.dn.no/` five times at once with
`asyncio.gather` (no earlier `browser_navigate`).
