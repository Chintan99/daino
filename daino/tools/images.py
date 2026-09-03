"""Reading images the agent is allowed to see.

Everything the agent could look at was text, so a screenshot in the repository,
an exported mockup, or a rendered chart was a file it knew existed and could say
nothing about. ``read_file`` reported it as undecodable, which is true and
useless.

Two constraints shape what is here. First, an image goes to the provider as
base64 inside the request body, so its *encoded* size is what counts against the
window and the bill — a 4 MB screenshot is roughly 5.5 MB of request. Second, the
formats models actually accept are a short list, and sending anything else is a
rejected request rather than a degraded answer. Both are enforced here rather
than discovered at the provider.
"""

from __future__ import annotations

import base64
from pathlib import Path

from daino.schemas import ImagePart, ToolResult

#: Formats every major vision model accepts. Deliberately short: a format that
#: only some providers take would work until the router chose a different one.
SUPPORTED_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

#: Ceiling on one image's bytes on disk. Base64 inflates by about a third, so
#: this is roughly 6.7 MB on the wire — already generous for a screenshot, and
#: well inside the per-request limits providers impose.
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def is_image(relative: str | Path) -> bool:
    return Path(relative).suffix.casefold() in SUPPORTED_MEDIA_TYPES


def media_type_for(relative: str | Path) -> str:
    return SUPPORTED_MEDIA_TYPES.get(Path(relative).suffix.casefold(), "")


def load_image(root: Path, relative: str, *, description: str = "") -> ToolResult:
    """Read one image inside the project, or explain why it cannot be read."""
    normalized = str(relative).strip().replace("\\", "/").lstrip("/")
    target = (root / normalized).resolve()
    if not target.is_relative_to(root.resolve()):
        return ToolResult(
            tool="read_image",
            success=False,
            error=f"{normalized} is outside the project.",
        )
    media_type = media_type_for(normalized)
    if not media_type:
        return ToolResult(
            tool="read_image",
            success=False,
            error=(
                f"{normalized} is not an image format a model can read. Supported: "
                + ", ".join(sorted(SUPPORTED_MEDIA_TYPES))
            ),
        )
    if not target.is_file():
        return ToolResult(
            tool="read_image", success=False, error=f"{normalized} does not exist."
        )
    size = target.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return ToolResult(
            tool="read_image",
            success=False,
            error=(
                f"{normalized} is {size / 1_048_576:.1f} MB, over the "
                f"{MAX_IMAGE_BYTES / 1_048_576:.0f} MB limit for one image. "
                "Resize or crop it first."
            ),
        )
    try:
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    except OSError as exc:
        return ToolResult(tool="read_image", success=False, error=f"{normalized}: {exc}")
    return ToolResult(
        tool="read_image",
        success=True,
        data={
            "path": normalized,
            "media_type": media_type,
            "bytes": size,
            "image": ImagePart(
                media_type=media_type,
                data=encoded,
                description=description or normalized,
            ).model_dump(mode="json"),
        },
    )
