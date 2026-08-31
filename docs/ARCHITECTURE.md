# Architecture

## Active path

- `insta360_converter/pipeline.py`
  - INSV discovery and pairing
  - MediaSDK command construction and execution
  - spherical metadata injection and verification
  - atomic upload ledger
  - YouTube upload and playlist reconciliation
  - CLI and end-to-end workflows
- `local_pipeline.py`
  - compatibility shim for the older command and imports
- `run-from-sd.ps1`
  - Windows launcher backed by external `%LOCALAPPDATA%\Insta360AutoConverter\config.psd1`
  - ignored `.local/config.psd1` remains a compatibility fallback
- `tests/`
  - offline unit tests; external services are mocked

## Data flow

```text
paired INSV
  -> source preflight
  -> reject output-name collisions
  -> write source/settings conversion manifest
  -> MediaSDK stitch
  -> inject spherical metadata into a partial file
  -> verify MP4 + 2:1 + spherical/equirectangular flags
  -> reserve one Pacific-day upload attempt under an inter-process lock
  -> optional resumable YouTube upload
  -> atomic channel-bound ledger receipt
  -> optional playlist reconciliation
  -> optional exact-fingerprint local MP4 cleanup
```

## Durable state

- The ledger is bound to one authenticated channel and is the source of truth for upload attempts, video IDs, and playlist state.
- A sibling lock file serializes quota reservations and ledger writes across processes.
- Failed and interrupted upload attempts conservatively count toward the 80-video Pacific quota-day cap.
- An ambiguous interrupted attempt fails closed until the owner inspects YouTube and resolves it explicitly; confirmed video IDs receive an online channel-ownership check.
- Conversion manifests bind each output to sampled source identity and stitching settings before interrupted artifacts can be reused.
- Source fingerprints are independent from the Windows drive letter.
- Playlist synchronization validates channel ownership and first reads remote membership, so retries do not duplicate items.
- Cleanup requires a matching channel-bound receipt and the exact current MP4 fingerprint.
- Source INSV files are never modified or deleted.

## Legacy and vendored code

- `apps/spatial-media/` is the vendored spherical metadata tool used by the active workflow.
- `apps/insta360_auto_converter.py` is an inert, fail-closed marker for the retired Google Drive/Google Photos controller.
- Other files under `apps/` are retained historical or vendored material and are not active entry points.
- `apps/Image-ExifTool-12.10/` is retained for historical compatibility and is not used by the active video workflow.
- The old Docker workflow is no longer an active or supported path.
