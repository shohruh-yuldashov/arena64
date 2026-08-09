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

`AccountRestrictionGate` (A64-024.6) is the second, and it has a consumer:
`auth` asks it at every credential boundary. It is the published form of
the arrow `domain-model.md` §6 draws — `ADMIN --> "sanctions gate" --> AUTH`
— and it too can only read. Restricting an account is `ModerationService`'s,
which is not published and never will be.
"""

from app.modules.admin.domain.roles import AdminRole
from app.modules.admin.public.authority import AdminAuthority
from app.modules.admin.public.moderation import AccountRestriction, AccountRestrictionGate

__all__ = ["AccountRestriction", "AccountRestrictionGate", "AdminAuthority", "AdminRole"]
