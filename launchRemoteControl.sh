#!/usr/bin/env bash
# Lance la télécommande PC du robot BatMobile.
set -euo pipefail

# Dossier du script, pour pouvoir le lancer depuis n'importe où
cd "$(dirname "$(readlink -f "$0")")"

if ! python3 -c "import PyQt6" 2>/dev/null; then
    echo "PyQt6 introuvable : sudo apt install python3-pyqt6  (ou pip install -r requirements.txt)" >&2
    exit 1
fi

exec python3 batmobile.py "$@"
