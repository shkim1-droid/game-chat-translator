# Game Chat Translator

Windows game chat OCR with transparent Korean subtitles. The updater uses this dedicated repository only. It has no relationship to any other repository or scheduled collector.

## User updates

The installed program checks `updates/latest.json` on startup and every 60 seconds. A new version is downloaded and checked, then applied on restart. Settings and translation history remain under the local `data/` folder and are never published here. The stable launcher rolls back when a new version fails its startup health check.

## Maintainer workflow

1. Edit `src/app.py` and/or `src/overlay.py`.
2. Increase `src/version.json`, for example from `8.0.0` to `8.0.1`.
3. Run tests and push the change to this repository's `main` branch.
4. This repository's GitHub Action creates the versioned update ZIP, computes SHA-256, and updates `updates/latest.json`.

The program receives the update without requiring the user to extract files. Releases changing dependencies require a separate installer update. Do not overwrite an existing version.

The remote manifest uses HTTPS and checks package hashes. This is not a cryptographic publisher-signature system. The Windows overlay has not been exercised in the development Linux environment. Translation calls use unofficial Google endpoints and can fail when the service changes or limits requests.
