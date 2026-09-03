"""Object-storage adapters — the implementations behind
`app.core.storage.StorageProvider`.

A sibling of `app/database/`, and for the same reason it is not inside a
module: object storage is a platform capability, not any one context's
property. `avatars` writes to it, `profiles` reads URLs out of it, and a
future exports feature (architecture.md §134 names "avatars, exports" as
the object-storage workload) will too.

Two providers. `LocalStorageProvider` is development only and refuses to
construct in a deployed tier; `S3StorageProvider` is what a deployed tier
uses, and covers AWS S3, Cloudflare R2, MinIO and Backblaze B2 alike
because they share one signing scheme.

A64-012.2 predicted the shape of this: *"a new class in this package plus a
branch in one dependency factory; nothing above the port changes."* That
held exactly — `_configure_storage` gained a branch and no service, domain
type or schema moved.
"""

from app.storage.local import LocalStorageProvider
from app.storage.s3 import S3StorageProvider

__all__ = ["LocalStorageProvider", "S3StorageProvider"]
