# Stock Cellular Center V8.0

Desktop inventory management and audit application for Cellular Center retail stores.

High-performance barcode scanning (physical reader or manual entry) with real-time comparison against a master catalog imported from CSV, location-aware auditing through a persistent container model, and export to external systems via keyboard automation or clipboard.

---

## Features

### Scanning & Live Feedback
- **Decoupled scan pipeline** — a dedicated worker thread consumes a FIFO queue and handles secondary processing (alerts, proximity checks, surplus detection), so registering scans never blocks on UI work.
- **Incremental UI updates** — scan bursts update the tables through row-level actions (insert / update / move / delete) instead of rebuilding the whole view; any inconsistency triggers a full rebuild fallback, so the UI is never left in an incorrect state.
- **Sort modes** — *Last on bottom* (default), *Last on top*, *Alphabetical*, *Quantity*, and *Scan order* (manual reordering via drag & drop or move up/down).
- Autofocus after every scan, autoscroll to the latest entry, and a live progress bar with percentage.

### Location Model (`main_stock.json`)
- Persistent, hierarchical container model: **furniture (`%`)**, **boxes (`@`)** and **display cases (`#`)**.
- Supports non-linear scans (products scanned before or after their container's QR).
- Location discrepancy detection with an interactive *Move / Keep here* resolution flow.
- Deferred location validation (5 s debounced) after manual moves or drag & drop.

### Structural QR Codes (`@`, `%`, `#`)
- QR rows act as collapsible containers (double-click to fold/unfold).
- The quantity column shows the container's missing count, or a ✓ when complete.
- **QR codes are excluded from physical stock metrics** (scanned count, expected count, progress) and from product image downloads.

### Audit & Shortcuts
- **Differences window** (`F3`) — missing and surplus items per SKU, with per-SKU export-exclusion toggles.
- `F4` adds +1 unit to the selected SKU; `Delete` removes −1.
- Relevant-incidents counter (unknown codes + surplus units).
- Product image preview bound to the SKU.

### Security & Licensing
- Local authentication with **PBKDF2-HMAC-SHA256** (per-install random salt, configurable cost, timing-safe comparison).
- Licenses signed with **Ed25519** (RFC 8032) and verified in the client against an embedded public key.
- The license **private key stays outside the repository and outside the distributable tree** — it lives in the owner's environment only.
- Clock-rollback protection via a persisted last-used date.

---

## Architecture

- **`src/core/scanpipeline.py`** — decoupled scan pipeline. A single FIFO worker computes alerts (unknown code, proximity, surplus) and publishes results; it never touches Tkinter.
- **`src/gui/updates.py`** — pure, Tk-free view projection. `build_full_view` computes the complete UI state from the model; `apply_event` projects the state incrementally from a single scan event; `diff_views` / `apply_actions` translate view changes into row actions.
- **`src/main.py`** — consumes worker results on the main thread (`after` loop), applies incremental row actions to the tables, and falls back to a full rebuild on any inconsistency.

---

## Project Structure

```
Stock-Cellular-Center/
├── run_app.py                  # Entry point (auto-elevation, session log)
├── build_windows.py            # PyInstaller build script
├── requirements.txt
├── README.md
├── SECURITY_NOTES.md
├── IMPLEMENTATION_PLAN.md
├── task.md
├── .gitignore
├── res/                        # Icons and UI graphics
├── docs/
│   └── historical/             # Version history / changelog
├── src/
│   ├── main.py                 # Main window, controller, incremental UI applicator
│   ├── config.py               # AppData-based persistent configuration
│   ├── core/
│   │   ├── auth.py             # PBKDF2 authentication + Ed25519 license verification
│   │   ├── automation.py       # Global hotkeys, keyboard/clipboard export
│   │   ├── images.py           # Background product image downloads + gallery
│   │   ├── inventory.py        # Stock model, CSV import, containers, main_stock.json
│   │   └── scanpipeline.py     # Decoupled scan worker (FIFO queue, no Tk)
│   └── gui/
│       ├── updates.py          # Pure view projection (full rebuild + incremental)
│       ├── utils.py            # Window helpers
│       └── components/
│           ├── tables.py       # InventoryTable (Treeview wrapper)
│           ├── selector.py     # Session selector (new / history)
│           └── history.py      # Session history explorer
├── tools/
│   ├── license_generator.py    # Owner-only offline license signing tool
│   └── convert_icon.py         # PNG → ICO converter
└── tests/
    ├── test_auth.py            # Auth & licensing tests
    ├── test_main_stock.py      # Containers, hierarchy, main_stock.json
    ├── test_v8_features.py     # V8 features: QR metrics, shortcuts, incidents
    ├── test_scan_burst.py      # Worker bursts, ordering, error resilience
    ├── test_fase_b_projection.py  # Incremental-projection invariant (29 tests)
    └── test_project_integrity.py  # Repo integrity: no private keys, sources parse
```

---

## Getting Started

```bash
# 1. Create a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python run_app.py
```

Requirements: Python 3.11+ on Windows.

> On first run the app asks you to set a local access password and requires a valid signed license (`license.dat`). Contact the owner to obtain one — see [License Management](#license-management-owner-only).

---

## Running the Tests

The test suite covers the core logic without requiring a display or scanning hardware:

```bash
python -m unittest tests.test_scan_burst tests.test_v8_features tests.test_main_stock tests.test_project_integrity tests.test_fase_b_projection
```

| Suite | Scope |
|---|---|
| `test_scan_burst` | Worker FIFO behavior: 50-event bursts registered exactly and in order, mixed/unknown codes, error resilience (a failing event never kills the worker), alerts are pure (no Tk). |
| `test_fase_b_projection` | 29 tests proving the **incremental projection invariant**: applying events step by step always yields the exact same UI state as a full rebuild — across sort modes, SKU repetition, QR folding and replacement, grouped modes with reordering, diff transitions, metrics, and worker→queue→updates integration, including the fallback on inconsistent events. |
| `test_v8_features` | QR exclusion from stock metrics, image manager ignoring QR codes, container hierarchy status, F4/Delete operations, relevant-differences counter. |
| `test_main_stock` | `main_stock.json` loading/persistence, container status and ✓, non-linear scans, product moves and location updates, discrepancy checks. |
| `test_project_integrity` | All Python sources parse, no private key in the distribution tree, no runtime license file in the tree. |
| `test_auth` | PBKDF2 setup/verify, dynamic iteration cost, Ed25519 license validation (valid / tampered / invalid signature), expiry + grace period, anti-clock-rollback. |

> Note: the auth grace-period flow opens a modal dialog, so `test_auth` needs an interactive desktop session (it blocks in headless environments).

---

## Building for Windows

```bash
python build_windows.py
```

Produces a windowed PyInstaller build (one-dir) in `dist/`, collecting `customtkinter`, `cryptography` and `Pillow`, and excluding the test modules.

---

## License Management (owner-only)

`tools/license_generator.py` is an **offline, owner-only** tool. It signs `license.dat` files with the Ed25519 private key, which by default lives in:

```
%LOCALAPPDATA%\StockCellularCenter\license-authority\
```

The private key is never embedded in the client, compiled into the binary, or stored in this repository. The application only verifies licenses with the embedded public key, and stores the active license in `%LOCALAPPDATA%\StockCellularCenter\license.dat`.

```bash
# Generate a 30-day license for a customer
python tools/license_generator.py --licensee "Customer Name" --days 30 --grace 2 --out C:\Licencias\license.dat
```

---

## Data Locations

| Data | Location |
|---|---|
| App configuration, credentials, license, runtime state | `%LOCALAPPDATA%\StockCellularCenter\` |
| Product image cache | `%LOCALAPPDATA%\StockCellularCenter\img\` |
| Scan sessions (JSON), imported CSVs, `main_stock.json` | `<project>\Escaneos\` (gitignored) |

All of these are excluded from version control — the repository contains only code, tests, and documentation.

---

## Status & Known Limitations

The project is in active development. The following are **not** implemented yet:

- **Grouped notifications** — alerts are currently emitted per scan event; grouping by SKU/situation is pending.
- **Full persistent hierarchy editing** — the container model in `main_stock.json` supports the current flows, but a complete management UI for furniture/box/display hierarchies is pending.
- **Manual GUI test pass** — unit tests cover the core logic; a full manual pass (real scanner, drag & drop, all windows) is still pending.
- **Verified Windows build artifact** — the build script is provided, but a release artifact has not been produced and verified yet.
