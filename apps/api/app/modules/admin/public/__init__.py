"""`admin`'s published surface — BE-03, architecture.md R-1.

Deliberately **read-only and tiny**. Another module may ask what authority
an account holds; nothing outside `admin` may grant or revoke one, because
a granting capability published to the platform is a granting capability
reachable from anywhere that already has a service locator.

`AdminAuthority` is the port an authorization guard programs against. The
guard lives in `admin.presentation` today, so nothing consumes this yet —
it exists because `fairplay` and a future moderation surface will ask the
same question, and publishing the read now is what stops the second caller
reaching for the repository.
"""

from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.public.authority import AdminAuthority

__all__ = ["AdminAuthority", "AdminRole"]
