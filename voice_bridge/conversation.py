from collections import defaultdict
from typing import Any
import uuid

class ConversationStore:
    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self.messages: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add(self, conversation_id: str, role: str, content: str):
        history = self.messages[conversation_id]
        history.append({"role": role, "content": content})
        # Keep system prompt separate; cap user/assistant turns.
        self.messages[conversation_id] = history[-self.max_turns * 2:]

    def get(self, conversation_id: str):
        return list(self.messages[conversation_id])

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"
