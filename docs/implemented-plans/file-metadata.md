# Plan: file metadata extraction (YAML) alongside parsing

See also [Jobs, storage and links](../architecture/jobs-and-storage.md), [OCR hybrid](ocr-hybrid.md) (the other planned addition to the parse step; independent).

## Problem / Goal

Parsing (`parsed/`, `terse/`) captures a file's *content*. Its *metadata* (EXIF/GPS/camera, Office authors and edit history, GeoTIFF CRS and bounding
box, media streams, PDF save history) is lost or only partly shown by markitdown. Goal: for every stored file, write a `metadata/<path>.yaml`
next to the other artefacts, readable by the agent with `get_file_text` / `grep_file`.

## Design

### Layout

`metadata/<path>.yaml`, one per original, same nesting as `parsed/` (e.g. `metadata/photos/a.jpg.yaml`, archive members
`metadata/x.zip.d/a.jpg.yaml`). A file with no metadata gets no file. It is listed in the job's `files` like `terse/` (no change to `text`,
`text_path`, or any tool signature). Stored with content type `application/yaml`.

### Shape

Top-level key = **group**, value = mapping of tags. ExifTool's own tags come from `exiftool -json -g1` (nested by group: `EXIF`, `XMP-dc`,
`QuickTime`, `File`, ...). Our extensions are further groups beside them, named `SpiderHoarder-<Format>`, a prefix ExifTool will never emit;
their values may be arbitrarily nested (lists of revisions etc.).

```yaml
File: {FileType: DOCX, MIMEType: application/vnd.openxmlformats-officedocument.wordprocessingml.document}
XMP-dc: {Creator: Jane Doe}
SpiderHoarder-Office:
  Revisions:
    - {Author: Jane Doe, Date: "2024-03-01T10:12:00Z", Type: insert, Text: "..."}
  Comments: [...]
  RevisionCount: 14
```

Values are passed through as produced (no remapping to a custom schema). Non-YAML-safe values (bytes, NaN) become strings; binary tags
(thumbnails, ICC) are dropped by ExifTool's default (`-b` not used), not embedded.

### Extractors

| Extractor | Files | Group(s) | Tool |
|---|---|---|---|
| ExifTool | everything | ExifTool's own groups | `exiftool -json -g1 -struct` |
| Raster | GeoTIFF and other GDAL rasters | `SpiderHoarder-Raster`: `CRS` (EPSG if known, WKT), `BBox` (native), `BBoxWGS84`, `Resolution`, `Size`, `Bands`, `NoData`, `Tags` | `rasterio` |
| Vector | GPX, KML, GeoJSON, shapefile (zip), GeoPackage | `SpiderHoarder-Vector`: `CRS`, `BBox`, `BBoxWGS84`, `Layers` (name, geometry type, feature count) | OGR via `pyogrio`/`fiona` (the same GDAL as rasterio) |
| Office | docx / xlsx / pptx | `SpiderHoarder-Office`: `Revisions` (author, date, insert/delete, text), `Comments`, `LastModifiedBy`, `RevisionCount` | `zipfile` + `lxml` on the OOXML parts (size/ratio limits as for archives) |
| PDF | pdf | `SpiderHoarder-PDF`: XMP `History`, `IncrementalUpdates` (count of `%%EOF` / xref sections), `Producer`/`Creator` chain | `pypdf` + raw scan |
| Media | audio and video | `SpiderHoarder-Media`: `Streams` (codec, bitrate, resolution, fps, channels), `Chapters`, container `Tags`, `Duration` | `ffprobe -print_format json` |

Detection by extension / content type (same style as `extract.binary_kind`); ExifTool runs on everything and decides by content itself.
Output of all extractors for a file is merged into one mapping (distinct group keys, so no collisions) and dumped with `yaml.safe_dump`.

### Where it hooks in

New module `sdk/spider_hoarder_sdk/content/metadata.py` with `extract_metadata(path_on_disk, ctype, name) -> dict` and a
child-process entry point. In `gateway/jobs.py::_store_one`, call it **before** the `MAX_PARSE_BYTES` early return, so oversize files get
metadata too (these tools read headers, not the whole file). It runs on archive members too (they pass through `_store_one`), but not on the
archive itself unless ExifTool recognises it.

Because the file may not be in memory (big or streamed), the original is staged to a temp file from storage (`storage.open_read` → temp dir,
removed afterwards) and the extractors get the path.

### Isolation and limits

- Each extractor runs in its own child process on the staged file: wall-clock `METADATA_TIMEOUT` (default 30 s, killed on overrun),
  `RLIMIT_AS` (`METADATA_MAX_MEM`, default 1 GiB) and `RLIMIT_CPU`, no network (unshared network namespace where available, else documented
  as not enforced), a scrubbed environment, and a working directory that is the temp dir. Python extractors run via
  `python -I -m spider_hoarder_sdk.content.metadata <extractor> <path>` printing JSON to stdout (capped).
- Never fails the job: any extractor error/timeout/crash is appended to `notes` (`"<path>: metadata <extractor> failed (Timeout)"`) and the other
  extractors' output is still written.
- Output size cap per file (`METADATA_MAX_BYTES`, default 1 MiB): longer lists (e.g. Office revisions) are truncated with a
  `Truncated: true` marker in the group and a note.

### Not included (YAGNI)

No normalisation to a common schema, no inline summary, no opt-in tool argument, no metadata search/index across jobs, no editing of metadata.

## Implementation order

1. Image: `libimage-exiftool-perl`, `ffmpeg` (for `ffprobe`), GDAL runtime via the `rasterio`/`pyogrio` wheels (verify they bundle GDAL; else `gdal-bin` +
   `libgdal-dev`) in the images that import `spider_hoarder_sdk.gateway` (check which); `pyyaml`, `rasterio`, `pyogrio`, `lxml`, `pypdf` in
   `sdk/pyproject.toml`.
2. `content/metadata.py`: child-process runner with limits, the six extractors, merge + YAML dump; unit tests per extractor.
3. `gateway/jobs.py::_store_one`: stage, call, write `metadata/<path>.yaml`, notes; confirm `_job_result` lists the new file.
4. Tests: sample files per format (JPEG with EXIF/GPS, docx with tracked changes and comments, GeoTIFF with non-WGS84 CRS, GPX/GeoJSON, mp3 and
   mp4, PDF with an incremental update); oversize file still gets metadata; hung/crashing/memory-hungry extractor is contained and noted;
   archive members; no metadata → no file; truncation.
5. Docs; move the plan.

## Files to change

| File | Change |
|---|---|
| `sdk/pyproject.toml`, gateway images | new python deps; `exiftool`, `ffprobe`, GDAL |
| `sdk/spider_hoarder_sdk/content/metadata.py` | new: extractors, subprocess runner, YAML dump |
| `sdk/spider_hoarder_sdk/gateway/jobs.py` | `_store_one`: call before the size check, write `metadata/<path>.yaml`, notes |
| `sdk/tests/test_jobs.py` | per-format samples, oversize, archive members, truncation |
| `sdk/tests/test_security.py` | timeout, memory limit, crash containment, no network, hostile Office XML (entity bombs), zip-bomb docx |

## Docs to update

- `docs/architecture/jobs-and-storage.md`: add `metadata/<path>.yaml` to the layout, the shape and group naming, extractors, isolation and limits, oversize behaviour.
- `docs/api/tools.md`: mention `metadata/*.yaml` in job `files` and what the agent can expect.
- `docs/implemented-plans/index.md`: link this plan once moved; each updated section gets `See also: [Plan: file metadata](../implemented-plans/file-metadata.md)`.
- `docs/plans/index.md`: entry removed on implementation.
