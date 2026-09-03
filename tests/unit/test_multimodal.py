"""Images: reading them, sending them, and not sending them to a model that is blind."""

from __future__ import annotations

import base64
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from daino.agents.gateway import ModelGateway, _without_unusable_images
from daino.agents.loop import ToolLoop, _detail
from daino.config.models import ModelProfileConfig, ProviderConfig, Settings
from daino.model_router import ModelRole
from daino.providers.openai_compatible import _wire_message
from daino.schemas import AgentAction, ContextBundle, ImagePart, LLMResponse, Message, ToolCall
from daino.tools import ActionExecutor, EditTools
from daino.tools.images import MAX_IMAGE_BYTES, is_image, load_image
from daino.tui.screens.workspace import _image_references


def tiny_png() -> bytes:
    """A real 1x1 PNG, so the reader is exercised on actual bytes."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    body = zlib.compress(b"\x00\xff\xff\xff")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", body)
        + chunk(b"IEND", b"")
    )


class RecordingDatabase:
    def __init__(self) -> None:
        self.records: list[Any] = []

    @contextmanager
    def session(self) -> Iterator[RecordingDatabase]:
        yield self

    def add(self, record: Any) -> None:
        self.records.append(record)


def vision_settings(*, vision: bool) -> Settings:
    settings = Settings()
    settings.providers = {
        "cloud": ProviderConfig(type="openrouter", base_url="http://x/v1", model="m")
    }
    settings.models = {
        "profile": ModelProfileConfig(provider="cloud", model="m", vision=vision)
    }
    settings.routing = {"builder": "profile"}
    return settings


def test_only_a_user_message_with_images_uses_the_multipart_form() -> None:
    """A plain string is what every backend accepts, including the local ones."""
    assert _wire_message(Message(role="user", content="hello"))["content"] == "hello"
    wire = _wire_message(
        Message(
            role="user",
            content="what is wrong here",
            images=[ImagePart(media_type="image/png", data="QUJD")],
        )
    )
    assert wire["content"][0] == {"type": "text", "text": "what is wrong here"}
    assert wire["content"][1]["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_images_are_never_attached_to_a_tool_result() -> None:
    """Providers reject image parts on a tool message, so the form must not appear."""
    wire = _wire_message(
        Message(
            role="tool",
            content="{}",
            tool_call_id="c1",
            images=[ImagePart(media_type="image/png", data="QUJD")],
        )
    )
    assert wire["content"] == "{}"


def test_a_blind_model_gets_a_note_rather_than_a_failed_request() -> None:
    settings = vision_settings(vision=False)
    selection = ModelGateway(settings, RecordingDatabase()).router.select(  # type: ignore[arg-type]
        ModelRole.BUILDER
    )
    messages = [
        Message(role="system", content="be helpful"),
        Message(
            role="user",
            content="why is this button off-centre",
            images=[ImagePart(media_type="image/png", data="QUJD", description="the header")],
        ),
    ]
    stripped = _without_unusable_images(messages, selection)
    assert stripped[1].images == []
    assert "the header" in stripped[1].content
    assert "cannot read images" in stripped[1].content
    # The originals are untouched.
    assert messages[1].images


def test_a_vision_model_keeps_its_images_and_the_list_is_not_copied() -> None:
    settings = vision_settings(vision=True)
    gateway = ModelGateway(settings, RecordingDatabase())  # type: ignore[arg-type]
    selection = gateway.router.select(ModelRole.BUILDER)
    messages = [
        Message(role="user", content="x", images=[ImagePart(media_type="image/png", data="Q")])
    ]
    assert _without_unusable_images(messages, selection) is messages
    assert gateway.supports_vision(ModelRole.BUILDER) is True


def test_a_message_with_no_images_is_passed_straight_through() -> None:
    settings = vision_settings(vision=False)
    selection = ModelGateway(settings, RecordingDatabase()).router.select(  # type: ignore[arg-type]
        ModelRole.BUILDER
    )
    messages = [Message(role="user", content="plain")]
    assert _without_unusable_images(messages, selection) is messages


def test_reading_an_image_returns_base64_and_its_media_type(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(tiny_png())
    result = load_image(tmp_path, "shot.png")
    assert result.success
    assert result.data["media_type"] == "image/png"
    assert base64.b64decode(result.data["image"]["data"]) == tiny_png()


def test_a_non_image_is_refused_with_the_supported_list(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = load_image(tmp_path, "a.py")
    assert not result.success
    assert ".png" in (result.error or "")


def test_an_oversized_image_is_refused_before_it_is_encoded(tmp_path: Path) -> None:
    """Base64 inflates by a third, so the ceiling has to bind on disk."""
    (tmp_path / "huge.png").write_bytes(b"\x00" * (MAX_IMAGE_BYTES + 1))
    result = load_image(tmp_path, "huge.png")
    assert not result.success
    assert "over the" in (result.error or "")


def test_an_image_outside_the_project_is_refused(tmp_path: Path) -> None:
    result = load_image(tmp_path, "../../../etc/hosts.png")
    assert not result.success
    assert "outside the project" in (result.error or "")


def test_is_image_recognises_the_supported_extensions() -> None:
    assert is_image("a/b/shot.PNG")
    assert is_image("mock.webp")
    assert not is_image("notes.md")


@pytest.mark.asyncio
async def test_read_image_follows_its_observation_with_the_picture(tmp_path: Path) -> None:
    """The wire format takes images on a user message, not on a tool result."""
    (tmp_path / "shot.png").write_bytes(tiny_png())

    class Gateway:
        def __init__(self) -> None:
            self.seen: list[Message] = []

        def route_supports_tools(self, role: object, context: object = None) -> bool:
            return True

        async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
            for value in (*args, *kwargs.values()):
                if isinstance(value, list) and all(isinstance(i, Message) for i in value):
                    self.seen = list(value)
                    break
            name = "read_image" if len(self.seen) < 4 else "finish"
            arguments = (
                {"thought": "look", "path": "shot.png"}
                if name == "read_image"
                else {"thought": "done", "summary": "saw it"}
            )
            return LLMResponse(
                content="",
                model="m",
                provider="p",
                tool_calls=[ToolCall(id="c1", name=name, arguments=arguments)],
            )

    gateway = Gateway()
    loop = ToolLoop(gateway, ModelRole.BUILDER, ActionExecutor(EditTools(tmp_path)))  # type: ignore[arg-type]
    await loop.run("mission-1", ContextBundle(task="look at it", acceptance_criteria=[]))

    carriers = [item for item in gateway.seen if item.images]
    assert len(carriers) == 1
    assert carriers[0].role == "user"
    assert carriers[0].images[0].media_type == "image/png"
    # And the tool observation itself carries no image.
    assert all(not item.images for item in gateway.seen if item.role == "tool")


def test_the_image_observation_says_what_was_loaded(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(tiny_png())
    result = load_image(tmp_path, "shot.png")
    rendered = _detail(
        AgentAction(thought="t", action="read_image", path="shot.png"), result
    )
    assert "shot.png" in rendered
    assert "image/png" in rendered


def test_image_references_are_extracted_from_a_prompt() -> None:
    assert _image_references("why does @image:docs/bug.png look wrong?") == ["docs/bug.png"]


def test_several_references_all_attach() -> None:
    assert _image_references("compare @image:a.png with @image:b.png.") == ["a.png", "b.png"]


def test_a_prompt_without_a_reference_attaches_nothing() -> None:
    assert _image_references("no images here") == []
