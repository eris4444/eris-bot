"""Screens and buttons. A screen is one message (text + inline keyboard) that is edited in place.

Button colours (Bot API 9.4, field "style" of InlineKeyboardButton / KeyboardButton):
  success = green  safe and positive actions (back up, start, create, normal confirmations)
  primary = blue   menus and details
  danger  = red    anything dangerous or irreversible
"""
SUCCESS, PRIMARY, DANGER = "success", "primary", "danger"

MENU_TEXT = "🏠 منو"
DASH_TEXT = "📊 داشبورد"


class Btn:
    """An inline button. `action`/`args` run a registered action; `confirm` (text) asks first with a
    red button, `steps=2` asks twice. `url` makes a link button."""

    def __init__(self, text, action=None, *args, style=None, url=None, confirm=None, steps=1, ttl=None, once=False):
        self.text, self.action, self.args = text, action, list(args)
        self.style, self.url, self.confirm, self.steps, self.ttl = style, url, confirm, steps, ttl
        self.once = once                 # a random id that works one time within `ttl` (60 s) seconds
        if confirm and style is None:
            self.style = DANGER


class Screen:
    def __init__(self, text, rows=None, back=None, refresh=None, notice=None):
        self.text = text
        self.rows = [r for r in (rows or []) if r]
        self.back = back              # (action, args) or None
        self.refresh = refresh        # (action, args) or None
        self.notice = notice          # short text for answerCallbackQuery


def nav_row(refresh=None, back=None):
    row = []
    if refresh:
        row.append(Btn("🔄 به‌روزرسانی", refresh[0], *refresh[1], style=PRIMARY))
    if back:
        row.append(Btn("🔙 برگشت", back[0], *back[1], style=PRIMARY))
    return row


def grid(buttons, per_row=2):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def pager(action, args, page, pages):
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(Btn("◀️ قبلی", action, *(list(args) + [page - 1]), style=PRIMARY))
    row.append(Btn("%s / %s" % (_fa(page + 1), _fa(pages)), "noop", style=None))
    if page < pages - 1:
        row.append(Btn("بعدی ▶️", action, *(list(args) + [page + 1]), style=PRIMARY))
    return row


def _fa(x):
    from .util import fa
    return fa(x)


def reply_keyboard():
    return {"keyboard": [[{"text": MENU_TEXT, "style": PRIMARY}, {"text": DASH_TEXT, "style": PRIMARY}]],
            "resize_keyboard": True, "is_persistent": True}


def markup(rows, make_id):
    """Inline keyboard JSON; make_id(btn) gives the callback id of a button."""
    out = []
    for row in rows:
        r = []
        for b in row:
            d = {"text": b.text[:64]}
            if b.url:
                d["url"] = b.url
            else:
                d["callback_data"] = make_id(b)
            if b.style:
                d["style"] = b.style
            r.append(d)
        if r:
            out.append(r)
    return {"inline_keyboard": out}
