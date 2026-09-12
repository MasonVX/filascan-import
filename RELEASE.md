# Release checklist

The repository is prepared for `filascan-import-v0.2.0`.

1. Confirm that `filascan_import/plugin.json`, `filascan_import/__init__.py`
   and `pyproject.toml` contain the same version.
2. Run the complete test suite against a current FilaMan backend checkout.
3. Build and verify the release artifacts:

   ```bash
   python3 scripts/build_release.py
   ```

4. Inspect `dist/filascan-import-0.2.0.zip` and its SHA-256 file.
5. Commit and push the repository to `MasonVX/filascan-import`.
6. Create and push the release tag:

   ```bash
   git tag -a filascan-import-v0.2.0 -m "FilaScan Import 0.2.0"
   git push origin filascan-import-v0.2.0
   ```

The release workflow reruns the tests, validates the tag against the manifest,
builds the ZIP, and publishes both the ZIP and checksum. Never reuse a release
tag for changed code; increment the plugin patch version instead.
