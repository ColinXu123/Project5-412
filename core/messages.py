"""Typed messages that agents exchange. Everything is JSON-serialisable."""
import json
import uuid
from dataclasses import dataclass, field, asdict


def _id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Task:
    sender: str
    recipient: str
    action: str
    description: str
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=_id)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


@dataclass
class Result:
    task_id: str
    sender: str
    status: str            # success | failed | denied
    summary: str
    data: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @property
    def ok(self) -> bool:
        return self.status == "success"
