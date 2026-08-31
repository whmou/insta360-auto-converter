# apps

- `spatial-media/` is used by the active local workflow to inject and inspect spherical metadata.
- `insta360_auto_converter.py` is deliberately inert and fails closed. It cannot run the retired Google Drive/Docker workflow.
- `Image-ExifTool-12.10/` and the remaining files in this directory are retained historical or vendored material.
- New work should target `insta360_converter/`; do not restore executable legacy behavior.
