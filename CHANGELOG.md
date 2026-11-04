# Changelog

## 1.2.0
- **🤖 Claude and Codex:** plan usage (5-hour and weekly windows with reset times, plan name), the model and effort of each (Codex: its own model list and efforts; Claude: opus/sonnet/haiku or any model id), and chatting: every message continues the conversation (`claude -p --resume`, `codex exec resume`) until «🆕 گفتگوی تازه»; typing indicator, ⏹ stop, long answers split, Markdown shown as Telegram formatting. The chat runs with no tools (Claude `--tools ""`, Codex read-only sandbox) in an empty folder. `/ai`.
- **📁 NAS files:** log in with a user of the server (its own password, checked with libcrypt against /etc/shadow; the message is deleted at once, nothing stored; 5 wrong tries = 15 minutes locked; the session ends after 30 idle minutes). Browse the OMV shared folders, upload any number of files (documents, photos, videos, audio) into any folder, send NAS files to Telegram (in 45 MB pieces when bigger), new folder, rename, delete. Every operation runs as that user (`setpriv` + `nasop.py`), so the server's own permissions decide. A file sent to the bot outside a question asks where to save it. `/files`.
- **🌤 Weather** (Open-Meteo, no key): city search in Persian or English, or a sent location; now, the next hours and 5 days; optional daily message at a set time. `/weather`.
- **New Marzban user / one user:** any amount can be typed besides the buttons — data `15`, `2.5`, `500 مگ`, `1 ترا`; time `45`, `2 ماه`, `12 ساعت`, `1 سال`; `0` = unlimited; Persian digits work. On a user: «✍️ روز دلخواه» / «✍️ حجم دلخواه» (negative takes away; for an unlimited user it starts from now / from the current usage).
- **Fix: false "public IP changed" alerts.** The IP now counts only when at least two services agree, a change only after two checks in a row, and with **📍 IP ثابت** (Settings → 📍 IP عمومی) the alert is only about the IP being something else. ifconfig.me (403 now) was replaced.
- **Fix: pressing a button twice turned the button colours off** ("API رنگ را رد کرد"): an unchanged edit ("message is not modified") was taken for Telegram refusing colours. Now only a real refusal does that, and a wrong old refusal is undone on start.

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
