"""The `avatars` domain layer — what an acceptable image is, and where one
lives. Imports nothing with a framework under it (architecture.md §8), and
notably no Pillow: deciding whether bytes are acceptable needs only the
bytes.
"""
