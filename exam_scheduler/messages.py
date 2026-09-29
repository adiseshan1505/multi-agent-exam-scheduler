"""Message types and the message bus agents communicate through.

Two delivery modes are supported:

* ``send``    - asynchronous: the message lands in the recipient's inbox and
                is processed on the recipient's next turn.
* ``request`` - synchronous request/response (like an RPC over the bus): the
                recipient handles the message immediately and returns a reply.
                Used for availability queries and booking requests, where the
                Coordinator needs an answer before it can continue.

Every message is recorded in the bus log so the frontend can replay the
negotiation.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .agents.base import Agent


class MsgType(str, Enum):
    # Exam Request Agent -> Coordinator
    EXAM_REQUEST = "EXAM_REQUEST"
    COUNTER_OFFER = "COUNTER_OFFER"
    HARD_CONFLICT = "HARD_CONFLICT"
    # Coordinator -> Exam Request Agent
    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    RESCHEDULE_NOTICE = "RESCHEDULE_NOTICE"
    # Coordinator <-> Resource Agent
    QUERY_AVAILABILITY = "QUERY_AVAILABILITY"
    AVAILABILITY = "AVAILABILITY"
    BOOK_REQUEST = "BOOK_REQUEST"
    BOOK_ACCEPT = "BOOK_ACCEPT"
    BOOK_REJECT = "BOOK_REJECT"
    CANCEL_BOOKING = "CANCEL_BOOKING"
    CANCEL_ACK = "CANCEL_ACK"
    # Resource Agent -> everyone (unprompted)
    UNAVAILABLE_BROADCAST = "UNAVAILABLE_BROADCAST"


BROADCAST = "*"
_ids = itertools.count(1)


@dataclass
class Message:
    type: MsgType
    sender: str
    recipient: str
    payload: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    id: int = field(default_factory=lambda: next(_ids))
    tick: int = 0
    in_reply_to: Optional[int] = None

    def reply(self, type: MsgType, payload: Optional[dict] = None, summary: str = "") -> "Message":
        return Message(type, self.recipient, self.sender, payload or {}, summary, in_reply_to=self.id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tick": self.tick,
            "type": self.type.value,
            "sender": self.sender,
            "recipient": self.recipient,
            "summary": self.summary,
            "in_reply_to": self.in_reply_to,
        }


class MessageBus:
    def __init__(self) -> None:
        self.agents: dict[str, "Agent"] = {}
        self.log: list[Message] = []
        self.tick = 0

    def register(self, agent: "Agent") -> None:
        self.agents[agent.id] = agent
        agent.bus = self

    def _record(self, msg: Message) -> None:
        msg.tick = self.tick
        self.log.append(msg)

    def send(self, msg: Message) -> None:
        self._record(msg)
        if msg.recipient == BROADCAST:
            for agent in self.agents.values():
                if agent.id != msg.sender and agent.listens_to_broadcasts:
                    agent.inbox.append(msg)
        else:
            self.agents[msg.recipient].inbox.append(msg)

    def request(self, msg: Message) -> Message:
        self._record(msg)
        reply = self.agents[msg.recipient].handle_request(msg)
        self._record(reply)
        return reply

    def pending_messages(self) -> int:
        return sum(len(a.inbox) for a in self.agents.values())
