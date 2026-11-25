#!/usr/bin/env bash
# Rio 2.0 (code in rio2/: everything Rio does, plus the 4K two-camera mode, coming soon).
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rio2/edit.sh" "$@"
