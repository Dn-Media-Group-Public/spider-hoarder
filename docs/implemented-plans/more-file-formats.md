# Plan: more archive formats and binary-structured-data → JSON

Depends on [Network request jobs](../implemented-plans/network-request-jobs.md) (must be implemented first): this plan builds on its `unpack_zip`
generator, `_store_one` / `_store_tree`, and the `MAX_PARSE_BYTES` cap. See also [Jobs, storage and links](../architecture/jobs-and-storage.md).

## Problem / Goal

1. Only `.zip` is unpacked (`extract.is_zip`, `extract.unpack_zip`). tgz, tbz, txz, plain tar, 7z, rar and single-file gz/bz2/xz
   are stored as opaque blobs.
2. Binary structured formats (msgpack, CBOR, BSON, binary plist, protobuf) are not readable by the agent; markitdown has no
   converter for them. API responses and app data are often in these formats.

Goal: unpack the common archives through the same tree rule as zip, and turn the binary structured formats into JSON in `parsed/`.

## Design

### Archives

- `extract.py` gets one generator `unpack_archive(fileobj, name, content_type) -> Iterator[(relpath, fileobj)]` replacing
  `unpack_zip`; `is_zip` becomes `archive_kind(content_type, filename) -> str | None`. Detection by extension / content-type only.

| Kind | Extensions | Library |
|---|---|---|
| zip | `.zip` | stdlib `zipfile` (unchanged) |
| tar | `.tar .tgz .tar.gz .tbz .tbz2 .tar.bz2 .txz .tar.xz` | stdlib `tarfile`, mode `r\|*` (streaming, auto-compression) |
| 7z | `.7z` | `py7zr` |
| rar | `.rar` | `rarfile` (needs the `unrar`/`unar`/`bsdtar` binary in the image) |
| single-file compressed | `.gz .bz2 .xz` (not tar) | stdlib `gzip` / `bz2` / `lzma`; yields one member named without the suffix |

- **No recursion:** an archive inside an archive is stored as a plain file under `original/` (and is not unpacked).
- **Safety (all kinds):** keep the zip-slip name check (no absolute paths, no `..`). For tar/7z/rar only regular files are
  extracted; symlinks, hardlinks, devices, FIFOs are skipped and noted in the result (`"<name> skipped (symlink)"`), never followed.
  Names are normalised, member count and running uncompressed bytes are limited exactly as the network-request-jobs plan does for zip.
- **Limits:** `MAX_ZIP_BYTES` / `MAX_ZIP_MEMBERS` are renamed `MAX_ARCHIVE_BYTES` / `MAX_ARCHIVE_MEMBERS`; the old names are read
  as fallbacks (never deployed, so only a convenience). Applied to every archive kind.
- Members then flow through `_store_tree` → `_store_one` like zip members do (one parse rule).

### Binary structured data → JSON

New `extract.binary_to_json(data, kind) -> str`; `_store_one` calls it for the kinds below and writes `parsed/<path>.json`
(pretty-printed, `indent=2`). `original/<path>` is untouched and `.json` originals are **not** pretty-printed or otherwise parsed.

| Kind | Detected by | Decoder |
|---|---|---|
| msgpack | `.msgpack .mp`, `application/msgpack`, `application/x-msgpack` | `msgpack` |
| CBOR | `.cbor`, `application/cbor` | `cbor2` |
| BSON | `.bson`, `application/bson` | `bson` (implementation to be verified at build time to avoid the pymongo/`bson` name clash; document the choice) |
| plist (binary) | `.plist`, `bplist00` magic, `application/x-plist` | stdlib `plistlib` |
| protobuf | `.pb`, `application/x-protobuf`, `application/protobuf`, `application/grpc-web*` | `blackboxprotobuf` |

- **Extension / content-type only**, except plist which is also sniffed by its `bplist00` magic. No content sniffing for the others
  (arbitrary bytes often decode as msgpack/protobuf).
- **Non-JSON values:** `bytes` → `{"$base64": "..."}`; datetimes → ISO 8601 string; `NaN`/`Inf` → string; sets → lists;
  non-string map keys → their `str()`. One `default=` hook, shared by all decoders.
- **Protobuf, phase 1 (this plan): schema-less.** `blackboxprotobuf.decode_message(data)` guesses types (string vs bytes vs
  nested message, signed vs unsigned); the output JSON has the field numbers as keys and the guessed type kept alongside, e.g.
  `{"message": {...}, "typedef": {...}}` so a later tool could re-encode or refine. gRPC-web bodies: strip the 5-byte frame header(s)
  (flag + big-endian length) and decode each message; trailer frames (flag bit 7) are decoded as text headers into the same array.
- **Protobuf, phase 2 (separate plan, not designed here):** a schema (`.proto` or compiled `FileDescriptorSet`) supplied as an
  extra job file would name the fields. Automatic discovery (JS descriptors in captured bundles, gRPC reflection) is out of scope.
- Failure to decode never fails the job: `parsed/` is skipped for that file and the error is noted, as for other parse errors.

### Why not more
YAGNI: Avro, Parquet, Thrift, FlatBuffers need schemas or heavy dependencies; not requested.

## Implementation order

1. Wait for / implement [network-request-jobs](../implemented-plans/network-request-jobs.md).
2. `requirements.txt` + image: `msgpack cbor2 bson py7zr rarfile blackboxprotobuf`; `unrar` (or `unar`) system package.
3. `extract.py`: `archive_kind`, `unpack_archive` (tar, 7z, rar, single-file-compressed alongside zip) with the safety rules; tests.
4. `app.py`: `_store_and_parse` uses `archive_kind` / `unpack_archive`; rename env vars with fallbacks.
5. `extract.py`: `binary_to_json` and the JSON default hook; `app.py` `_store_one` writes `parsed/<path>.json` for the kinds above.
6. Docs; move the plan.

## Files to change

| File | Change |
|---|---|
| `mcp-research/requirements.txt`, `Dockerfile` | new python deps; `unrar`/`unar` |
| `mcp-research/extract.py` | `archive_kind`, `unpack_archive` (replaces `is_zip`/`unpack_zip`), `binary_to_json`, grpc-web de-framing |
| `mcp-research/app.py` | archive dispatch in `_store_and_parse`; binary → `parsed/*.json` in `_store_one`; `MAX_ARCHIVE_*` env |
| `mcp-research/test_gateway.py` | tgz/tbz/txz/7z/rar/gz members unpacked and parsed; no recursion; limits per kind; each binary format → expected JSON; protobuf type guess; grpc-web framing; undecodable file noted, job not failed; `.json` original untouched |
| `mcp-research/test_security.py` | tar: absolute/`..` names, symlink, hardlink, device, FIFO skipped; 7z/rar slip names; bomb hits count/byte limit |

## Docs to update

- `docs/architecture/jobs-and-storage.md`: supported archives, safety rules, `MAX_ARCHIVE_*`, binary → `parsed/*.json`, detection rules.
  Replace "Uploaded zips are unpacked…" line.
- `docs/api/tools.md`: wherever zip upload/ingest is described, say "archives"; mention `parsed/*.json` for binary formats.
- `docs/implemented-plans/index.md`: link this plan once moved; each updated section gets
  `See also: [Plan: more file formats](../implemented-plans/more-file-formats.md)`.
