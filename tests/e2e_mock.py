#!/usr/bin/env python3
"""End-to-end check for the install test: a mock Bot API on PORT; send /start from CHAT and wait until the
installed eris-bot (running under systemd) answers with its menu, then press the dashboard button.
  e2e_mock.py PORT CHAT [SECONDS]"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mocks  # noqa: E402

port, chat = int(sys.argv[1]), int(sys.argv[2])
limit = float(sys.argv[3]) if len(sys.argv) > 3 else 90
tg = mocks.MockTelegram(port=port)
print("mock Bot API on %s" % tg.url(), flush=True)


def wait(pred, what):
    end = time.time() + limit
    while time.time() < end:
        for m, p in list(tg.sent):
            if pred(m, p):
                return p
        time.sleep(0.3)
    print("FAIL: no %s; calls: %s" % (what, sorted(set(tg.calls))), flush=True)
    sys.exit(1)


wait(lambda m, p: m == "getUpdates", "getUpdates from the bot")
tg.text(666, "/start")                       # a stranger: no answer
tg.text(chat, "/start")
menu = wait(lambda m, p: m == "sendMessage" and "منوی ERIS" in p.get("text", ""), "menu")
kb = menu["reply_markup"]
styles = {b.get("style") for row in kb["inline_keyboard"] for b in row}
print("menu ok, buttons: %s" % [b["text"] for row in kb["inline_keyboard"] for b in row], flush=True)
assert styles == {"primary"}, styles
assert not any(int(p.get("chat_id", 0)) == 666 for m, p in tg.sent if m == "sendMessage"), "answered a stranger"
data = mocks.find_button(kb, "داشبورد")
mid = max(k[1] for k in tg.messages if k[0] == chat)
tg.press(chat, mid, data)
dash = wait(lambda m, p: m == "editMessageText" and "داشبورد" in p.get("text", ""), "dashboard")
print("dashboard ok:\n" + dash["text"], flush=True)
print("E2E OK", flush=True)
