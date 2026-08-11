"""High-signal approval dialog."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ApprovalModal(ModalScreen[tuple[bool, str]]):
    BINDINGS = [
        ("a", "approve_once", "Approve once"),
        ("s", "approve_session", "Approve session"),
        ("r", "reject", "Reject"),
        ("escape", "reject", "Reject"),
    ]

    def __init__(
        self,
        *,
        title: str,
        subject: str,
        risk: str = "medium",
        details: str = "",
    ) -> None:
        super().__init__()
        self.dialog_title = title
        self.subject = subject
        self.risk = risk
        self.details = details

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-dialog"):
            yield Label(self.dialog_title, classes="modal-title")
            yield Static(
                f"[b]Action:[/b] {self.subject}\n"
                f"[b]Risk:[/b] {self.risk.upper()}\n\n"
                f"{self.details or 'Review the active plan before allowing execution.'}"
            )
            with Horizontal(classes="modal-actions"):
                yield Button("Approve once", id="approve-once", variant="success")
                yield Button("For session", id="approve-session", variant="primary")
                yield Button("Reject", id="reject", variant="error")

    def action_approve_once(self) -> None:
        self.dismiss((True, "once"))

    def action_approve_session(self) -> None:
        self.dismiss((True, "session"))

    def action_reject(self) -> None:
        self.dismiss((False, "once"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        result = {
            "approve-once": (True, "once"),
            "approve-session": (True, "session"),
            "reject": (False, "once"),
        }.get(event.button.id or "")
        if result:
            self.dismiss(result)
