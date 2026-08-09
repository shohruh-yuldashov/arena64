"""What an admin route returns. A64-024.1 §5."""

from pydantic import BaseModel, ConfigDict, Field

from app.modules.admin.domain.roles import AdminRole


class AdminSessionResponse(BaseModel):
    """`GET /admin/me` — the minimum `apps/admin` needs to render a shell.

    **Four fields, and the omissions are the design.** No email, no
    password state, no session or refresh material, no last-sign-in, no
    counts. The admin client needs to know who it is signed in as and what
    it may do; everything else would be PII travelling to a privileged
    surface for no reason (§5).

    `roles` rather than a boolean, so a second role added later needs no
    new field and no client change — the shell already branches on
    membership.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="The administrator's opaque account id (DM-06).")
    username: str = Field(description="What an operator recognises them by.")
    display_name: str | None = Field(default=None)
    roles: list[AdminRole] = Field(
        description="Every role held right now. Read from storage per request, "
        "so a revoked role disappears on the next call."
    )


__all__ = ["AdminSessionResponse"]
