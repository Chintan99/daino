"""Lightweight prompt extensions: slash commands and skills.

Playbooks already existed and are the wrong weight for this. A playbook is a
governed procedure — preconditions, approval points, verification steps, rollback
steps, all required — which is right for "deploy to production" and absurd for
"review this diff the way our team reviews diffs". The result was that the second
kind of knowledge had nowhere to live, so it lived in people's heads and got
retyped into the prompt every time.

Two shapes, because there are two distinct needs:

* A **command** is expansion. ``/review-pr 481`` becomes a prompt. The user
  invokes it, it is substituted, and the turn proceeds as if they had typed the
  whole thing. Nothing is decided by the model.
* A **skill** is retrieval. Its name and one-line description sit in the system
  prompt; the body is loaded only when the model decides the task calls for it.
  That is what makes a dozen skills affordable — a dozen full playbooks in
  context would not be.

Both are a markdown file with YAML frontmatter, which is a format people can
write without reading a schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Substituted with everything the user typed after the command name.
ARGUMENTS_TOKEN = "$ARGUMENTS"  # nosec B105 - a template placeholder, not a secret
#: ``$1``…``$9``, positional. A command that takes two named things reads much
#: better with these than by re-splitting ``$ARGUMENTS`` in prose.
_POSITIONAL = re.compile(r"\$([1-9])")


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """A prompt template the user invokes by name."""

    name: str
    body: str
    description: str = ""
    #: Shown in the completion list, e.g. ``<pr-number>``.
    argument_hint: str = ""
    source: Path | None = None
    #: True when it came from the user's global directory rather than the project.
    global_scope: bool = False

    @property
    def invocation(self) -> str:
        return f"/{self.name}"

    def expand(self, arguments: str) -> str:
        """Substitute the user's arguments into the template.

        A template that references neither ``$ARGUMENTS`` nor a positional gets
        the arguments appended. Silently dropping what the user typed is the
        worse failure: they asked for something specific and got the generic
        version of the command with no indication why.
        """
        text = self.body
        words = arguments.split()
        substituted = False
        if ARGUMENTS_TOKEN in text:
            text = text.replace(ARGUMENTS_TOKEN, arguments)
            substituted = True
        if _POSITIONAL.search(text):
            text = _POSITIONAL.sub(
                lambda match: (
                    words[int(match.group(1)) - 1] if int(match.group(1)) <= len(words) else ""
                ),
                text,
            )
            substituted = True
        if not substituted and arguments:
            text = f"{text.rstrip()}\n\n{arguments}"
        return text.strip()


@dataclass(frozen=True, slots=True)
class Skill:
    """Instructions the *model* chooses to load, when the task calls for them."""

    name: str
    description: str
    body: str
    directory: Path | None = None
    global_scope: bool = False
    #: Files sitting beside ``SKILL.md``. Listed rather than inlined: the whole
    #: point of a skill is that its bulk stays out of context until wanted, and
    #: the agent can ``read_file`` any of these once it has the body.
    resources: tuple[str, ...] = field(default_factory=tuple)

    def summary_line(self) -> str:
        """The one line that goes in the system prompt for this skill."""
        return f"- {self.name}: {self.description}"

    def render(self) -> str:
        """The body, plus a pointer to whatever else the skill ships with."""
        if not self.resources or self.directory is None:
            return self.body
        listing = "\n".join(f"- {self.directory / item}" for item in self.resources)
        return (
            f"{self.body.rstrip()}\n\n"
            f"Files bundled with this skill (read them if the task needs them):\n{listing}"
        )
