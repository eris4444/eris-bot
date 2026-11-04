#!/bin/bash
# eris-bot installer (self-contained): a Telegram bot for this server (ERIS Console, Marzban, backups, fans).
#
#   bash install-eris-bot.sh                install or upgrade (config.json, the password and the state stay)
#   bash install-eris-bot.sh --uninstall    remove eris-bot (same as eris-bot-uninstall)
#   bash install-eris-bot.sh --no-start     install, but do not start the services now
#   bash install-eris-bot.sh --no-selftest  skip the checks at the end
# After the first install: eris-bot setup
set -euo pipefail
VERSION=1.0.0
LOG=/var/log/eris-bot-install.log
TS=$(date +%Y%m%d-%H%M%S)
BK=/var/backups/eris-bot/$TS
LIB=/usr/local/lib/eris-bot
STAGE=$LIB.new
say() { printf '\033[1;36m[eris-bot]\033[0m %s\n' "$*"; printf '%s [install] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
die() { say "ERROR: $*"; exit 1; }

put_file() {  # DEST MODE < content
  local dest=$1 mode=$2
  install -d -m 0755 "$(dirname "$dest")"
  cat > "$dest.new-file"
  chmod "$mode" "$dest.new-file"
  mv -f "$dest.new-file" "$dest"
}

#@@FILES@@

[ "$(id -u)" -eq 0 ] || die "run as root"
START=1
SELFTEST=1
for a in "$@"; do
  case $a in
    --uninstall)
      if [ -x /usr/local/sbin/eris-bot-uninstall ]; then exec /usr/local/sbin/eris-bot-uninstall; fi
      tmp=$(mktemp)
      write_uninstaller "$tmp"
      bash "$tmp"; rc=$?; rm -f "$tmp"; exit $rc ;;
    --no-start) START=0 ;;
    --no-selftest) SELFTEST=0 ;;
    -h | --help) sed -n '2,9p' "$0"; exit 0 ;;
    *) die "unknown option $a" ;;
  esac
done

[ -r /etc/debian_version ] || die "this installer is for Debian"
say "eris-bot $VERSION installer, log: $LOG"

# 1. packages: only what is missing, never an upgrade (python3, openssl for the backups, CA certificates)
need=()
for p in python3 openssl ca-certificates; do
  dpkg-query -W -f='${Status}' "$p" 2> /dev/null | grep -q "install ok installed" || need+=("$p")
done
if [ ${#need[@]} -gt 0 ]; then
  say "installing missing packages: ${need[*]}"
  if ! DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y -q --no-install-recommends --no-upgrade "${need[@]}" >> "$LOG" 2>&1; then
    DEBIAN_FRONTEND=noninteractive timeout 600 apt-get update -q >> "$LOG" 2>&1 || true
    DEBIAN_FRONTEND=noninteractive timeout 900 apt-get install -y -q --no-install-recommends --no-upgrade "${need[@]}" >> "$LOG" 2>&1 \
      || die "apt-get install ${need[*]} failed (see $LOG)"
  fi
else
  say "packages: all present (python3 openssl ca-certificates)"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || die "python3 3.9 or newer is needed"

# 2. a copy of everything this installer may change (the settings too)
mkdir -p "$BK"
chmod 700 "$BK" /var/backups/eris-bot
for p in /etc/eris-bot /usr/local/lib/eris-bot /usr/local/bin/eris-bot /usr/local/sbin/eris-bot-uninstall \
         /etc/systemd/system/eris-bot.service /etc/systemd/system/eris-fan.service; do
  if [ -e "$p" ]; then
    mkdir -p "$BK$(dirname "$p")"
    cp -a "$p" "$BK$p"
  fi
done
say "backup of the old files: $BK"

# 3. an upgrade stops the bot (eris-fan stops with it: the fans go back to the BIOS meanwhile)
was_active=0
if systemctl is-active --quiet eris-bot.service 2> /dev/null; then
  was_active=1
  systemctl stop eris-bot.service eris-fan.service || true
fi

# 4. files: the program into a new folder first, then swapped in
rm -rf "$STAGE"
write_files
rm -rf "$LIB.old"
[ -d "$LIB" ] && mv "$LIB" "$LIB.old"
mv "$STAGE" "$LIB"
rm -rf "$LIB.old"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} + 2> /dev/null || true
install -d -m 0700 /etc/eris-bot /var/lib/eris-bot
say "files: /usr/local/bin/eris-bot, $LIB/, eris-bot.service, eris-fan.service, /usr/local/sbin/eris-bot-uninstall"

# 5. settings: kept on an upgrade (only new keys are added), mode 600
if [ ! -e /etc/eris-bot/config.json ]; then
  python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris-bot"); from erisbot import config
c = dict(config.DEFAULTS); c["allowed_chat_ids"] = []; config.write_json(config.CONFIG, c, 0o600)'
  say "settings: /etc/eris-bot/config.json (no token yet)"
else
  python3 - << 'PY'
import sys
sys.path.insert(0, "/usr/local/lib/eris-bot")
from erisbot import config
data = config.read_json(config.CONFIG, {}) or {}
missing = {k: v for k, v in config.DEFAULTS.items() if k not in data}
if missing:
    data.update(missing)
config.write_json(config.CONFIG, data, 0o600)
PY
  say "settings: kept /etc/eris-bot/config.json"
fi
chmod 600 /etc/eris-bot/config.json
[ -e /etc/eris-bot/backup.pass ] && chmod 600 /etc/eris-bot/backup.pass

# 6. services
systemctl daemon-reload
systemctl enable eris-bot.service eris-fan.service > /dev/null 2>&1 || say "warning: could not enable the services"
configured=$(python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris-bot"); from erisbot import config
c = config.load(); print("yes" if c["token"] and c["allowed_chat_ids"] else "no")')
if [ "$START" = 1 ]; then
  systemctl restart eris-bot.service || say "warning: eris-bot.service did not start (journalctl -u eris-bot)"
  systemctl start eris-fan.service || say "warning: eris-fan.service did not start (journalctl -u eris-fan)"
fi
if [ "$configured" = yes ]; then
  say "eris-bot runs (proxy and chats from /etc/eris-bot/config.json)"
else
  say "next: eris-bot setup   (the bot token and your chat ID; it waits until then)"
fi
[ "$was_active" = 1 ] && say "upgraded from the running version; the bot was restarted"

# 7. self-test
rc=0
if [ "$SELFTEST" = 1 ]; then
  /usr/local/bin/eris-bot selftest || rc=$?
fi
say "done. Commands: eris-bot setup | status | test | proxy | password | logs | update"
say "remove: eris-bot-uninstall   (or: bash $0 --uninstall)"
exit $rc
