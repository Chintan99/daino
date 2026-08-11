"""Live task checklist shown beside the active workspace."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Static

from vasuki.schemas import TodoItem
from vasuki.tui import palette

_ACTIVITY_LABELS = {
    "idle": "READY",
    "thinking": "THINKING",
    "planning": "PLANNING",
    "inspecting": "INSPECTING",
    "building": "BUILDING",
    "verifying": "VERIFYING",
    "completed": "TASK COMPLETED",
    "failed": "ERROR",
}

_RUNNING_STATES = frozenset({"thinking", "planning", "inspecting", "building", "verifying"})
_DINO_BODY = (
    "   ▄██",
    "▖▄███▀",
)
_DINO_FEET = (" ▀▘  ▀", "  ▀ ▀")
_DINO_STANDING = " ▀▘ ▀"
_GROUND = "─  · ───   ·  ──  "

_ACTIVITY_COLOURS = {
    "idle": palette.DIM,
    "thinking": palette.PLAN,
    "planning": palette.PLAN,
    "inspecting": palette.TOOL,
    "building": palette.ACCENT,
    "verifying": palette.CAUTION,
    "completed": palette.READY,
    "failed": palette.ALERT,
}


class TaskChecklist(VerticalScroll):
    """A compact, persistent view of the plan the agent is working through."""

    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.todos: list[TodoItem] = []
        self.activity_state = "idle"
        self.activity_detail = ""
        self._runner_frame = 0
        self._obstacle_x = 20
        self._runner_timer: Timer | None = None
        self._animation_running = False

    def compose(self) -> ComposeResult:
        yield Static("", id="runner-stage")
        yield Static("", id="activity-label")
        yield Static("", id="checklist-title")
        yield Static("", id="checklist-items")

    def on_mount(self) -> None:
        self._runner_timer = self.set_interval(
            0.14,
            self._advance_runner,
            name="task-runner",
            pause=True,
        )
        self._paint()
        self._sync_animation()

    def on_resize(self, _: events.Resize) -> None:
        self._obstacle_x = min(self._obstacle_x, self._runner_width() - 3)
        self._paint_runner()

    def set_todos(self, todos: list[TodoItem]) -> None:
        self.todos = list(todos)
        self._update_visibility()
        if self.is_mounted:
            self._paint()

    def set_activity(self, state: str, detail: str = "") -> None:
        """Show what Vasuki is doing independently of checklist progress."""
        normalized = state if state in _ACTIVITY_LABELS else "thinking"
        was_running = self.activity_state in _RUNNING_STATES
        self.activity_state = normalized
        self.activity_detail = detail.strip()
        if normalized in _RUNNING_STATES and not was_running:
            self._runner_frame = 0
            self._obstacle_x = self._runner_width() - 3 if self.is_mounted else 20
        elif normalized == "failed":
            # Freeze the cactus at the dinosaur instead of leaving failure as
            # one more abstract status colour.
            self._obstacle_x = 5
        self._update_visibility()
        if self.is_mounted:
            self._paint_activity()
            self._paint_runner()
            self._sync_animation()

    def _update_visibility(self) -> None:
        self.set_class(not self.todos and self.activity_state == "idle", "hidden-panel")

    def _paint_activity(self) -> None:
        colour = _ACTIVITY_COLOURS[self.activity_state]
        label = Text(_ACTIVITY_LABELS[self.activity_state], style=f"bold {colour}")
        if self.activity_detail:
            label.append(f"\n{self.activity_detail[:24]}", style=palette.DIM)
        self.query_one("#activity-label", Static).update(label)

    def _runner_width(self) -> int:
        if not self.is_mounted:
            return 24
        stage = self.query_one("#runner-stage", Static)
        return max(18, stage.size.width)

    def _sync_animation(self) -> None:
        running = self.activity_state in _RUNNING_STATES
        if self._runner_timer is not None:
            if running and not self._animation_running:
                self._runner_timer.resume()
            elif not running and self._animation_running:
                self._runner_timer.pause()
        self._animation_running = running

    def _advance_runner(self) -> None:
        if self.activity_state not in _RUNNING_STATES:
            return
        self._runner_frame += 1
        self._obstacle_x -= 1
        if self._obstacle_x < 4:
            self._obstacle_x = self._runner_width() - 3
        self._paint_runner()

    @staticmethod
    def _draw(
        canvas: list[list[tuple[str, str]]],
        x: int,
        y: int,
        value: str,
        style: str,
    ) -> None:
        if not 0 <= y < len(canvas):
            return
        for offset, character in enumerate(value):
            column = x + offset
            if character != " " and 0 <= column < len(canvas[y]):
                canvas[y][column] = (character, style)

    def _paint_runner(self) -> None:
        if not self.is_mounted:
            return
        width = self._runner_width()
        canvas = [[(" ", palette.DIM) for _ in range(width)] for _ in range(5)]
        ground = (_GROUND * (width // len(_GROUND) + 2))[
            self._runner_frame % len(_GROUND) :
        ]
        for column in range(width):
            canvas[4][column] = (ground[column], palette.FAINTEST)

        running = self.activity_state in _RUNNING_STATES
        jumping = running and 4 <= self._obstacle_x <= 11
        dino_y = 0 if jumping else 1
        dino_colour = (
            palette.ALERT
            if self.activity_state == "failed"
            else palette.READY
            if self.activity_state == "completed"
            else palette.TEXT
            if running
            else palette.DIM
        )
        for sprite_row, line in enumerate(_DINO_BODY):
            self._draw(canvas, 0, dino_y + sprite_row, line, dino_colour)
        feet = (
            _DINO_FEET[self._runner_frame % len(_DINO_FEET)]
            if running
            else _DINO_STANDING
        )
        self._draw(canvas, 0, dino_y + 2, feet, dino_colour)

        if running or self.activity_state == "failed":
            obstacle_colour = (
                palette.ALERT if self.activity_state == "failed" else palette.MUTED
            )
            self._draw(canvas, self._obstacle_x, 1, "╷", obstacle_colour)
            self._draw(canvas, self._obstacle_x - 1, 2, "┤│├", obstacle_colour)
            self._draw(canvas, self._obstacle_x, 3, "│", obstacle_colour)
        if self.activity_state == "failed":
            self._draw(canvas, 7, 1, "×", palette.ALERT)

        output = Text()
        for row_index, canvas_row in enumerate(canvas):
            current_style = canvas_row[0][1]
            run = ""
            for character, style in canvas_row:
                if style != current_style:
                    output.append(run, style=current_style)
                    run = ""
                    current_style = style
                run += character
            output.append(run.rstrip(), style=current_style)
            if row_index < len(canvas) - 1:
                output.append("\n")
        self.query_one("#runner-stage", Static).update(output)

    def _paint(self) -> None:
        self._paint_runner()
        self._paint_activity()
        completed = sum(todo.status == "completed" for todo in self.todos)
        self.query_one("#checklist-title", Static).update(
            Text.assemble(
                ("TASKS", f"bold {palette.PLAN}"),
                (f"  {completed}/{len(self.todos)}", palette.DIM),
            )
        )
        text = Text()
        marks = {
            "completed": ("✓", palette.READY),
            "in_progress": ("●", palette.CAUTION),
            "pending": ("○", palette.DIM),
            "failed": ("×", palette.ALERT),
        }
        for index, todo in enumerate(self.todos):
            mark, colour = marks[todo.status]
            text.append(f"{mark} ", style=colour)
            text.append(
                todo.content,
                style=(
                    palette.DIM
                    if todo.status == "completed"
                    else palette.ALERT
                    if todo.status == "failed"
                    else palette.TEXT
                ),
            )
            if index < len(self.todos) - 1:
                text.append("\n\n")
        self.query_one("#checklist-items", Static).update(text)
