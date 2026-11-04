# eris-bot

A Telegram bot for one home server: dashboard, Marzban users, services, the ERIS firewall, encrypted backups of each part of the server, alerts, fans and performance profiles. The bot speaks Persian; this README is in English.

- Plain Python 3 standard library: no pip, no aiogram or python-telegram-bot, no binary wheels (it runs on old CPUs without SSE4.2/POPCNT/AVX).
- Long polling (`getUpdates`), so it works behind NAT. Telegram is reached directly or through an HTTP or SOCKS5 proxy, picked automatically.
- One self-contained installer, `install-eris-bot.sh`, like [ERIS Console](https://github.com/eris4444/eris-console).
- Works with ERIS Console 2.5 (`eris backup … --json`, `eris fw`, the dashboard alerts) and Marzban 0.8.4. Without ERIS or Marzban their menus are hidden and the rest works.

## Install

```bash
curl -fsSL https://github.com/eris4444/eris-bot/releases/latest/download/install-eris-bot.sh -o /tmp/i.sh && sudo bash /tmp/i.sh && sudo eris-bot setup
```

If GitHub is slow or filtered, download the file through your proxy (for example `curl -x http://127.0.0.1:1091 …`) and run `sudo bash install-eris-bot.sh`.

The installer:
- needs Debian (made for Debian 13 trixie, Python 3.13) and root;
- installs only missing packages (`python3 openssl ca-certificates`) and never upgrades anything;
- copies every file it replaces to `/var/backups/eris-bot/<time>/` first;
- keeps `/etc/eris-bot/config.json`, the backup password and the state on an upgrade;
- options: `--uninstall`, `--no-start`, `--no-selftest`.

| Path | What |
| --- | --- |
| `/usr/local/bin/eris-bot` | the command |
| `/usr/local/lib/eris-bot/erisbot/` | the program |
| `/etc/eris-bot/config.json` (600) | token, chat IDs, proxy, schedules, path of the backup password |
| `/etc/eris-bot/backup.pass` (600) | the backup password |
| `/etc/eris-bot/fans.json`, `profile.json` | fan modes, the chosen performance profile |
| `/var/lib/eris-bot/` (700) | state: alerts, send queue, buttons, original fan/CPU settings |
| `eris-bot.service` | the bot (`Restart=always`, `After=network-online.target`) |
| `eris-fan.service` | fan control; runs while the bot runs (`BindsTo=eris-bot.service`) |

Remove it with `sudo eris-bot-uninstall` (or `bash install-eris-bot.sh --uninstall`): the services stop (the fans go back to the BIOS), the CPU settings of the profile go back, and `config.json` and the state are kept in `/var/backups/eris-bot/uninstall-<time>/`.

Update: `sudo eris-bot update` downloads the latest release through the bot's own proxy settings.

## `eris-bot setup`

1. Make a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. `sudo eris-bot setup`
   - asks for the token; what you type is not shown, and the token is never printed;
   - asks for the proxy (Enter keeps `auto`) and tests the connection;
   - asks for your chat ID, or finds it: send any message to your bot while it waits;
   - offers to set the backup password;
   - saves `config.json` (mode 600) and (re)starts the service.
3. Send `/start` to the bot. The 🏠 منو button stays at the bottom of the chat.

Other commands:

```
eris-bot status                 what is set up and running
eris-bot test                   getMe directly and through every proxy, with the latency
eris-bot proxy [off|auto|URL|test]
eris-bot password               set the backup password
eris-bot logs [-f] [-n N]       journalctl -u eris-bot -u eris-fan
eris-bot update [--check|--force]
```

To add more people: ⚙️ تنظیمات → 👥 chat IDها. A new chat starts as a viewer (sees everything, changes nothing); make it an owner there. `roles` in `config.json` holds the same (`"owner"` or `"viewer"`).

## Proxy

Telegram (and sometimes GitHub) is filtered or slow in Iran, so the way to Telegram is a setting:

| `proxy` | |
| --- | --- |
| `auto` (default) | a direct connection first; if it fails, every proxy of `proxy_list` in order; the first that answers `getMe` is used, and it is checked again after a failure |
| `off` | direct only |
| `http://[user:pass@]host:port` | an HTTP proxy (`CONNECT` for HTTPS) |
| `socks5://[user:pass@]host:port` | SOCKS5, with the DNS lookup on the proxy side |

`proxy_list` defaults to `http://127.0.0.1:1090`, `socks5://127.0.0.1:1090` (a mixed port) and `http://127.0.0.1:1091`. Marzban (`https://127.0.0.1:8000`) never goes through a proxy. The public-IP alert asks a few services directly, without a proxy.

⚙️ تنظیمات → 🌐 پروکسی and `eris-bot test` test each way live and show the latency.

Uploads stream from disk (multipart, never the whole file in memory) with separate write and read timeouts (`upload.write_timeout`, `upload.read_timeout`). If big uploads stall on your line, choose smaller pieces in ⚙️ تنظیمات → 🧩 اندازه‌ی تکه (10/20/30/45 MB).

## Menus

| Menu | |
| --- | --- |
| 📊 داشبورد | uptime; CPU and disk temperatures; CPU, memory and disk use; Marzban users connected right now (traffic in the last 2 minutes, by name); today's traffic (since local midnight); nodes; active alerts; the last backup of each part |
| 👥 مرزبان | users (pages, status filter; send a name to search); each user: usage, limit, expiry, last online, subscription link and QR; +7/+30/+90 days, +10/+50/+100 GB, reset usage, disable/enable, delete; a new user in four steps (name, data, days, inbounds); nodes with reconnect |
| 🖥 سرویس‌ها | state of `ssh x-ui haproxy nginx xray-site smbd docker eris-guard eris-dashboard claude-f-web smartmontools` and of the containers `marzban-marzban-1`, `marzban-node-*`, `jellyfin`; the last 30 log lines; restart |
| 🛡 فایروال | `eris fw`; open/close a port, allow/deny an address, remove a rule; after a change ✅ نگه دار with a 60 s countdown, else ERIS rolls it back. SSH is never opened to the Internet from the bot; changes ERIS calls risky need a second red confirmation (`--force`) |
| 💾 بکاپ | see below |
| 🔔 هشدارها | active alerts, history, each kind on/off, silence for 1 h, 8 h or until 08:00 |
| 🌀 فن و عملکرد | see below |
| ⚙️ تنظیمات | proxy, chat IDs and roles, backup schedule and parts, backup password (set only, never shown), daily report, button colours, piece size |
| 🔁 سرور | reboot and power off (two confirmations) |

Every screen is one message that is edited in place, with 🔄 به‌روزرسانی and 🔙 برگشت.

**Button colours** use the `style` field of `InlineKeyboardButton` / `KeyboardButton` (Bot API 9.4): `success` (green) for safe, positive actions, `primary` (blue) for menus and details, `danger` (red) for anything dangerous or irreversible. Telegram apps older than February 2026 show plain buttons. If the API ever refuses the field, the bot sends the message again without colours once and remembers it (⚙️ تنظیمات → 🎨 to try again). The bot does not use `icon_custom_emoji_id` (it needs a Fragment username or Telegram Premium); the emoji are in the button text.

## Alerts

Sources: the alerts of the ERIS dashboard (`alerts()` in `erisd.py`: CPU/disk heat, SMART, disks over 90 %, failed services, the full backup), plus:
- a Marzban user with less than 3 days or 10 % of the data left; a user that became limited or expired;
- a node that disconnects or comes back; the panel not answering;
- the server rebooted (a new boot id);
- an SSH login from an address not seen before (`journalctl -u ssh`, `Accepted … for USER from IP`);
- a certificate in `/etc/xray-site/certs/*/fullchain.pem` with less than 10 days left;
- a new public IP;
- a part backup that failed, or a backup that could not be sent;
- fans: protection at 100 %, a fan that does not turn.

An alert is sent when it starts and when it is over (✅). A change within 10 minutes of the last message about the same alert is held back and sent after the 10 minutes if it is still true, so a flapping problem cannot flood the chat. A daily report is optional (⚙️ تنظیمات → 🗓).

## Backups

ERIS Console 2.5 makes a full backup (it stays on the flash disk) and one backup of each part (`marzban`, `x-ui`, `xray-site`, `jellyfin`, `eris`, `claude-f-web`, `system`). **Only the part backups are ever sent; the full backup never leaves the server.**

- **When:** after the nightly backup (`after_nightly`, the default), or at a time of its own (a fresh backup of the parts at that time), or off. Which parts are sent is a setting. By hand: 📤 بکاپ و ارسال همین الان (all parts or one), and 📂 بکاپ‌های روی فلش to send any older copy again.
- **Encryption:** every file is encrypted first, with `eris backup encrypt` (`openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt`, the password read from a file so it never shows in `ps`). The temporary `.enc` file is in `/var/tmp/eris-bot/` and is deleted after sending. Nothing is sent while no password is set.
- **Big files:** Telegram bots can send at most 50 MB per file, so a file over 45 MB (or the piece size you chose) is sent as `NAME.enc.part01`, `.part02`, … Each piece is retried with backoff (5 s … 15 min); what was sent is saved after every piece, so after a lost connection or a restart the bot goes on with the next piece.
- **First** a summary message lists the parts and how each one went, **then** the files. Each caption has the part, date, size, the sha256 of the original archive, the number of files and databases, and how to open it.

Open a backup (on any computer with openssl):

```bash
cat NAME.tar.enc.part* > NAME.tar.enc          # only when it came in pieces
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in NAME.tar.enc -out NAME.tar
sha256sum NAME.tar                              # compare with the caption
tar -tf NAME.tar                                # files.tar.gz, databases/, manifest.json, RESTORE.txt
```

Put a backup back on the server: `eris restore --part x-ui FILE` (see ERIS Console), or from the bot: 📂 بکاپ‌های روی فلش → a copy → ♻️ بازگردانی (two red confirmations). ERIS saves the present state of that part first.

Keep the backup password somewhere safe: without it nobody can open the files, you included.

## Fans and performance profiles

What the bot can and cannot do on a G41 / ICH7 board with a Core 2 (Pentium E5700, later a Core 2 Quad Q9550):

- **Fans:** the bot finds every `pwmN` in `/sys/class/hwmon/*` (with `pwmN_enable`, `fanN_input` and the chip name) and shows only fans that really exist. Today that is the GTX 650 fan (`nouveau`, `pwm1`). The CPU and case fans need a Super I/O driver (it87, nct6775, w83627ehf, f71882fg …), and none is loaded: 🔍 تشخیص کنترلر فن CPU runs `sensors-detect --auto`, offers to load a suggested driver (and `it87 force_id=…`, separately) and keeps it in `/etc/modules-load.d/eris-bot-fans.conf`. If nothing is found, the bot says so: then only the BIOS (Smart Fan) controls that fan.
  - 🪄 پیدا کردن خودکار (1.1) does it all by itself: it tries the Super I/O drivers one by one (each checks the chip ID; nothing is forced), retries it87 with `ignore_resource_conflict=1` when the BIOS holds the ports for ACPI, keeps the driver that works, and turns each new pwm to 100 % and 35 % to see which fan really follows it. When the chip is held by ACPI and nothing else helps, a button adds `acpi_enforce_resources=lax` to GRUB (two confirmations, a backup of `/etc/default/grub`, then a reboot). Also set the CPU fan in the BIOS to manual/disabled Smart Fan so the BIOS does not fight Linux; a 3-pin fan needs DC (voltage) mode.
- **Modes:** 🔁 automatic (the original `pwm_enable`), 🎚 manual (30–100 %, ±5 %), 📈 curve (temperature → percent, 3 °C hysteresis, gradual steps). The GPU fan follows the GPU's own temperature, CPU/case fans the hottest core. Percent becomes a pwm value with `pwmN_min`/`pwmN_max` when they exist, else 0–255.
- **Safety, always on:** manual is never below 25 %; CPU ≥ 75 °C or GPU ≥ 80 °C puts that fan at 100 % at once with a red alert; a fan at 0 RPM with more than 30 % power raises an alert. `eris-fan.service` reads the temperatures every 2 s (systemd watchdog); when it or the bot stops or crashes, every fan gets its original `pwm_enable` back (`ExecStopPost=eris-bot fan-restore`).
- **Profiles:** 🤫 quiet (gentle curve, `powersave`), ⚖️ balanced (normal curve, `schedutil`/`ondemand`), 🚀 performance (steep curve, `performance`, `vm.swappiness=10`), 🔥 maximum (100 %, `performance`, minimum frequency = maximum), and your own (curve + governor + swappiness). A profile changes only what exists on the server and shows exactly what changed; the rest is marked "not available on this server" (today: cpufreq, because EIST is off or unsupported). The chosen profile is applied again at boot; ↩️ puts everything back to the system defaults.
- **Overclocking:** raising the FSB or the voltage on Core 2 / G41 is only safe from the BIOS. Tools like setfsb write to the clock chip directly and can hang the server or corrupt data, so the bot never does that. 📖 راهنمای اورکلاک از BIOS explains the steps for the Q9550 (FSB 333 → 366 → 380, Vcore, under 70 °C under load) and warns about the cheap 230 W power supply.
- **Stress test:** 🔥 تست پایداری for 10, 30 or 60 minutes, all cores busy with Python processes; every 30 s the temperature, fan speeds and frequency, with a small text chart; it stops by itself at 85 °C. The result shows the highest temperature, the throttle count and whether the server stayed up (a reboot during the test is reported after the restart).

## Security

- Only the chat IDs in `allowed_chat_ids` get any answer; everything else is only logged (once an hour per chat). Owners can do everything, viewers can only look.
- Dangerous actions (restarting a service, rebooting, powering off, firewall changes, deleting a user, restoring a backup) need a red confirmation button that works once and only for 60 seconds; reboot, power off and restore need two. The action itself refuses to run without that confirmation, whatever button data arrives.
- `callback_data` is only a short id (well under Telegram's 64 bytes) mapped to the action in the bot's memory/state, bound to the chat that got the button, with an expiry. Unknown, expired or foreign ids do nothing.
- The token, passwords, keys and full subscription links are never logged. A subscription link is shown only as the answer to its own button, to an owner. The password message is deleted from the chat right after it is read.
- Rate limit: 20 updates per 10 seconds per chat.
- `config.json`, `backup.pass` and the state folder are readable by root only.

## Troubleshooting

| Problem | Try |
| --- | --- |
| The bot does not answer | `eris-bot status`, `eris-bot logs`; is your chat ID in `allowed_chat_ids`? |
| `no way to Telegram works` | `eris-bot test` shows which way fails; check the local proxy (`ss -ltnp | grep 109`); `eris-bot proxy socks5://127.0.0.1:1090` |
| `409` in the log | another program polls the same bot token; stop it |
| `401` in the log | the token was revoked: `eris-bot setup` |
| Uploads stop half way | smaller pieces (⚙️ تنظیمات → 🧩); the job goes on by itself; `eris-bot logs` shows each retry |
| No backup menu | ERIS Console 2.5 is not installed (`eris version`) |
| No Marzban menu | `/opt/marzban/.env` is missing |
| Buttons without colours | an old Telegram app, or the API refused them (⚙️ تنظیمات → 🎨) |
| Fans back to BIOS control | `systemctl status eris-fan`; it stops whenever eris-bot stops |

## Development

```bash
python3 -m unittest discover -s tests -v                    # everything against local mocks
ERIS_CONSOLE_SRC=../eris-console/src python3 -m unittest discover -s tests -p test_real_eris.py
bash src/build.sh "$PWD/install-eris-bot.sh"                # the single-file installer
bash tests/ci-install.sh                                     # real install in a debian:trixie container with systemd
```

The tests run a local Bot API (getUpdates, sendMessage, editMessageText, answerCallbackQuery, sendDocument, sendPhoto, getMe; 429/`retry_after`, 502, a connection cut during an upload, a refused `style` field), a Marzban 0.8.4 API over HTTPS (with the DELETE 500 bug), HTTP-CONNECT and SOCKS5 proxies (with and without a password), a fake `eris` command and a fake `/sys` and `/proc` (hwmon with pwm/fan/temp, cpufreq with and without). `api_base` in `config.json` points the bot at any Bot API server. GitHub Actions runs it all on `debian:trixie`, with the real ERIS Console, and installs the package in a systemd container; a `v*` tag makes the release.
