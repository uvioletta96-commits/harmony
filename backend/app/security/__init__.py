"""Security subsystem.

Layered controls, each usable on its own:

* :mod:`validators`          — every input validated before it reaches the ORM
* :mod:`xss`                — markup neutralised on ingest, escaped on render
* :mod:`content_moderation` — local screening plus an optional remote classifier
* :mod:`captcha`            — self-hosted proof-of-work or a hosted provider
* :mod:`spam`               — scoring-based bot and spam detection
* :mod:`rate_limit`         — sliding-window request throttling
* :mod:`decorators`         — authentication, CSRF and role enforcement
"""

from . import content_moderation, spam, validators, xss
from .decorators import (
    admin_required,
    auth_optional,
    auth_required,
    csrf_protect,
    current_user,
    current_user_id,
    moderator_required,
    roles_required,
    verify_csrf,
)

# ``rate_limit`` names both the submodule and the decorator. The import below
# binds the decorator, which is what callers want; the submodule is still
# reachable as ``app.security.rate_limit`` because importing a submodule always
# sets it as an attribute of its package.
from .rate_limit import Limit, enforce, rate_limit

__all__ = [
    "Limit",
    "admin_required",
    "auth_optional",
    "auth_required",
    "content_moderation",
    "csrf_protect",
    "current_user",
    "current_user_id",
    "enforce",
    "moderator_required",
    "rate_limit",
    "roles_required",
    "spam",
    "validators",
    "verify_csrf",
    "xss",
]
