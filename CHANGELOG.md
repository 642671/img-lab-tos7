# Changelog

All notable changes to Image Lab are documented in this file. The project follows a simple `MAJOR.MINOR.PATCH` scheme while it is pre-1.0.

## 0.1.0 - 2026-11-11

First release.

### Added

- Resolution-mode generation for JPEG, PNG, and WebP with fixed quality and aligned dimensions.
- Byte-target generation with a two-level solver: prior-bpp resolution search, then noise-mix correction with damped secant extrapolation; final tolerance 1.5%.
- Pre-generation plan preview and post-generation measured pixels, bytes, deviation, convergence flag, iterations, and elapsed time.
- Batch generation (up to 50 specs) with CSV manifest and ZIP bundle download.
- Content models: photo texture (default), smooth gradient, solid colour, high-frequency noise.
- Downsampling probe with 1 px black/white stripes for PNG and 4 px stripes aligned to JPEG 8×8 blocks.
- Photo library with preview, per-file download, delete, clear, and automatic pruning (200 entries / 1.5 GiB).
- Response-speed measurement with concurrency, rounds, per-request timeout, p50/p95/mean/min/max, and failure rate.
- Network phase breakdown (connect, send, wait, download) and optional server timing from `Server-Timing`, `X-Response-Time`, or `X-Runtime`.
- Deterministic fixed-seed rendering so repeated specs produce identical bytes.
- TOS theme inheritance, light/dark modes, and responsive layout.
- systemd service with bounded shutdown, stale-socket cleanup, install diagnostics, and resource limits.
- Deterministic x86_64 and aarch64 Deb packaging plus SHA256 checksums.

### Safety

- Generation is offline and never contacts network services.
- Non-converged results are explicitly flagged in the API, CSV, and UI; sizes are never faked by pinning a resolution.
- Displayed byte counts are read from disk when the library is loaded.
- The speed tester caps redirects at 3, request bodies at 32 MiB, and concurrency at 16.

### Verification

- Backend unit tests cover rendering determinism, probe geometry, PNG/JPEG/WebP encoders, calibration references, byte-mode convergence, cancellation, library persistence, CSV bytes, ZIP contents, speed timing, HTTP routes, and package contracts.
- Frontend Node tests cover formatting, priors, plan estimation, spec parsing, batch parsing, convergence summaries, escaping, and cookie parsing.
- POSIX socket lifecycle tests require Linux/TOS; they are skipped on Windows.
- Real-device installation, iframe proxy, encoder ABI, and performance acceptance are still pending.
