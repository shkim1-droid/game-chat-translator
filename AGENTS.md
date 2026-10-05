# Scope and release rules

Work only in shkim1-droid/game-chat-translator. Do not modify any other repository, collector, workflow, or schedule.

- User data, credentials, logs, OCR captures, and chat history must never be committed.
- Application changes belong in src/. Increase src/version.json for every release.
- Run python -m unittest discover -s tests and syntax compilation before publication.
- The release workflow packages only app.py, overlay.py, requirements.txt, version.json.
- Existing updates/<version>.zip files are immutable. Increase the version instead of replacing one.
- Keep the dependency file unchanged for ordinary application updates; the current runtime cannot migrate dependencies.
- launcher.py and updater.py are the stable installation foundation; changes to those require an installer revision.
