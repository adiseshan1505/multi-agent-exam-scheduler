"""Base class shared by every agent."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Optional

from ..messages import Message, MsgType

if TYPE_CHECKING:
    from ..messages import MessageBus


class Agent:
    """An agent owns private state, an inbox, and reacts to messages.

    ``PEAS`` documents the agent type in the terms used in the course
    (Performance measure, Environment, Actuators, Sensors).
    """

    kind: ClassVar[str] = "agent"
    PEAS: ClassVar[dict[str, str]] = {}
    listens_to_broadcasts: ClassVar[bool] = False

    def __init__(self, agent_id: str) -> None:
        self.id = agent_id
        self.inbox: list[Message] = []
        self.bus: Optional["MessageBus"] = None

    # -- messaging helpers -------------------------------------------------
    def send(self, type: MsgType, to: str, payload: Optional[dict] = None, summary: str = "") -> None:
        assert self.bus is not None, "agent is not registered on a bus"
        self.bus.send(Message(type, self.id, to, payload or {}, summary))

    def request(self, type: MsgType, to: str, payload: Optional[dict] = None, summary: str = "") -> Message:
        assert self.bus is not None, "agent is not registered on a bus"
        return self.bus.request(Message(type, self.id, to, payload or {}, summary))

    def drain_inbox(self) -> list[Message]:
        msgs, self.inbox = self.inbox, []
        return msgs

    # -- behaviour -------------------------------------------------------
    def step(self, tick: int) -> None:
        """Called once per simulation tick; process the inbox and act."""
        for msg in self.drain_inbox():
            self.on_message(msg)

    def on_message(self, msg: Message) -> None:  # pragma: no cover - overridden
        pass

    def handle_request(self, msg: Message) -> Message:  # pragma: no cover - overridden
        raise NotImplementedError(f"{self.kind} does not handle synchronous {msg.type}")

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind}
