#!/bin/sh
# Print one release's section of CHANGELOG.md (without its heading). Exits 1 if there's no such section or it's empty.
#   sh .github/changelog.sh 0.6.2
v="$1"
[ -n "$v" ] || { echo "usage: changelog.sh VERSION" >&2; exit 2; }
out=$(awk -v v="$v" '
  /^## / { if (on) exit; split($0, w, " "); if (w[2] == v) { on = 1; next } }
  on { print }
' "$(dirname "$0")/../CHANGELOG.md" | sed -e '/./,$!d' | awk '
  # GitHub release notes keep single line breaks, so a wrapped line is joined back onto the one before it
  /^[ \t]*$/ { if (buf != "") print buf; buf = ""; print; next }
  # a new item or heading: "- ", "* ", "1. " or "#" (a line that starts **bold** is a wrapped line, not an item)
  /^[ \t]*([-*]|[0-9]+\.)[ \t]/ || /^#/ { if (buf != "") print buf; buf = $0; next }
  { sub(/^[ \t]+/, ""); buf = (buf == "") ? $0 : buf " " $0 }
  END { if (buf != "") print buf }
')
[ -n "$(printf %s "$out" | tr -d '[:space:]')" ] || exit 1
printf '%s\n' "$out"
