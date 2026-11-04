#!/bin/bash
# Assemble the self-contained install-eris-bot.sh from the sources in this folder.
#   bash src/build.sh /path/to/install-eris-bot.sh
set -euo pipefail
cd "$(dirname "$0")"
out=${1:?usage: build.sh OUTPUT}
emit() {  # SRC DEST MODE
  local tag
  tag="ERISBOT_EOF_$(basename "$1" | tr -c 'A-Za-z0-9\n' '_')"
  if grep -qx "$tag" "$1"; then echo "delimiter $tag occurs in $1" >&2; exit 1; fi
  printf "  put_file %s %s << '%s'\n" "$2" "$3" "$tag"
  cat "$1"
  printf '%s\n' "$tag"
}
{
  sed -n '1,/^#@@FILES@@$/p' install-head.sh | sed '$d'
  echo "write_uninstaller() {"
  emit eris-bot-uninstall '"$1"' 0755
  echo "}"
  echo "write_files() {"
  for f in erisbot/*.py; do
    emit "$f" "\"\$STAGE/$f\"" 0644
  done
  emit eris-bot /usr/local/bin/eris-bot 0755
  emit eris-bot.service /etc/systemd/system/eris-bot.service 0644
  emit eris-fan.service /etc/systemd/system/eris-fan.service 0644
  echo "  write_uninstaller /usr/local/sbin/eris-bot-uninstall"
  echo "}"
  sed -n '/^#@@FILES@@$/,$p' install-head.sh | sed '1d'
} > "$out.tmp"
chmod 0755 "$out.tmp"
bash -n "$out.tmp"
mv -f "$out.tmp" "$out"
echo "built $out ($(wc -c < "$out") bytes, sha256 $(sha256sum "$out" | cut -c1-16))"
