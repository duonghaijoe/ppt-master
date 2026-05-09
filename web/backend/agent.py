"""Claude Agent SDK session manager.

One ClaudeSDKClient per chat tab; sessions are keyed by sessionId and pinned
to a project directory. Authentication relies on the host having run
`claude /login` — no ANTHROPIC_API_KEY required when a Pro/Max subscription
is signed in.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _block_to_event(block) -> Optional[dict]:
    """Translate an SDK content block into a wire event dict."""
    btype = getattr(block, "type", None) or block.__class__.__name__.lower()
    if btype in ("text", "textblock"):
        return {"type": "text", "text": getattr(block, "text", "")}
    if btype in ("tool_use", "tooluseblock"):
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}),
        }
    if btype in ("tool_result", "toolresultblock"):
        content = getattr(block, "content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, default=str)
            except Exception:
                content = str(content)
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", ""),
            "content": content[:4000],
        }
    return {"type": "raw", "repr": repr(block)[:500]}


@dataclass
class Session:
    id: str
    project_path: Path
    client: Optional[ClaudeSDKClient] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def ensure_client(self) -> ClaudeSDKClient:
        if self.client is not None:
            return self.client
        options = ClaudeAgentOptions(
            cwd=str(self.project_path),
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=["project", "user"],
            permission_mode="acceptEdits",
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self.client = client
        return client

    async def send(self, text: str) -> AsyncIterator[dict]:
        async with self.lock:
            client = await self.ensure_client()
            await client.query(text)
            async for message in client.receive_response():
                content = getattr(message, "content", None)
                if isinstance(content, list):
                    for block in content:
                        evt = _block_to_event(block)
                        if evt:
                            yield evt
                elif content is not None:
                    yield {"type": "text", "text": str(content)}
                else:
                    cls = message.__class__.__name__
                    if cls.lower().startswith("result"):
                        yield {"type": "done"}
                        return
            yield {"type": "done"}

    async def close(self):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None


class SessionRegistry:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    async def create(self, project_path: Path) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(id=sid, project_path=project_path)
        async with self._lock:
            self._sessions[sid] = session
        return session

    def get(self, sid: str) -> Optional[Session]:
        return self._sessions.get(sid)

    async def remove(self, sid: str):
        async with self._lock:
            session = self._sessions.pop(sid, None)
        if session:
            await session.close()


registry = SessionRegistry()
