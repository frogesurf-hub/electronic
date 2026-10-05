"""有限容量的会话存储；同一会话的请求按顺序处理。"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field

from agent_core.understanding.service import Interpretation


@dataclass
class SessionState:
    task_id: str = ""
    pending: bool = False
    previous: Interpretation | None = None
    history: list[dict[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def remember(self, user: str, assistant: str) -> None:
        self.history.extend(
            (
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            )
        )
        self.history = self.history[-12:]


class SessionStore:
    def __init__(self, capacity: int = 128) -> None:
        self._capacity = capacity
        self._sessions: OrderedDict[str, SessionState] = OrderedDict()

    def get(self, session_id: str) -> SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            if len(self._sessions) >= self._capacity:
                for key, candidate in self._sessions.items():
                    if not candidate.lock.locked():
                        del self._sessions[key]
                        break
                else:
                    raise RuntimeError("All sessions are busy")
            state = SessionState()
            self._sessions[session_id] = state
        self._sessions.move_to_end(session_id)
        return state
