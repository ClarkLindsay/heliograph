#!/bin/sh
# Print one release's section of CHANGELOG.md (without its heading). Exits 1 if there's no such section or it's empty.
#   sh .github/changelog.sh 0.6.2
v="$1"
[ -n "$v" ] || { echo "usage: changelog.sh VERSION" >&2; exit 2; }
out=$(awk -v v="$v" '
  /^## / { if (on) exit; split($0, w, " "); if (w[2] == v) { on = 1; next } }
  on { print }
' "$(dirname "$0")/../CHANGELOG.md" | sed -e '/./,$!d')
[ -n "$(printf %s "$out" | tr -d '[:space:]')" ] || exit 1
printf '%s\n' "$out"
