# Plans

Plans awaiting implementation or kept as background. Implemented plans move to [implemented-plans](../implemented-plans/index.md).

- [OCR: Tesseract first, remote multimodal model fallback](ocr-hybrid.md) — needs the lightllm plan `ocr-vision-models.md` for the default remote model
- [Parser service](parser-service.md) — all parsing and metadata extraction in a separate egress-less container with one HTTP endpoint; supersedes the child-process isolation of the file-metadata plan, amends the OCR plan
- [Parser wrapper](parser-wrapper.md) — the parser container pulls from and writes to the blob store itself: a wrapper with the credentials, a no-secret parser uid, an authenticated egress proxy; POST/poll status; replaces the topology of the parser-service plan
- [gVisor runtime](gvisor-runtime.md) — stub
- [Metrics and structured logs](metrics-and-logging.md) — stub
- [`mcp` 2.x SDK port](mcp-sdk-2-migration.md) — stub
- [Persistent profile and Playwright traces](optional-session-features.md) — stub
- [Kubernetes variant](kubernetes-variant.md) — stub
- [Android manual test](android-manual-test.md) — **todo, needs a human**: Google sign-in and real apps on both Android servers, the
  one part of the [Android bring-up](../implemented-plans/android-kvm-bringup.md) that could not be run unattended.
