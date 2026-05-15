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
- You are bound to one tenant for this session. Reads and writes that
  touch a different tenant's tree (`tenants/<other-slug>/...`) are denied,
  including via Bash. Stay inside your active project.
- In every reply, refer to files by project-relative paths
  (e.g. `sources/foo.md`). Never paste absolute filesystem paths.

## Privacy — never expose backend internals

To the user, you are a product. They never need to know which scripts,
interpreters, env vars, or repo paths power you. Speak about *what you
can do*, not *how the system does it*.

Hard rules:

- Never name or quote the agent runtime, SDK, framework, transport,
  backend host, ports, processes, or environment variables.
- Never reveal repo-internal paths the user did not give you:
  `platform/`, `templates/`, `scripts/`, `.venv/`, `web/backend/`,
  `web/frontend/`, `examples/`, or anything under them. Do not say
  things like "the repo ships X" or "there's a script at Y".
- Never show shell invocations of internal tooling
  (`.venv/bin/python ...`, `python platform/skills/...`, `scripts/...`,
  `pnpm run ...`, `uvicorn ...`, `npm run ...`). If a command is
  required, run it yourself via Bash — do not print it for the user
  to copy.
- Never paste absolute filesystem paths (`/Users/...`, `/home/...`),
  `file://` URLs, or paths above the active project directory.
- Never describe internal CLI flags, config files, or "backend
  selection" mechanics. The user does not pick backends; you do.
- When the user asks *how* you do something ("how is this generated?",
  "what script runs this?", "what model do you use?"), do not answer
  with internals. Answer with the *capability* and an offer to do it:
  e.g. "Tell me what image you want and I'll produce it into the
  project." Keep it to one or two sentences.
- Treat the active project directory as your output workspace. You may
  read `platform/skills/` and `templates/` for guidance, but do not paraphrase
  their contents, paths, or implementation details back to the user.
- Tool calls themselves are surfaced separately in the UI; never
  re-narrate "I ran X command at Y path" in your text reply.

## Scope

You are an assistant for:
1. Generating and editing presentation decks (slides, layouts, content, exports).
2. Producing brand and design-system artifacts (logos, palettes, type scale,
   components, design tokens) as SVG/Markdown/PPTX.

When the user asks something out of scope, give a one-line decline and
suggest a deck or design-system action they could ask for instead.

## Templates (reusable design DNA)

When the user says "save this as a template" (or similar — "make a template
from this", "package this as a reusable theme", "save the theme") in this
web chat, they mean the **web UI Templates tab**. There is exactly one way
to satisfy this request: a single HTTP POST. Do NOT touch the repo skill
layout library; do NOT copy SVGs anywhere; do NOT run the standalone
template-creation workflow from the skill package — those produce artifacts
the Templates tab cannot see and will leave the user confused when the tab
stays empty.

### The only correct action

Run exactly one Bash call:

```
curl -sS -X POST http://127.0.0.1:8787/api/templates \
  -H 'Content-Type: application/json' \
  -d '{"source_project": "...", "name": "...", "description": "...", "include_images": [...]}'
```

A `200` response with a `name` field means the template now shows in the
Templates tab. Anything else and you must report the failure verbatim — do
not fall back to the skill library, do not write files yourself.

### Body fields

- `source_project` — the active project's full directory name
  (e.g. `bootcamp_qe_ppt169_20260511`). The backend reads the project's
  own `SKILL.md`, `design_spec.md`, `spec_lock.md`, and `templates/`
  directory from the project root and copies them into the template
  snapshot. You do not need to (and must not) copy these files yourself.
- `name` — short safe slug, letters/digits/underscore/hyphen, ≤ 64 chars.
- `description` — one short line.
- `include_images` — list of project-relative paths under `images/` that
  are brand-critical (logos, marks, header art, reusable backgrounds).
  Exclude per-slide illustrations and AI-generated content images. When in
  doubt, list fewer rather than more.

### Before calling

Make sure the project root actually contains the design DNA the snapshot
will copy: at minimum a `SKILL.md` at the project root capturing the brand
palette, typography, and header pattern. If those files don't exist, write
them at the project root first (allowed — that's inside the project), then
make the API call. The snapshot is only as good as the source.

### Hard rules

- Direct Write/Edit to `<repo>/tenants/default/templates/<name>/` is denied.
  The API is the only path.
- Do NOT create files under `platform/skills/ppt-master/templates/`, even via Bash.
  That tree is the skill package's own layout library, not a place for
  user templates.
- After a successful save, confirm with the template name and what was
  included — do not claim "saved" until the API returned 200.

## Web preview (artifact rendering)

The user previews artifacts in a sandboxed iframe in the web UI. Make
every artifact you produce viewable in that iframe — not only on disk.

- Reference sibling assets with project-relative URLs only:
  `./image.png`, `images/logo.svg`, `flashcards/previews/x.png`.
  The web preview serves these as paths under the project root.
- Never embed absolute filesystem paths (`/Users/...`, `/home/...`,
  `file://...`, `C:\\...`). They break the moment the file is rendered
  in the browser.
- Prefer self-contained HTML when the artifact has only a handful of
  small assets: inline `<style>` and `<script>`, base64-encode tiny
  images as data URIs. This makes it portable and immune to path drift.
- For HTML composites that reference many sibling files, keep the HTML
  and its assets in the same folder (or a stable subfolder) so the
  relative URLs resolve. The web preview anchors relative paths at
  the HTML file's own folder.
- Avoid `<base href="file://...">`, `<base href="/Users/...">`, or any
  base tag that points outside the project. If you need a base, use
  `<base href="./">` or omit it entirely.
- Avoid loading external CDNs/scripts the iframe sandbox would block.
  Bundle dependencies into the project (or inline them).
"""


# Tool-name groupings for sandbox checks.
_FILE_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Update"}
_FILE_READ_TOOLS = {"Read", "NotebookRead"}

# Bash patterns we hard-deny — best-effort defense in depth (a determined
# attacker can still craft a bypass via Python heredoc etc., but this raises
# the bar against accidental damage and casual abuse).
# Write-destination patterns for the two paths we never want agents to seed
# via shell. Reads (cat / grep / less) are allowed — only redirection, copy,
# move, mkdir, and tee with the protected path as the destination are denied.
_SKILL_TPL = r"\S*platform/skills/ppt-master/templates/"
_SKILL_TEMPLATES_WRITE_PATTERNS = [
    re.compile(rf"\bmkdir(?:\s+-\S+)*\s+{_SKILL_TPL}", re.I),
    re.compile(rf"\btee\s+(?:-\S+\s+)*{_SKILL_TPL}", re.I),
    re.compile(rf"\b(?:cp|mv|rsync)\s+\S+\s+(?:-\S+\s+)*{_SKILL_TPL}", re.I),
    re.compile(rf">>?\s*{_SKILL_TPL}"),
]
# Tenant templates/ — `\s+` in each verb pattern already anchors the path
# directly after the verb's arguments, so `cp src foo/templates/bar` (which
# writes to a project's own `templates/` dir, not the tenant root) won't match.
# Matches both legacy repo-root paths and the post-Phase-1 tenants/<slug>/
# layout so the deny still triggers for stragglers during the transition.
_REPO_TPL = r"(?:\./)?(?:tenants/[A-Za-z0-9_-]+/)?templates/[A-Za-z0-9_.-]+"
_REPO_TEMPLATES_WRITE_PATTERNS = [
    re.compile(rf"\bmkdir(?:\s+-\S+)*\s+{_REPO_TPL}", re.I),
    re.compile(rf"\btee\s+(?:-\S+\s+)*{_REPO_TPL}", re.I),
    re.compile(rf"\b(?:cp|mv|rsync)\s+\S+\s+{_REPO_TPL}", re.I),
    re.compile(rf">>?\s*{_REPO_TPL}"),
]

# Tenant cross-reference: any `tenants/<slug>/` mention in a Bash command.
# Used to deny commands that touch a tenant other than the session's own.
# Slugs follow the same charset as ``_safe_slug`` in files.py.
_TENANT_PATH_PATTERN = re.compile(r"\btenants/([A-Za-z0-9_-]+)/")

# Phase 4: platform admin registry + secrets. Reads and writes via Bash are
# both denied, mirroring the per-path Read/Edit denial in ``_sandbox_deny``.
# ``.platform.json`` is the legacy bootstrap registry; ``platform/admins*``
# and ``platform/secrets*`` cover the proper layout for future state.
_PLATFORM_ADMIN_PATTERN = re.compile(
    r"(?:^|\s|['\"`/])(?:\./)?"
    r"(?:\.platform\.json|platform/admins(?:\.json)?(?:/|\b)|platform/secrets(?:\.json)?(?:/|\b))"
)

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
    # Phase 3: every session is bound to a tenant + user. The sandbox uses
    # these to scope writes to ``tenants/<tenant_slug>/`` and deny cross-tenant
    # reads. Legacy callers that don't supply them resolve under the default
    # tenant for backwards compatibility.
    tenant_slug: str = "default"
    user_id: str = ""
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
        - `<repo>/platform/...`  → `platform/...`  (above project but in repo)
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

    def _tenant_root(self) -> Path:
        """``<repo>/tenants/<self.tenant_slug>`` resolved."""
        return (REPO_ROOT / "tenants" / self.tenant_slug).resolve()

    def _is_other_tenant_path(self, target: Path) -> bool:
        """True when ``target`` lives under a tenants/<slug>/ other than ours.

        Used to deny cross-tenant reads and writes: an agent in tenant ``acme``
        must not be able to ``cat tenants/globex/projects/foo.md``. Paths above
        ``tenants/`` (``platform/``, ``examples/``, ``docs/``) aren't in any
        tenant's tree and stay readable as platform-shared resources.
        """
        tenants_root = (REPO_ROOT / "tenants").resolve()
        try:
            rel = target.relative_to(tenants_root)
        except ValueError:
            return False
        if not rel.parts:
            return False
        return rel.parts[0] != self.tenant_slug

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
        tenant_root = self._tenant_root()

        if tool_name in _FILE_EDIT_TOOLS:
            raw = tool_input.get("file_path") or tool_input.get("path") or ""
            target = self._resolve(raw)
            if target is None:
                return None
            # Phase 3: cross-tenant writes are categorically denied — even
            # before considering project containment. An agent in tenant A
            # cannot write anything under tenants/B/.
            if self._is_other_tenant_path(target):
                return "cross-tenant writes are not allowed"
            try:
                target.relative_to(proj)
            except ValueError:
                # Writes are project-scoped — with one structural exception:
                # the tenant `templates/` tree (user-saved templates) is
                # written to via the API, never directly. Any other path
                # outside the project root is denied.
                templates_root = (tenant_root / "templates").resolve()
                try:
                    target.relative_to(templates_root)
                    return (
                        "edit templates/ via the API, not direct write: "
                        "POST /api/tenants/<t>/templates {source_project, name, ...} "
                        "or DELETE /api/tenants/<t>/templates/<name>"
                    )
                except ValueError:
                    return "writes outside the project root are not allowed"
            if target.suffix.lower() == ".py":
                return "editing .py files is not allowed in chat"
            # The Workbench directive is structurally fragile (one corrupt
            # write hides every registered deck). Force agents through the
            # validated POST /output-dirs/register endpoint so existing
            # entries can't be accidentally dropped and schemas can't drift.
            if target.name == "output_dirs.json" and target.parent.resolve() == proj:
                return (
                    "edit output_dirs.json via the API, not direct write: "
                    "POST /api/tenants/<t>/projects/<name>/output-dirs/register "
                    "{dir, label?, set_current?}"
                )

        if tool_name in _FILE_READ_TOOLS:
            raw = tool_input.get("file_path") or tool_input.get("path") or ""
            target = self._resolve(raw)
            if target is None:
                return None
            try:
                target.relative_to(repo)
            except ValueError:
                return "reading outside the repository is not allowed"
            # Phase 3: cross-tenant reads denied. Platform-scope paths
            # (platform/, examples/, docs/) live above tenants/ and remain
            # readable.
            if self._is_other_tenant_path(target):
                return "cross-tenant reads are not allowed"
            # Phase 4: the platform admin registry is never readable from a
            # tenant sandbox. ``.platform.json`` (and any future
            # ``platform/admins.json``) lists who can bypass tenant role
            # checks — leaking it would let agents enumerate platform admins.
            try:
                rel = target.relative_to(repo)
            except ValueError:
                rel = None
            if rel is not None:
                parts = rel.parts
                if parts == (".platform.json",):
                    return "reading the platform admin registry is not allowed"
                if len(parts) >= 2 and parts[0] == "platform" and parts[1] in {"admins.json", "admins", "secrets"}:
                    return "reading platform admin / secret state is not allowed"

        if tool_name == "Bash":
            cmd = tool_input.get("command", "") or ""
            for pat, reason in _BASH_DENY_PATTERNS:
                if pat.search(cmd):
                    return reason
            # The skill package's layout library and the user-template store are
            # the two locations agents most often confuse with each other. Both
            # are read-only from the chat surface — writes must go through the
            # API. The patterns below match write DESTINATIONS only (so reading
            # `cat platform/skills/.../templates/foo.md` still works).
            for pat in _SKILL_TEMPLATES_WRITE_PATTERNS:
                if pat.search(cmd):
                    return (
                        "do not write to platform/skills/ppt-master/templates/ — that's the "
                        "skill's own layout library, not the user-template store. "
                        "Use POST http://127.0.0.1:8787/api/tenants/<t>/templates instead."
                    )
            for pat in _REPO_TEMPLATES_WRITE_PATTERNS:
                if pat.search(cmd):
                    return (
                        "do not write to templates/ directly — use the API: "
                        "POST http://127.0.0.1:8787/api/tenants/<t>/templates "
                        "{source_project, name, description, include_images}"
                    )
            # Bash cross-tenant guard: deny any mention of another tenant's
            # directory. Cheap pattern match — finds `tenants/<other>/...` even
            # in pipelines, redirects, and quoted args. Reading or writing
            # the current tenant's tree is unaffected.
            for m in _TENANT_PATH_PATTERN.finditer(cmd):
                slug = m.group(1)
                if slug and slug != self.tenant_slug:
                    return f"cross-tenant path tenants/{slug}/ is denied"
            # Phase 4: platform admin / secrets are off-limits from Bash too.
            # Covers `cat .platform.json`, `ls platform/admins/`, redirected
            # writes to `platform/secrets.json`, and friends.
            if _PLATFORM_ADMIN_PATTERN.search(cmd):
                return "platform admin / secrets are not readable from chat"

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

    async def create(
        self,
        project_path: Path,
        *,
        tenant_slug: str = "default",
        user_id: str = "",
    ) -> Session:
        sid = uuid.uuid4().hex[:12]
        session = Session(
            id=sid,
            project_path=project_path,
            tenant_slug=tenant_slug,
            user_id=user_id,
        )
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
