"""Object-storage adapters — the implementations behind
`app.core.storage.StorageProvider`.

A sibling of `app/database/`, and for the same reason it is not inside a
module: object storage is a platform capability, not any one context's
property. `avatars` writes to it, `profiles` reads URLs out of it, and a
future exports feature (architecture.md §134 names "avatars, exports" as
the object-storage workload) will too.

One provider today — `LocalStorageProvider`, development only. The
S3/R2/MinIO/GCS providers A64-012.2 anticipates are new classes in this
package plus a branch in one dependency factory; nothing above the port
changes, which is what makes that claim checkable rather than aspirational.
"""

from app.storage.local import LocalStorageProvider

__all__ = ["LocalStorageProvider"]
