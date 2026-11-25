#!/usr/bin/env bash
# Rio (the original system, code in rio1/). Kept at the root so `./edit.sh ...` still works.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/rio1/edit.sh" "$@"
