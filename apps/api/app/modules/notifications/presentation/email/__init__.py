"""Notification email rendering — A64-021.5 §13.

`presentation`, because a subject line is a rendering of a fact in a
language, and the fact is `NotificationRecord`. The *application* layer holds
`NotificationEmailRenderer`, a port this satisfies — see `templates.py`.
"""

from app.modules.notifications.presentation.email.templates import (
    TemplateEmailRenderer,
    UnsupportedEmailTemplate,
    render,
)

__all__ = ["TemplateEmailRenderer", "UnsupportedEmailTemplate", "render"]
