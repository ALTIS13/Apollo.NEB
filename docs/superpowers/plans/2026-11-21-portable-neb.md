# Apollo.NEB Portable Implementation Plan

> **For agentic workers:** Complete the independent Python runtime and panel surface, then verify them together.

**Goal:** Install and operate Apollo.NEB on Linux with operator-supplied hardware and a supported/configured miner.

**Architecture:** User-owned service + authenticated loopback HTTP + declarative process adapters, bounded local telemetry and incident history.

**Tech Stack:** Python 3.10+, SQLite, HTML/CSS/JS, optional systemd --user.

**Spec:** `docs/superpowers/specs/2026-11-21-portable-neb-design.md`.

### 1. Runtime and explicitly supported worker configuration

- [ ] Reject invalid executables, config, wallet templates, unknown IDs, missing temperature and duplicate target GPU in tests.
- [ ] Build AMD/OpenCL, NVIDIA/CUDA and custom adapters, stop only a worker's process group and record a thermal incident. Separate preset selection from hardware claims.
- [ ] Accrue optional support time only when opted in; never report it as balance or tokens. Cover change/stop/failure and normal return.

### 2. First installation and panel

- [ ] Verify local host/origin and authenticated POST actions with tests; install into XDG-compatible user directories without sudo, remote downloads or autostarted workers.
- [ ] Render overview, worker list, wallet, event history and status of support. Reuse applicable first-party patterns from Atlas and retain deterministic configuration.

### 3. Artifact and readme

- [ ] Confirm that the archive excludes private config/data, execute the Linux setup and test the real loopback dashboard with synthetic worker drives. Document which physical devices remain untested.
