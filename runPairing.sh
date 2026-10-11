#!/usr/bin/env bash
# Appaire automatiquement le robot BatMobile (options transmises : --repair, --timeout N).
set -euo pipefail

# Dossier du script, pour pouvoir le lancer depuis n'importe où
cd "$(dirname "$(readlink -f "$0")")"

if ! python3 -c "import dbus, gi" 2>/dev/null; then
    echo "dbus / gi introuvables : sudo apt install python3-dbus python3-gi" >&2
    exit 1
fi

exec python3 bt_pairing.py "$@"
