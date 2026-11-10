#!/usr/bin/env bash
# Render build command (render.yaml buildCommand and the dashboard both run
# `bash scripts/render_build.sh`), kept in the repo so the build is reviewable
# and reproducible:
#   - pip is pinned: pip 25.0.1 backtracked into a failed pandas source build
#     on the 2026-11-10 graph, while 26.3 resolves it cleanly.
#   - the locked graph is installed, not a fresh resolution of
#     requirements.txt, so a new transitive release can't change or break a
#     deploy.
#   - pip check fails the build if the installed set is inconsistent.
set -euo pipefail
pip install "pip==26.3"
pip install -r requirements.lock.txt
pip check
