"""Process entrypoint for the HTTP runtime profile (architecture.md AD-02).

`uvicorn main:app` in deployed environments; `python main.py` for local
development. The gateway, worker, and clock profiles are separate
entrypoints under the same distribution (services.md §1) and are out of
scope for this bootstrap — see the task's closing recommendations.
"""

import uvicorn

from app.app_factory import create_app
from app.config.settings import get_settings

app = create_app()

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host="0.0.0.0",  # noqa: S104 — bound inside a container; the edge terminates TLS (architecture.md §3)
        port=8000,
        reload=settings.environment.is_local,
        # logging is configured by app.common.logging, not uvicorn's default dictConfig
        log_config=None,
    )
