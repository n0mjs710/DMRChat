#!/bin/sh
# Launch DMRChat inside a venv, leaving the system Python untouched.
#
#   ./run.sh                       discover the radio, install routes, chat
#   ./run.sh --radio-id 1234567    skip the radio-id prompt
#   ./run.sh --sim --radio-id 1111 local peer-to-peer test, no radio needed
#
# The project folder can safely live in iCloud and be shared between Macs. The
# venv cannot: pyvenv.cfg hardcodes an absolute path to one interpreter, bin/
# holds symlinks into it, and psutil is a compiled architecture-specific
# extension. A venv built on one Mac will not run on another. So when the
# project is inside a synced folder, the venv is kept on the local disk and
# each Mac builds its own. Override with DMRCHAT_VENV=/path if you want.
set -e

PROJECT="$(cd "$(dirname "$0")" && pwd)"

case "$PROJECT" in
    */Mobile\ Documents/*|*/CloudStorage/*|*/Dropbox/*|*/Google\ Drive*)
        VENV_DEFAULT="$HOME/.dmrchat/venv"
        SYNCED=1 ;;
    *)
        VENV_DEFAULT="$PROJECT/.venv"
        SYNCED=0 ;;
esac
VENV="${DMRCHAT_VENV:-$VENV_DEFAULT}"

# Verify the venv actually runs on THIS Mac rather than assuming it exists.
# Catches a synced venv, a moved or upgraded Python, and arm64/x86_64 mismatch.
if ! "$VENV/bin/python" -c "import psutil, curses" >/dev/null 2>&1; then
    if [ -f "$VENV/pyvenv.cfg" ]; then
        echo "The venv at $VENV does not run on this Mac (different Python or"
        echo "architecture). Rebuilding it locally..."
        rm -rf "$VENV"
    elif [ -e "$VENV" ]; then
        echo "$VENV exists but is not a venv. Refusing to touch it." >&2
        echo "Set DMRCHAT_VENV to a different path." >&2
        exit 1
    else
        [ "$SYNCED" = "1" ] && echo "Project is in a synced folder; building a local venv."
        echo "Creating venv at $VENV ..."
    fi
    python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet -r "$PROJECT/requirements.txt"
fi

exec "$VENV/bin/python" "$PROJECT/dmrchat.py" "$@"
