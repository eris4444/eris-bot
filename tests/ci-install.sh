#!/bin/bash
# Real install of install-eris-bot.sh in a Debian trixie container that runs systemd (no Telegram token:
# a mock Bot API inside the container plays Telegram).
#   bash tests/ci-install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
work=$(mktemp -d)
bash src/build.sh "$work/install-eris-bot.sh"
docker build -q -t eris-bot-ci-systemd -f tests/Dockerfile.systemd tests > /dev/null
cid=$(docker run -d --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
        --tmpfs /run --tmpfs /run/lock -v "$PWD:/src:ro" -v "$work:/ci" eris-bot-ci-systemd)
trap 'docker rm -f "$cid" > /dev/null' EXIT
for _ in $(seq 60); do
  st=$(docker exec "$cid" systemctl is-system-running 2> /dev/null || true)
  case $st in running | degraded) break ;; esac
  sleep 1
done
echo "container: systemd $st"
docker exec "$cid" bash /src/tests/install-test.sh /ci/install-eris-bot.sh
