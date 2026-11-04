#!/bin/bash
# Runs inside the systemd container (tests/ci-install.sh): install, check, talk to a mock Telegram,
# upgrade, uninstall.   install-test.sh INSTALLER
set -euo pipefail
inst=$1
fail() { echo "FAIL: $*" >&2; journalctl -u eris-bot -u eris-fan --no-pager -n 60 >&2 || true; exit 1; }
ok() { echo "ok: $*"; }
mode() { stat -c %a "$1"; }

bash "$inst" > /tmp/install.log 2>&1 || { cat /tmp/install.log; fail "installer"; }
cat /tmp/install.log
want=$(python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/eris-bot"); import erisbot; print(erisbot.VERSION)')
[ "$(eris-bot version)" = "$want" ] || fail "version ($(eris-bot version), expected $want)"
for f in /usr/local/bin/eris-bot /usr/local/lib/eris-bot/erisbot/app.py /usr/local/lib/eris-bot/erisbot/fand.py \
         /etc/systemd/system/eris-bot.service /etc/systemd/system/eris-fan.service /usr/local/sbin/eris-bot-uninstall \
         /etc/eris-bot/config.json; do
  [ -e "$f" ] || fail "missing $f"
done
[ "$(mode /etc/eris-bot/config.json)" = 600 ] || fail "config.json is $(mode /etc/eris-bot/config.json), not 600"
[ "$(mode /etc/eris-bot)" = 700 ] || fail "/etc/eris-bot is not 700"
[ "$(mode /var/lib/eris-bot)" = 700 ] || fail "/var/lib/eris-bot is not 700"
ok "files and permissions"
systemctl is-enabled --quiet eris-bot.service || fail "eris-bot.service not enabled"
systemctl is-enabled --quiet eris-fan.service || fail "eris-fan.service not enabled"
sleep 3
systemctl is-active --quiet eris-bot.service || fail "eris-bot.service not running (it should wait for setup)"
systemctl is-active --quiet eris-fan.service || fail "eris-fan.service not running"
journalctl -u eris-bot --no-pager | grep -q "eris-bot setup" || fail "no 'run eris-bot setup' hint in the log"
ok "services: eris-bot waits for setup, eris-fan runs"
eris-bot status
eris-bot selftest
eris-bot proxy
ok "eris-bot status / selftest / proxy"

# BindsTo: stopping the bot stops the fan control (fans back to the BIOS)
systemctl stop eris-bot.service
sleep 2
! systemctl is-active --quiet eris-fan.service || fail "eris-fan still runs without eris-bot"
ok "eris-fan stops with eris-bot"

# a mock Bot API plays Telegram: /start must give the menu, a stranger nothing
python3 /src/tests/e2e_mock.py 18080 1001 90 > /tmp/e2e.log 2>&1 &
mock=$!
sleep 2
python3 - << 'PY'
import sys
sys.path.insert(0, "/usr/local/lib/eris-bot")
from erisbot import config
sys.path.insert(0, "/src/tests")
import mocks
config.save({"token": mocks.TOKEN, "allowed_chat_ids": [1001], "api_base": "http://127.0.0.1:18080", "proxy": "off"})
PY
[ "$(mode /etc/eris-bot/config.json)" = 600 ] || fail "config.json lost mode 600"
systemctl start eris-bot.service
wait "$mock" || { cat /tmp/e2e.log; fail "end-to-end with the mock Bot API"; }
cat /tmp/e2e.log
sleep 1
systemctl is-active --quiet eris-fan.service || fail "eris-fan did not start with eris-bot"
if journalctl -u eris-bot --no-pager | grep -q "AAEhBP0av18OEMZ"; then fail "the token is in the log"; fi
ok "end-to-end: menu, dashboard, stranger ignored, token not logged"

# upgrade: the settings stay
bash "$inst" --no-selftest > /tmp/upgrade.log 2>&1 || { cat /tmp/upgrade.log; fail "upgrade"; }
grep -q "kept /etc/eris-bot/config.json" /tmp/upgrade.log || fail "upgrade did not keep config.json"
python3 -c 'import json; c = json.load(open("/etc/eris-bot/config.json")); assert c["allowed_chat_ids"] == [1001] and c["token"]'
ls /var/backups/eris-bot/*/etc/eris-bot/config.json > /dev/null || fail "no backup of config.json"
systemctl is-active --quiet eris-bot.service || fail "eris-bot not running after the upgrade"
ok "upgrade keeps the settings"

eris-bot-uninstall
[ ! -e /usr/local/bin/eris-bot ] || fail "eris-bot still installed"
[ ! -e /etc/systemd/system/eris-bot.service ] || fail "unit still there"
ls /var/backups/eris-bot/uninstall-*/eris-bot/config.json > /dev/null || fail "settings not kept on uninstall"
! systemctl is-active --quiet eris-bot.service || fail "still running"
ok "uninstall (settings kept in /var/backups/eris-bot)"
echo "ALL INSTALL CHECKS PASSED"
