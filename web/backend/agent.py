"""Claude Agent SDK session manager.

One ClaudeSDKClient per chat tab; sessions are keyed by sessionId and pinned
to a project directory. Authentication relies on the host having run
`claude /login` — no ANTHROPIC_API_KEY required when a Pro/Max subscription
is signed in.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional


# Strip API-key env vars *before* importing the SDK so the spawned `claude`
# subprocess inherits only the host's `claude /login` subscription credentials.
# The SDK merges options.env on top of os.environ — it can add keys but not
# remove them — so we have to clear them here, in the backend's own process.
for _key in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(_key, None)


from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions,
    ClaudeSDKClient,
)
from claude_agent_sdk.types import (  # noqa: E402
    PermissionResultAllow,
    PermissionResultDeny,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


# Scope/privacy guardrail appended to the Claude Code preset system prompt.
# Keeps the assistant focused on deck + design-system work and prevents it
# from leaking host-app implementation details to the user. The hard rules
# below are also enforced server-side in `_sandbox_deny`; this prompt makes
# the model behave consistently instead of looping against denials.
GUARDRAIL_SUFFIX = """\
## Sandbox rules (also enforced server-side)

- You may only Write/Edit files inside the active project directory. Writes
  outside the project will be denied.
- You may NEVER edit `.py` files via Write/Edit/MultiEdit, even inside the
  project. Use Bash to run scripts; do not modify them.
- You may Read files anywhere inside this repository (project + skills +
  templates). Reads outside the repo will be denied.
- Bash commands that try to leave the repo, write to `.py` files, run as
  root, or pipe remote content into a shell will be denied.
- In every reply, refer to files by project-relative paths
  (e.g. `sources/foo.md`). Never paste absolute filesystem paths.

## Privacy

- Never echo absolute filesystem paths above the project directory.
- Never name the agent runtime, SDK, framework, transport, backend host,
  ports, or environment variables in user-facing replies.
- Treat the active project directory as your output workspace. Reads of
  `skills/` and `templates/` are allowed for guidance, but do not
  paraphrase host config or implementation details to the user.

## Scope

You are an assistant for:
1. Generating and editing presentation decks (slides, layouts, content, exports).
2. Producing brand and design-system artifacts (logos, palettes, type scale,
   components, design tokens) as SVG/Markdown/PPTX.

When the user asks something out of scope, give a one-line decline and
suggest a deck or design-system action they could ask for instead.
"""


# Tool-name groupings for sandbox checks.
_FILE_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Update"}
_FILE_READ_TOOLS = {"Read", "NotebookRead"}

# Bash patterns we hard-deny — best-effort defense in depth (a determined
# attacker can still craft a bypass via Python heredoc etc., but this raises
# the bar against accidental damage and casual abuse).
_BASH_DENY_PATTERNS = [
    (re.compile(r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b", re.I), "rm -rf is blocked"),
    (re.compile(r":\s*\(\s*\)\s*\{"), "fork bomb pattern blocked"),
    (re.compile(r"\b(curl|wget|fetch)\b[^|]*\|\s*(sh|bash|zsh|python|python3)\b", re.I), "remote-pipe-to-shell blocked"),
    (re.compile(r"\bsudo\b", re.I), "sudo is blocked"),
    (re.compile(r"\bsu\s+-"), "su is blocked"),
    (re.compile(r"\bssh-keygen\b", re.I), "ssh-keygen is blocked"),
    (re.compile(r">\s*/etc/"), "writes to /etc/ are blocked"),
    (re.compile(r">\s*~/?\.ssh/"), "writes to ~/.ssh/ are blocked"),
    (re.compile(r">\s*~/?\.aws/"), "writes to ~/.aws/ are blocked"),
    (re.compile(r"\bnc\s+-l\b", re.I), "netcat listen is blocked"),
    (re.compile(r">\s*\S+\.py(\s|$|;|&|\|)"), "writes to .py files are blocked"),
    (re.compile(r">>\s*\S+\.py(\s|$|;|&|\|)"), "appends to .py files are blocked"),
]


def _block_to_dict(block) -> Optional[dict]:
    """Translate an SDK content block into a wire event dict (no redaction)."""
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
    # "auto" → SDK acceptEdits (no prompts).
    # "confirm" → can_use_tool callback gates each tool on user approval.
    permission_mode: str = "auto"
    # Empty / None → SDK default. Otherwise a model alias the CLI accepts:
    # "haiku" / "sonnet" / "opus" (or a full model id).
    model: Optional[str] = None
    _pending: dict[str, asyncio.Future] = field(default_factory=dict)
    _events: Optional[asyncio.Queue] = None  # active SSE event channel

    def _redact_paths(self, value):
        """Strip absolute prefixes from paths so chat output is project- or
        repo-relative — never the host's full filesystem layout.

        - `<project>/sub/path`  → `sub/path`
        - bare `<project>`       → `.`
        - `<repo>/skills/...`    → `skills/...`  (above project but in repo)
        - bare `<repo>`          → `.`
        - `<home>/...`           → `~/...`       (everything else)
        """
        proj = str(self.project_path).rstrip("/")
        repo = str(REPO_ROOT).rstrip("/")
        home = os.path.expanduser("~").rstrip("/")

        def fix(s: str) -> str:
            if proj:
                s = s.replace(proj + "/", "")
                s = s.replace(proj, ".")
            if repo:
                s = s.replace(repo + "/", "")
                s = s.replace(repo, ".")
            if home:
                s = s.replace(home + "/", "~/")
                s = s.replace(home, "~")
            return s

        if isinstance(value, str):
            return fix(value)
        if isinstance(value, dict):
            return {k: self._redact_paths(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_paths(v) for v in value]
        return value

    def _resolve(self, raw: str) -> Optional[Path]:
        """Best-effort absolute path resolution. Returns None on failure."""
        if not raw:
            return None
        try:
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = (self.project_path / p)
            return p.resolve()
        except Exception:
            return None

    def _sandbox_deny(self, tool_name: str, tool_input) -> Optional[str]:
        """Return a short deny reason if the call violates project sandbox.

        Always enforced — independent of permission_mode. The model also
        sees these rules in the system prompt so it doesn't loop on
        denials.
        """
        if not isinstance(tool_input, dict):
            return None
        proj = self.project_path.resolve()
        repo = REPO_ROOT.resolve()

        if tool_name in _FILE_EDIT_TOOLS:
            raw = tool_input.get("file_path") or tool_input.get("path") or ""
            target = self._resolve(raw)
            if target is None:
                return None
            try:
                target.relative_to(proj)
            except ValueError:
                return "writes outside the project root are not allowed"
            if target.suffix.lower() == ".py":
                return "editing .py files is not allowed in chat"

        if tool_name in _FILE_READ_TOOLS:
            raw = tool_input.get("file_path") or tool_input.get("path") or ""
            target = self._resolve(raw)
            if target is None:
                return None
            try:
                target.relative_to(repo)
            except ValueError:
                return "reading outside the repository is not allowed"

        if tool_name == "Bash":
            cmd = tool_input.get("command", "") or ""
            for pat, reason in _BASH_DENY_PATTERNS:
                if pat.search(cmd):
                    return reason

        return None

    def _block_to_event(self, block) -> Optional[dict]:
        """Wrap _block_to_dict with path redaction so SSE consumers never
        see absolute host paths."""
        evt = _block_to_dict(block)
        if evt is None:
            return None
        if evt["type"] == "text":
            evt["text"] = self._redact_paths(evt.get("text", ""))
        elif evt["type"] == "tool_use":
            evt["input"] = self._redact_paths(evt.get("input"))
        elif evt["type"] == "tool_result":
            evt["content"] = self._redact_paths(evt.get("content", ""))
        return evt

    async def _can_use_tool(self, tool_name, tool_input, _ctx):
        # 1. Hard sandbox — applies regardless of permission_mode. The user
        #    can't override these from the UI; they're the safety floor.
        deny_reason = self._sandbox_deny(tool_name, tool_input)
        if deny_reason:
            return PermissionResultDeny(message=f"sandbox: {deny_reason}")

        # 2. Auto mode: allow without a prompt.
        if self.permission_mode == "auto":
            return PermissionResultAllow()

        # 3. Confirm mode: ask the connected client and block on response.
        if self._events is None:
            return PermissionResultAllow()  # no UI listening — fall through

        req_id = uuid.uuid4().hex[:10]
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            try:
                serializable = json.loads(json.dumps(tool_input, default=str))
            except Exception:
                serializable = str(tool_input)[:1000]
            preview = self._redact_paths(serializable)
            await self._events.put({
                "type": "permission_request",
                "id": req_id,
                "tool": tool_name,
                "input": preview,
            })
            try:
                decision = await asyncio.wait_for(fut, timeout=600)
            except asyncio.TimeoutError:
                return PermissionResultDeny(message="approval timeout")
        finally:
            self._pending.pop(req_id, None)

        if decision == "approve":
            return PermissionResultAllow()
        return PermissionResultDeny(message="user denied")

    def resolve_permission(self, req_id: str, decision: str) -> bool:
        fut = self._pending.get(req_id)
        if fut and not fut.done():
            fut.set_result(decision)
            return True
        return False

    async def ensure_client(self) -> ClaudeSDKClient:
        if self.client is not None:
            return self.client
        opt_kwargs = dict(
            cwd=str(self.project_path),
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": GUARDRAIL_SUFFIX,
            },
            setting_sources=["project", "user"],
            # In confirm mode we still set acceptEdits to avoid the CLI's
            # own interactive prompts; gating happens in can_use_tool.
            permission_mode="acceptEdits",
            can_use_tool=self._can_use_tool,
        )
        if self.model:
            opt_kwargs["model"] = self.model
        options = ClaudeAgentOptions(**opt_kwargs)
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self.client = client
        return client

    async def set_model(self, model: Optional[str]) -> None:
        """Change the underlying model. Closes the active SDK client so the
        next send reconnects with the new --model. Callers should be aware
        this drops in-session memory (a fresh CLI process is launched)."""
        if (self.model or None) == (model or None):
            return
        self.model = model or None
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

    async def interrupt(self) -> bool:
        """Tell the running model to stop. Returns True if an interrupt was
        actually delivered, False if there was nothing to interrupt."""
        if self.client is None:
            return False
        try:
            await self.client.interrupt()
            return True
        except Exception:
            return False

    async def send(self, text: str) -> AsyncIterator[dict]:
        async with self.lock:
            client = await self.ensure_client()
            self._events = asyncio.Queue()
            await client.query(text)

            sdk_done = asyncio.Event()

            async def pump_sdk():
                try:
                    async for message in client.receive_response():
                        content = getattr(message, "content", None)
                        if isinstance(content, list):
                            for block in content:
                                evt = self._block_to_event(block)
                                if evt:
                                    await self._events.put(evt)
                        elif content is not None:
                            await self._events.put({"type": "text", "text": self._redact_paths(str(content))})
                        else:
                            cls = message.__class__.__name__
                            if cls.lower().startswith("result"):
                                await self._events.put({"type": "done"})
                                return
                    await self._events.put({"type": "done"})
                finally:
                    sdk_done.set()

            pump_task = asyncio.create_task(pump_sdk())
            try:
                while True:
                    evt = await self._events.get()
                    yield evt
                    if evt.get("type") == "done":
                        break
            finally:
                pump_task.cancel()
                try:
                    await pump_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._events = None
                # Reject any orphaned permission requests so the SDK unblocks.
                for fut in list(self._pending.values()):
                    if not fut.done():
                        fut.set_result("deny")
                self._pending.clear()

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
