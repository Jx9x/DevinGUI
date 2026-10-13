"""Image formats that can be displayed as passive browser images."""

IMAGE_MIME_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif",
})


def image_mime_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    mime = value.strip().lower()
    return mime if mime in IMAGE_MIME_TYPES else None
