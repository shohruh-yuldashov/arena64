"""The ORM model registry — the one place that imports every module's
SQLAlchemy models, so `Base.metadata` is complete for whoever needs it.

Two consumers, and both break silently rather than loudly without this:

  `alembic/env.py`           autogenerate compares `Base.metadata` against
                             the live database. A model whose module was
                             never imported is simply absent from the
                             metadata, so Alembic concludes the table
                             should be *dropped* — or, on a first run,
                             never creates it and reports "no changes
                             detected". Both are silent wrong answers.
  test fixtures              `Base.metadata.create_all` has the same gap.

This is deliberately an explicit list rather than a `pkgutil.walk_packages`
scan of `app.modules`. Import side effects that depend on filesystem
traversal order are the kind of implicit behaviour CLAUDE.md §2.1 rules
out, and a scan would also silently pick up a half-finished module someone
is still writing. One line per module, added when the module adds its
first table, is a cost of exactly one line.
"""

# Each import is load-bearing: it registers that module's tables onto
# `app.database.base.Base.metadata` as a side effect of class definition.
from app.modules.auth.infrastructure import models as _auth_models  # noqa: F401
from app.modules.friends.infrastructure import models as _friends_models  # noqa: F401
from app.modules.game.infrastructure import models as _game_models  # noqa: F401
from app.modules.matchmaking.infrastructure import models as _matchmaking_models  # noqa: F401
from app.modules.rating.infrastructure import models as _rating_models  # noqa: F401
from app.modules.statistics.infrastructure import models as _statistics_models  # noqa: F401
from app.modules.users.infrastructure import models as _users_models  # noqa: F401
from app.modules.users.infrastructure.search_ddl import register_search_ddl

# A64-013.7. Not a module: `platform.outbox` and `platform.processed_event`
# belong to the platform rather than to a bounded context (database.md
# §232), so this import sits apart from the four above rather than pretending
# to be a fifth module.
from app.platform.outbox import models as _outbox_models  # noqa: F401,E402

# A64-013.1. Schema objects a table definition cannot express — two
# extensions, a function and two expression indexes — attached to
# `after_create` so that `create_all` produces a *searchable* schema and not
# merely a complete one.
#
# An explicit call rather than an import side effect, unlike the three
# lines above. Those are unavoidable: a mapped class registers itself when
# Python defines it. This is not, and a module whose import silently
# attaches event listeners is the implicit behaviour CLAUDE.md §2.1 rules
# out — one that would fire twice if anything ever imported it again.
register_search_ddl()

__all__: list[str] = []
