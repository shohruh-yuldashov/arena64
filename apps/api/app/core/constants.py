"""Platform-wide constants with no business meaning.

Deliberately short. A constant belongs here only once a second consumer
needs it (CLAUDE.md §2.6/§1 rule 7) — module-specific constants (rating
K-factors, abandonment thresholds, and the like) belong in that module's
own settings section (dependency-injection.md §2.1), not here.
"""

from typing import Final

# ASGI middleware (app.common.middleware) reads the inbound header under
# this name and binds it into the logging context (app.common.context);
# the same name is echoed back on every response so a client can correlate
# its own logs with the server's.
REQUEST_ID_HEADER: Final[str] = "X-Request-Id"

# Propagates the causal chain across process and transport boundaries —
# services.md §8.2. Distinct from the request id: one correlation id can
# span many requests (and, once workers exist, many Celery tasks).
CORRELATION_ID_HEADER: Final[str] = "X-Correlation-Id"

API_PREFIX: Final[str] = "/api"
API_V1_PREFIX: Final[str] = "/v1"

HEALTH_PATH: Final[str] = "/health"
