# Changelog

## 1.1.0
- **CPU fan control, found by the bot itself:** 🌀 فن و عملکرد → 🔍 تشخیص کنترلر → 🪄 پیدا کردن خودکار. It tries the Super I/O drivers one by one (sensors-detect's suggestions first, then it87, w83627ehf, nct6775, w83627hf, f71882fg, …; each driver checks the chip ID itself, nothing is forced), loads it87 with `ignore_resource_conflict=1` when the BIOS claims the ports for ACPI, keeps the working driver for the next boots, and tests every new pwm (100 % then 35 %) to see which fan it really turns. The fan gets a name from the test (`فن CPU/کیس fan1`).
- If the chip is there but held by ACPI and no module option helps: a button adds `acpi_enforce_resources=lax` to GRUB (two confirmations, a copy of /etc/default/grub first, then a reboot).
- If nothing is found, the bot says so: the fan is then controlled only by the BIOS.

## 1.0.0
First release.
- Telegram bot in plain Python 3 (standard library only), long polling, one installer (`install-eris-bot.sh`) with `--uninstall`, settings kept on upgrade, `eris-bot update` through the bot's proxy.
- Ways to Telegram: direct, HTTP proxy (CONNECT), SOCKS5 (DNS on the proxy), `auto` (direct first, then `proxy_list` in order); live test with latency.
- Menus: dashboard, Marzban (users, search, one user with extend/data/reset/disable/delete, subscription link and QR, new user wizard, nodes), services (state, logs, restart), ERIS firewall (with the 60 s keep button), backups, alerts, settings, server reboot/power off.
- Coloured buttons (Bot API 9.4 `style`), with a fallback to plain buttons that is remembered.
- Security: allowed chat IDs only, owner/viewer roles, red confirmation buttons that expire after 60 s (two steps for reboot, power off and restore), short random button ids, no secrets in the log, rate limit.
- Backups of each part (never the full one), encrypted with `eris backup encrypt`, in pieces over 45 MB, retried with backoff and resumed after a restart; after the nightly backup or at a set time.
- Alerts only on change (ERIS dashboard alerts, Marzban users and nodes, reboot, new SSH address, certificates, public IP, backups, fans) with a 10-minute rule, mute and a daily report.
- Fans (`eris-fan.service`): discovery of every pwm, auto/manual/curve modes, protection at 75 °C (CPU) / 80 °C (GPU), stall alert, back to the BIOS when it stops; finding a Super I/O driver with sensors-detect; performance profiles (fan curve, governor, min frequency, swappiness) that only touch what exists; a BIOS overclocking guide; a stress test that stops at 85 °C.
- QR codes in pure Python (checked against a reference encoder).
