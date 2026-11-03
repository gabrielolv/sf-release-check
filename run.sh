#!/usr/bin/env bash
# Salesforce Release Impact Check - macOS / Linux launcher. Arguments are passed to release_check.py.
set -euo pipefail
cd "$(dirname "$0")"
command -v sf >/dev/null || { echo "Salesforce CLI (sf) is required: https://developer.salesforce.com/tools/salesforcecli"; exit 1; }
PY=$(command -v python3 || command -v python)
"$PY" -c "import pymupdf, openpyxl" 2>/dev/null || "$PY" -m pip install --user -q -r requirements.txt
exec "$PY" release_check.py "$@"
