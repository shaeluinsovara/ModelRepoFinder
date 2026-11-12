#!/bin/bash
# ModelRepoFinder launcher for macOS.
# Double-click this file. The first time, use right-click > Open.

cd "$(dirname "$0")" || exit 1
export PYTHONUNBUFFERED=1

echo
echo "  ModelRepoFinder v1.3"
echo "  ======================"
echo

pause_close() {
  echo
  read -r -p "  Press Return to close this window..." _
}

find_python() {
  local candidates=(
    python3
    python
    /usr/local/bin/python3
    /opt/homebrew/bin/python3
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3
  )
  local cmd path
  for cmd in "${candidates[@]}"; do
    if path="$(command -v "$cmd" 2>/dev/null)"; then
      if "$path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        printf '%s\n' "$path"
        return 0
      fi
    elif [[ -x "$cmd" ]]; then
      if "$cmd" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        printf '%s\n' "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

PY="$(find_python)" || true
if [[ -z "${PY}" ]]; then
  echo "  Python 3.9 or newer was not found on this Mac."
  echo "  Please install it from https://www.python.org/downloads/"
  echo "  Then run this file again (right-click it, then choose Open)."
  pause_close
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "  First run: setting up a private toolkit folder. This can take a minute..."
  if ! "$PY" -m venv .venv; then
    echo
    echo "  Could not create the toolkit folder. If you installed Python from"
    echo "  python.org, try running this file again after opening a new Terminal"
    echo "  window. If you use Homebrew, you may need: brew install python"
    pause_close
    exit 1
  fi
fi

VPY=".venv/bin/python"
"$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || true
echo "  Making sure the helper libraries are ready..."
if ! "$VPY" -m pip install -r requirements.txt; then
  echo
  echo "  Could not install the helper libraries. Please check your internet"
  echo "  connection and run this file again."
  pause_close
  exit 1
fi

echo
echo "  Starting. A browser window should open. Leave this window open."
echo "  Close this window when you are finished."
echo
"$VPY" app.py
status=$?
if [[ "$status" -ne 0 ]]; then
  echo
  echo "  The app stopped with a problem. You can screenshot this window and share it."
  pause_close
  exit "$status"
fi
