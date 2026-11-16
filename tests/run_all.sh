#!/bin/bash
# Runs all tests against the copy on the Pi, one after another, and stops if Tiny LLM is in use there.
# Needs an installed copy on the Pi (TINYLLM_PI_DIR, default ~/pi-projects/tinyllm) and, on this PC,
# tests/.venv with pyte (see README.md). Takes about 20 minutes; outputs land in tests/out/.
cd "$(dirname "$0")" || exit 1
PYTHON="${PYTHON:-.venv/bin/python}"
export TINYLLM_PI_DIR="${TINYLLM_PI_DIR:-~/pi-projects/tinyllm}"
# Runs on the Pi, whose shell expands the ~; the unit tests read the variable there.
PI_PYTHON="TINYLLM_PI_DIR=$TINYLLM_PI_DIR $TINYLLM_PI_DIR/.venv/bin/python"
mkdir -p out
failed=0

in_use() {
    [ "$(ssh -o BatchMode=yes pi 'pgrep -fc "[t]inyllm_tui"')" != "0" ]
}

run() {
    local name=$1; shift
    if in_use; then
        echo "STOPPED before $name: Tiny LLM is in use on the Pi"
        exit 2
    fi
    ssh -o BatchMode=yes pi 'rm -rf /tmp/tinyllm-test /tmp/tinyllm-unit'
    local start=$SECONDS
    "$@" > "out/$name.txt" 2>&1
    local code=$?
    echo "$name: exit $code after $(( SECONDS - start )) s"
    grep -E "^(FAIL|!!!)" "out/$name.txt"
    [ $code -eq 0 ] || failed=1
}

run unit_render_heat ssh -o BatchMode=yes pi "$PI_PYTHON -" < unit_render_heat.py
run unit_files_trend ssh -o BatchMode=yes pi "$PI_PYTHON -" < unit_files_trend.py
run tui_basics_100x24 "$PYTHON" tui_basics.py 100 24
run tui_basics_80x24 "$PYTHON" tui_basics.py 80 24
run tui_live_status_100x24 "$PYTHON" tui_live_status.py 100 24
run tui_live_status_50x24 "$PYTHON" tui_live_status.py 50 24
run tui_startup "$PYTHON" tui_startup.py
run tui_retry_copy_info_files "$PYTHON" tui_retry_copy_info_files.py
run tui_chats_compact_download "$PYTHON" tui_chats_compact_download.py
run serve_network "$PYTHON" serve_network.py

ssh -o BatchMode=yes pi 'rm -rf /tmp/tinyllm-test /tmp/tinyllm-unit
echo "left on the Pi: llama-server $(pgrep -c llama-server), tinystories_pi $(pgrep -c tinystories_pi)"'
exit $failed
