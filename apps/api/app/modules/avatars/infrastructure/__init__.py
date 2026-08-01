"""The `avatars` infrastructure layer — adapters realising the ports in
`application/ports.py`.

One adapter, and it is the only module in the codebase that imports PIL —
see `pillow_processor.py` on why an image decoder is worth confining to one
file.

No storage adapter lives here: object storage is a platform capability
(`app/storage/`), not this module's, which is what lets `profiles` read
URLs out of the same provider without depending on `avatars`.
"""

from app.modules.avatars.infrastructure.pillow_processor import PillowImageProcessor

__all__ = ["PillowImageProcessor"]
