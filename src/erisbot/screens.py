"""All screens of the bot (registered on the App)."""
from . import s_alerts, s_backup, s_fans, s_main, s_mz, s_ops
from .s_main import dashboard_text  # noqa: F401  (daily report)


def register(app):
    for m in (s_main, s_mz, s_ops, s_backup, s_alerts, s_fans):
        m.register(app)


def after_start(app):
    s_fans.after_start(app)
