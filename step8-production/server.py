"""Drive MCP server — Inbox Guardian Step 8, production hardening.

Carries Step 7's server forward unmodified in its actual capabilities
(HTTP transport, OAuth 2.1, sampling-ranked search, elicitation on
ambiguity, progress, cancellation) -- this step adds nothing new a
client can *do*, only things that make what it can already do safe to
run unattended:

  - Tool annotations (`readOnlyHint`/`destructiveHint`/`idempotentHint`/
    `openWorldHint`) on every tool, precise enough that a client reading
    only `tools/list` -- never this file -- can correctly predict which
    calls are safe to make automatically.
  - Explicit Pydantic output schemas for every tool result, published as
    each `Tool`'s `outputSchema` and validated (twice, independently --
    see "Structured output" below) before any response leaves this
    process.
  - Real `logging` at real levels, verbosity configurable at startup
    instead of a hardcoded `DEBUG` (which is what Steps 5-7 shipped).
  - A `--dry-run` guard rail (`_guard_write`) wired in now and proven
    against a synthetic write path in `test_dry_run_guard.py`, even
    though no tool in this server writes anything yet -- so it's a
    tested mechanism Step 9's Dispatcher can depend on, not a promise
    made in a docstring.
  - The running server's actual git SHA in `serverInfo.version`
    (`initialize`'s response), computed fresh at startup via
    `git rev-parse HEAD`, not a value baked in once and left to rot.

Sampling and elicitation remain the two inversions Step 6/7 built --
see those steps' READMEs for what they are and why they're different
primitives; nothing about the mechanism changes here.
"""

import argparse
import io
import json
import logging
import os
import secrets
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

import anyio
import mcp.types as types
import uvicorn
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from pydantic import AnyHttpUrl, AnyUrl, BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from oauth.consent import create_consent_routes
from oauth.provider import DriveOAuthProvider

# --- Logging: real levels, verbosity configurable at startup ---------------
#
# Steps 5-7 hardcoded `level=logging.DEBUG`, which meant every raw
# JSON-RPC frame always printed to stderr whether anyone wanted it or
# not. Production-facing default here is INFO (tool calls, ambiguity/
# denial/decline events, startup/auth milestones -- readable at a
# glance); DEBUG (raw wire frames, resources/list/tools/list handling)
# is opt-in via `--log-level DEBUG` or `LOG_LEVEL=DEBUG` in the
# environment, not the default. See "Real logging" in this step's
# README for the verified DEBUG-vs-INFO comparison.
_DEFAULT_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=_DEFAULT_LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s [server] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.drive-server")

STEP_DIR = Path(__file__).parent
REPO_ROOT = STEP_DIR.parent
LOG_FILE = STEP_DIR / "jsonrpc.log"
_rpc_log = LOG_FILE.open("w", encoding="utf-8")

TOKEN_PATH = Path.home() / ".drive-mcp" / "token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

GOOGLE_APPS_PREFIX = "application/vnd.google-apps."
GOOGLE_NATIVE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
DEFAULT_EXPORT_MIME_TYPE = "text/plain"

MCP_PATH = "/mcp"
REQUIRED_SCOPE = "drive:read"
DOWNLOAD_CHUNK_SIZE = 131072  # 128 KiB -- small on purpose, so a ~2.5MB file yields ~20 progress steps
DOWNLOAD_CHUNK_PACING_SECONDS = 0.35  # artificial; see Step 5 README's "what's synthetic here" note


# --- dry-run: the guard rail this step's build task asks for, wired in ----
# before there's a real write tool to protect, and proven against a
# synthetic one in test_dry_run_guard.py rather than left untested.


DRY_RUN = False  # set from --dry-run in main(); a module-level flag (not
# per-request state) because it describes how this server instance was
# started, not something a caller can influence per-call.


class DryRunBlocked(RuntimeError):
    """Raised by `_guard_write` when a write-capable code path is reached
    while `--dry-run` is active. A real write tool (Step 9+'s Dispatcher,
    e.g. `gmail::send_email`) is expected to let this propagate rather
    than catch it -- the low-level `Server.call_tool` wrapper turns any
    uncaught exception into `CallToolResult(isError=True)`, the same
    mechanism every other tool-level failure in this project already
    relies on (see Step 1's error-handling discipline)."""


def _guard_write(action: str) -> None:
    """Call this as the first line of any code path that would write,
    send, or otherwise modify state outside this process (a Drive file,
    a sent email, anything not a read). No tool in this server does that
    yet -- `SCOPES` above is still `drive.readonly`, same discipline kept
    since Step 3 -- but Step 9's Dispatcher will, and retrofitting a
    safety check onto a write path after it exists is exactly the wrong
    order. This function is what that future tool calls into; today its
    only caller is the synthetic probe below, which exists solely to
    prove this actually blocks something rather than being a guard rail
    with nothing wired to it.

    Pattern for a future write-capable tool's annotations (not
    registered here -- no such tool exists yet, but the shape a reviewer
    should expect):

        types.Tool(
            name="send_email",
            annotations=types.ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,   # an unread/unsent state can't be recovered by calling it again
                idempotentHint=False,   # calling it twice sends two emails, not one
                openWorldHint=True,
            ),
        )

    and per spec, a client is expected to require explicit human
    confirmation before calling any tool with `destructiveHint=True` --
    the same confirmation discipline this project has used for sampling
    approval (Step 6) and elicitation (Step 7) since they were built.
    """
    if DRY_RUN:
        logger.error("dry-run: blocked a write-capable action: %s", action)
        raise DryRunBlocked(f"--dry-run is active; refusing to execute: {action}")
    logger.info("write-capable action proceeding (dry-run not active): %s", action)


async def _synthetic_write_probe() -> str:
    """Not a real tool -- not in `handle_list_tools`, not reachable via
    `tools/call`. Exists only so `test_dry_run_guard.py` can prove
    `_guard_write` actually blocks a write-shaped call end to end, since
    this server has no real write tool yet to attempt one against.
    Stands in for whatever Step 9's Dispatcher eventually calls (e.g.
    Gmail send) before that code exists.
    """
    _guard_write("synthetic: send an email / write a Drive file")
    return "synthetic write executed (should never happen while --dry-run is active)"


# --- git SHA: computed fresh at startup, not hardcoded ---------------------


def _resolve_git_sha() -> str:
    """`serverInfo.version` should tell a connecting client exactly which
    commit this running process is -- useful the moment there's more
    than one deployed instance. Computed via `git rev-parse HEAD` against
    the repo root at *startup*, not written once into a constant and
    left to drift the moment another commit lands; a dirty working tree
    (uncommitted changes on top of HEAD) is flagged with a `-dirty`
    suffix rather than silently reporting a SHA that doesn't fully
    describe what's actually running.
    """
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return f"{sha}-dirty" if status else sha
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        logger.warning("could not resolve git SHA for serverInfo.version (%s) -- falling back to 'unknown'", exc)
        return "unknown"


_GIT_SHA = _resolve_git_sha()


# --- Structured output: explicit Pydantic schemas, validated before any ---
# response leaves this process. Declared as each Tool's `outputSchema`
# (below, in handle_list_tools) so a client can validate independently
# too, and enforced twice: once here via Pydantic model construction
# (raises pydantic.ValidationError on bad data, before any dict is ever
# built), and again by the SDK's own low-level `Server.call_tool`
# wrapper, which runs `jsonschema.validate` against the declared
# `outputSchema` on whatever this module hands back. Both layers reject
# the same malformed payload independently -- see
# `test_output_schemas.py` for both proven directly, not assumed.


class DriveFileMatch(BaseModel):
    """One Drive file as returned to a caller -- file_id/name/mimeType
    only, never content (see search_drive_files' tool description)."""

    model_config = ConfigDict(extra="forbid")

    file_id: str
    name: str
    mimeType: str


class SearchDriveFilesResult(BaseModel):
    """search_drive_files' one consistent result shape, discriminated by
    `status` rather than the two different top-level JSON shapes (a bare
    list vs. a dict) Step 7 returned -- a client validating against a
    single declared `outputSchema` needs one shape, not two to guess
    between.

    `status="matched"`: `matches` is the ranked (possibly filtered)
    result list, `message` is unset.
    `status="ambiguous"`: sampling flagged a genuine content conflict
    and the human didn't resolve it (declined/cancelled); `matches`
    holds the unresolved candidates and `message` explains why nothing
    was picked.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["matched", "ambiguous"]
    matches: list[DriveFileMatch] = Field(default_factory=list)
    message: str | None = None


class DownloadDriveFileResult(BaseModel):
    """download_drive_file's result -- validated before being returned,
    same discipline as SearchDriveFilesResult. `size_bytes` is
    constrained non-negative (`ge=0`): a malformed byte count is exactly
    the kind of bug this schema boundary exists to catch loudly instead
    of quietly relaying a nonsensical number downstream.
    """

    model_config = ConfigDict(extra="forbid")

    file_id: str
    name: str
    mimeType: str
    size_bytes: int = Field(ge=0)


def _build_matched_result(matches: list[dict]) -> dict:
    """Validate + serialize a `status="matched"` search result. Raises
    `pydantic.ValidationError` if any candidate dict is missing a
    required field or has the wrong type -- see `test_output_schemas.py`
    for this proven with a deliberately malformed candidate, not assumed.
    """
    result = SearchDriveFilesResult(status="matched", matches=[DriveFileMatch(**m) for m in matches])
    return result.model_dump(mode="json")


def _build_ambiguous_result(candidates: list[dict], message: str) -> dict:
    """Validate + serialize a `status="ambiguous"` search result (human
    didn't resolve a genuine conflict -- see `_disambiguate_with_human`).
    """
    result = SearchDriveFilesResult(
        status="ambiguous",
        matches=[DriveFileMatch(**c) for c in candidates],
        message=message,
    )
    return result.model_dump(mode="json")


def _build_download_result(*, file_id: str, name: str, mime_type: str, size_bytes: int) -> dict:
    """Validate + serialize a download_drive_file result."""
    result = DownloadDriveFileResult(file_id=file_id, name=name, mimeType=mime_type, size_bytes=size_bytes)
    return result.model_dump(mode="json")


SEARCH_DRIVE_FILES_OUTPUT_SCHEMA = SearchDriveFilesResult.model_json_schema()
DOWNLOAD_DRIVE_FILE_OUTPUT_SCHEMA = DownloadDriveFileResult.model_json_schema()


def _log_wire(direction: str, raw: str) -> None:
    """Record one line crossing the /mcp HTTP boundary, either direction.

    Unlike Steps 1-3's stdio pump (which re-serialized parsed
    SessionMessage objects), this logs the literal bytes Starlette
    reads off/writes to the socket -- POST request bodies (JSON-RPC
    requests) and each SSE `data: ...` write (JSON-RPC responses/
    notifications) -- via ASGI-level receive/send wrapping in
    _WireLoggingMiddleware below. Always written to jsonrpc.log in full
    regardless of --log-level (that file is the audit trail); only the
    human-readable stderr echo (`logger.debug`, below) is gated by
    verbosity, so `--log-level DEBUG` is what actually makes raw frames
    show up on the console on demand.
    """
    line = f"{direction} {raw}"
    logger.debug(line)
    _rpc_log.write(line + "\n")
    _rpc_log.flush()


class _WireLoggingMiddleware:
    """ASGI middleware logging every HTTP request/response body byte range
    that crosses this server's boundary into one jsonrpc.log -- MCP
    JSON-RPC traffic on /mcp, and the OAuth handshake requests
    (/authorize, /token, /register, /consent, the metadata documents)
    on every other path, all in one place so the two can be read
    together during a debugging session instead of guessing which log
    a given request landed in.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]

        async def logged_receive():
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body:
                    _log_wire(">>>", f"{method} {path} {body.decode('utf-8', 'replace')}")
            return message

        async def logged_send(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    for line in body.decode("utf-8", "replace").splitlines():
                        if line.strip():
                            _log_wire("<<<", f"{path} {line}")
            await send(message)

        await self.app(scope, logged_receive, logged_send)


def _load_credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        logger.error(
            "Drive credentials not found at %s. Run step3-real-servers/drive-server/auth_setup.py "
            "once to authenticate before starting this server.",
            TOKEN_PATH,
        )
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        logger.info("refreshing expired Drive access token")
        creds.refresh(GoogleAuthRequest())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def _gdrive_uri(file_id: str) -> str:
    return f"gdrive:///{file_id}"


def _file_id_from_uri(uri: str) -> str:
    if not uri.startswith("gdrive:///"):
        raise ValueError(f"Unsupported resource URI scheme: {uri!r}")
    return uri.removeprefix("gdrive:///")


server = Server("inbox-guardian-drive-http", version=_GIT_SHA)
drive_service = None  # set in main(), after auth succeeds


# --- Resources: unchanged from Step 3 -- transport doesn't touch these -----


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    logger.debug("handling resources/list")
    response = drive_service.files().list(pageSize=20, fields="files(id, name, mimeType)").execute()
    return [
        types.Resource(
            uri=AnyUrl(_gdrive_uri(file["id"])),
            name=file["name"],
            mimeType=file.get("mimeType"),
        )
        for file in response.get("files", [])
    ]


async def _fetch_drive_file_content(
    file_id: str,
    *,
    report_progress: Callable[[float, float | None], Awaitable[None]] | None = None,
) -> tuple[bytes | str, str, str]:
    """Fetch a Drive file's full content, branching on mimeType -- the one
    implementation both resources/read and the download_drive_file tool
    call into, so the two can't silently drift the way independent copies
    of the same lookup did in Step 2 (see test_shared_download.py, which
    proves this by spying on this function, not just comparing output).

    Google-native files (Docs/Sheets/Slides) only support `export()`, not
    `get_media()` -- calling get_media on one raises exactly the 403 this
    fix addresses ("Only files with binary content can be downloaded. Use
    Export with Docs Editors files."). Drive's export API has no chunked/
    paginated equivalent to MediaIoBaseDownload, so that branch is one
    blocking call (still real work, still off the event loop, still
    cancellable) with no intermediate progress to report -- `report_progress`
    is only ever invoked from the regular-file branch below.

    Returns (content, mime_type, name). `content` is `str` for a
    Google-native export (always text) or a UTF-8-decodable regular file,
    `bytes` for a binary regular file.
    """
    metadata = await anyio.to_thread.run_sync(
        lambda: drive_service.files().get(fileId=file_id, fields="name, mimeType").execute()
    )
    mime_type = metadata["mimeType"]
    name = metadata["name"]

    if mime_type.startswith(GOOGLE_APPS_PREFIX):
        export_mime_type = GOOGLE_NATIVE_EXPORT_MIME_TYPES.get(mime_type, DEFAULT_EXPORT_MIME_TYPE)
        raw = await anyio.to_thread.run_sync(
            lambda: drive_service.files().export(fileId=file_id, mimeType=export_mime_type).execute(),
            abandon_on_cancel=True,
        )
        return raw.decode("utf-8"), export_mime_type, name

    buffer = io.BytesIO()
    request = drive_service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buffer, request, chunksize=DOWNLOAD_CHUNK_SIZE)

    done = False
    chunk_index = 0
    while not done:
        chunk_index += 1
        logger.debug("_fetch_drive_file_content: requesting chunk %d of %r", chunk_index, name)

        # abandon_on_cancel=True: if notifications/cancelled arrives while
        # this chunk's network call is in flight, the await raises
        # immediately instead of waiting for the (unkillable) worker
        # thread to finish -- we stop issuing further chunk requests and
        # never build/return a result, which is the real proof cancellation
        # actually stopped work rather than just the caller giving up on
        # waiting for a response that was coming anyway.
        status, done = await anyio.to_thread.run_sync(downloader.next_chunk, abandon_on_cancel=True)

        logger.debug(
            "_fetch_drive_file_content: chunk %d received (%d / %s bytes, %.0f%%)",
            chunk_index,
            status.resumable_progress,
            status.total_size,
            status.progress() * 100,
        )

        if report_progress is not None:
            await report_progress(status.resumable_progress, status.total_size)
            # Artificial pacing -- see Step 5 README's "what's synthetic here"
            # note. Only applied when a caller is actually listening for
            # progress (i.e. the download_drive_file tool, not
            # resources/read): a 128KiB chunk of this file arrives fast
            # enough on a real connection that there's no reliable window to
            # send notifications/cancelled mid-flight without it. The Drive
            # API call itself (MediaIoBaseDownload.next_chunk) is entirely
            # real.
            await anyio.sleep(DOWNLOAD_CHUNK_PACING_SECONDS)

    raw = buffer.getvalue()
    try:
        return raw.decode("utf-8"), mime_type, name
    except UnicodeDecodeError:
        return raw, mime_type, name


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    logger.debug("handling resources/read: uri=%s", uri)
    file_id = _file_id_from_uri(str(uri))

    try:
        content, mime_type, _name = await _fetch_drive_file_content(file_id)
        return [ReadResourceContents(content=content, mime_type=mime_type)]
    except HttpError as exc:
        logger.error("resources/read failed for %r: %s", file_id, exc)
        raise ValueError(f"Failed to read Drive file {file_id!r}: {exc}") from exc


# --- Tools: search (sampling-ranked, elicitation on ambiguity) + download_drive_file --


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    logger.debug("handling tools/list")
    return [
        types.Tool(
            name="search_drive_files",
            title="Search Drive files (sampling-ranked)",
            description=(
                "Find Drive files relevant to a natural-language description of "
                "what you're looking for (e.g. \"how do I get my money back\") -- "
                "not Drive's own query operator syntax, unlike Steps 3-5's version "
                "of this tool. Every candidate file in the account is ranked by "
                "actual relevance to your query via the connected MCP client's LLM "
                "(sampling/createMessage), not by Drive's own keyword matching. If "
                "two or more top candidates are genuinely ambiguous -- plausibly "
                "relevant but materially conflicting -- this tool pauses and asks "
                "a human to disambiguate via elicitation/create rather than "
                "guessing; a decline leaves the ambiguity unresolved rather than "
                "silently picking one. Returns file_id/name/mimeType only -- no "
                "content. Fetch content via resources/read on the returned "
                "gdrive:///{file_id} URI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Natural-language description of what you're looking for, "
                            "e.g. \"how do I get a refund\" -- not Drive query syntax."
                        ),
                    }
                },
                "required": ["query"],
            },
            outputSchema=SEARCH_DRIVE_FILES_OUTPUT_SCHEMA,
            # Annotated for a stranger who reads only this schema, never
            # server.py: never writes anything (readOnlyHint=true,
            # destructiveHint=false); calling it again with the same query
            # doesn't accumulate any state change even though the *answer*
            # can vary run to run (LLM ranking isn't perfectly
            # deterministic, and an ambiguous query needs a fresh human
            # answer each time) -- idempotentHint is about environment
            # side effects, not output stability, and there are none here;
            # openWorldHint=true because it calls out to a real external
            # system (Google Drive, plus whatever LLM the connected
            # client's sampling handler uses).
            annotations=types.ToolAnnotations(
                title="Search Drive files (sampling-ranked)",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        ),
        types.Tool(
            name="download_drive_file",
            title="Download a Drive file",
            description=(
                "Download a Drive file's content in chunks, reporting real "
                "progress as it goes. Supply _meta.progressToken on the "
                "request to receive notifications/progress per chunk. The "
                "slowest real operation this server has -- built to "
                "demonstrate progress notifications and cancellation, not "
                "just search."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Drive file ID (as returned by search_drive_files).",
                    }
                },
                "required": ["file_id"],
            },
            outputSchema=DOWNLOAD_DRIVE_FILE_OUTPUT_SCHEMA,
            # Same reasoning as search_drive_files above: read-only, not
            # destructive, no accumulating side effect from repeated calls
            # on the same file_id, and it's talking to a real external
            # system (Drive), hence openWorldHint=true.
            annotations=types.ToolAnnotations(
                title="Download a Drive file",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=True,
            ),
        ),
    ]


async def _download_drive_file(arguments: dict) -> dict:
    file_id = arguments["file_id"]
    ctx = server.request_context
    progress_token = ctx.meta.progressToken if ctx.meta is not None else None

    async def report_progress(progress: float, total: float | None) -> None:
        if progress_token is not None:
            await ctx.session.send_progress_notification(progress_token, progress=progress, total=total)

    logger.info("download_drive_file: starting file_id=%s", file_id)
    content, mime_type, name = await _fetch_drive_file_content(file_id, report_progress=report_progress)

    size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
    logger.info("download_drive_file: complete, %r, %d bytes total", name, size)
    return _build_download_result(file_id=file_id, name=name, mime_type=mime_type, size_bytes=size)


async def _fetch_relevance_snippet(file_id: str, mime_type: str) -> str | None:
    """A short content excerpt to send alongside name/mimeType when asking
    the client to rank + judge ambiguity -- without it, the sampling call
    below could only ever compare filenames, and "materially different
    content" (the whole point of the ambiguous_group case) isn't something
    a filename can prove either way.

    Only fetched for Google-native docs/sheets/slides -- a single
    `export()` call, already implemented by `_fetch_drive_file_content`.
    Skipped for regular files (e.g. this account's PDF): fetching and
    chunk-downloading a whole binary file just to slice off a snippet
    nobody asked to see would be real, unjustified extra work, and a
    binary file wouldn't `.decode("utf-8")` into a usable snippet anyway.
    """
    if mime_type not in GOOGLE_NATIVE_EXPORT_MIME_TYPES:
        return None
    content, _mime, _name = await _fetch_drive_file_content(file_id)
    return content[:400] if isinstance(content, str) else None


def _public_match(candidate: dict) -> dict:
    """Strip the internal-only `snippet` field before it ever reaches a
    tool result -- the tool's contract (see its description in
    handle_list_tools) is file_id/name/mimeType only; snippet exists
    purely to give the ranking sampling call something to judge content
    conflicts with, not to be echoed back to the caller.
    """
    return {"file_id": candidate["file_id"], "name": candidate["name"], "mimeType": candidate["mimeType"]}


async def _rank_candidates_by_relevance(query: str, candidates: list[dict]) -> tuple[list[str], list[str]]:
    """Ask the connected client's LLM to rank Drive files by relevance to
    `query`, via sampling/createMessage -- Step 6's primitive, unchanged
    in mechanism. Drive's own files().list(q=...) has no notion of
    relevance beyond a literal query match; ranking a full candidate pool
    by actual intent is something Drive's API can't do on its own, and
    doing it here would mean this server holding its own LLM credentials.
    Every ranking judgment is delegated to whichever client is connected --
    this function never calls an LLM API directly and this server never
    holds a model API key.

    Also flags genuine ambiguity -- two or more top candidates that are
    each plausibly what the user meant but whose actual content
    materially conflicts (see `_fetch_relevance_snippet` above for why
    content, not just names, is available to judge this). That's still
    the LLM's judgment call, made via sampling; *acting* on it by asking
    a human is a separate primitive (`_disambiguate_with_human`, below),
    not sampling doing double duty.

    Returns (ranked_file_ids, ambiguous_group) -- ranked_file_ids most
    relevant first with irrelevant candidates dropped, ambiguous_group a
    (possibly empty) list of file_ids the model flagged as genuinely
    conflicting. On an unparseable response, returns the original
    (unranked) order with an empty ambiguous_group, so a malformed
    response degrades to "no ranking, no ambiguity flagged" rather than
    losing the candidates entirely.
    """
    ctx = server.request_context

    # ServerCapabilities has no `sampling` field -- this check against the
    # *client's* declared capabilities is this server's explicit, enforced
    # statement of its dependency on the client for model access, checked
    # before ever attempting the call rather than left to surface as a
    # confusing low-level protocol error.
    if not ctx.session.check_client_capability(types.ClientCapabilities(sampling=types.SamplingCapability())):
        raise ValueError(
            "search_drive_files requires a client that declared the 'sampling' "
            "capability during initialize (sampling/createMessage support) -- "
            "the connected client did not."
        )

    def _candidate_line(c: dict) -> str:
        line = f"- file_id={c['file_id']!r} name={c['name']!r}"
        if c.get("snippet"):
            line += f" content_excerpt={c['snippet']!r}"
        return line

    candidate_lines = "\n".join(_candidate_line(c) for c in candidates)
    prompt = (
        f"Rank the following candidate Drive files by relevance to this query: {query!r}\n\n"
        f"Candidates:\n{candidate_lines}\n\n"
        'Respond with ONLY a JSON object of the shape {"ranked_file_ids": ["...", ...], '
        '"ambiguous_group": ["...", ...]}, most relevant first in ranked_file_ids, omitting '
        "any file that is not actually relevant to the query. Populate ambiguous_group ONLY "
        "if two or more of the top-ranked files are each plausibly what the user meant but "
        "their content_excerpt materially conflicts (e.g. different policy terms, different "
        "numbers) such that picking the wrong one would give the user a wrong answer -- in "
        "that case list exactly those file_ids (2 or more, all also present in "
        "ranked_file_ids). If there is no such genuine conflict, ambiguous_group must be an "
        "empty list -- do not flag ambiguity just because multiple files are relevant; only "
        "when their actual content disagrees. No other text, no markdown fences."
    )

    logger.info(
        "search_drive_files: sending sampling/createMessage (server -> client), %d candidates",
        len(candidates),
    )
    try:
        result = await ctx.session.create_message(
            messages=[types.SamplingMessage(role="user", content=types.TextContent(type="text", text=prompt))],
            # Higher than Step 6's 500: this step's prompt is longer (content
            # excerpts, ambiguity instructions) and gpt-oss-120b spends hidden
            # reasoning tokens against the same max_tokens budget before ever
            # emitting visible output -- 500 was observed (client.py's log)
            # producing a truncated, empty completion for the ambiguous-case
            # prompt specifically, which the parse-failure fallback below
            # silently absorbed as "no ranking, no ambiguity" rather than
            # surfacing it as the token-budget problem it actually was.
            max_tokens=1500,
            system_prompt=(
                "You rank search results for relevance and flag genuine content conflicts. "
                "Respond with strict JSON only, no other text."
            ),
        )
    except McpError as exc:
        # This is exactly what a denied sampling request looks like: the
        # client's approval gate (Step 6's sampling_handler) returns
        # ErrorData on "n", and create_message() raises McpError for any
        # error response (see BaseSession.send_request). WARNING, not
        # ERROR: a human denying a spend-money-on-a-model-call prompt is
        # an expected, correctly-working outcome of the approval gate, not
        # a server malfunction -- but it's operationally significant
        # enough to want above routine INFO tool-call bookkeeping. Re-
        # raised so the caller (handle_call_tool) still fails the tool
        # call the same way Step 6 established (isError=True via the
        # SDK's own uncaught-exception handling).
        logger.warning("search_drive_files: sampling request denied or failed (%s)", exc.error.message)
        raise

    logger.info(
        "search_drive_files: received sampling response (client -> server), model=%s",
        result.model,
    )

    response_text = result.content.text if isinstance(result.content, types.TextContent) else ""
    try:
        parsed = json.loads(response_text)
        ranked_ids = parsed["ranked_file_ids"]
        if not isinstance(ranked_ids, list):
            raise TypeError("ranked_file_ids was not a list")
        ambiguous_group = parsed.get("ambiguous_group", [])
        if not isinstance(ambiguous_group, list):
            raise TypeError("ambiguous_group was not a list")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning(
            "search_drive_files: could not parse ranking response (%s) -- falling back to unranked "
            "order, no ambiguity flagged: %r",
            exc,
            response_text,
        )
        return [c["file_id"] for c in candidates], []

    return ranked_ids, ambiguous_group


async def _disambiguate_with_human(query: str, ambiguous_candidates: list[dict]) -> str | None:
    """Ask a human, via elicitation/create, to choose among candidates the
    sampling call above flagged as genuinely ambiguous. This is Step 7's
    inversion, deliberately a separate function from
    `_rank_candidates_by_relevance`: that one calls a *model* and gets back
    unstructured text this server has to parse; this one calls a *human*
    and gets back an answer pre-validated against a schema this server
    declares -- an enum of exactly the ambiguous file_ids, not a free-text
    question. No model is involved in this function at all.

    Returns the chosen file_id on an explicit "accept", or None on
    "decline" or "cancel" -- both real, both required to be handled by the
    caller as "ambiguity unresolved", never as license to fall back to
    ambiguous_candidates[0].
    """
    ctx = server.request_context

    # Mirrors the sampling capability check above, for the client's
    # elicitation support instead -- this server's explicit, enforced
    # statement that resolving a genuinely ambiguous match depends on a
    # human at the other end of this session, not a guess made in here.
    if not ctx.session.check_client_capability(types.ClientCapabilities(elicitation=types.ElicitationCapability())):
        raise ValueError(
            "search_drive_files needs to disambiguate a genuinely ambiguous match and requires "
            "a client that declared the 'elicitation' capability during initialize "
            "(elicitation/create support) -- the connected client did not."
        )

    candidate_ids = [c["file_id"] for c in ambiguous_candidates]
    options = "\n".join(
        f"  {i}. {c['name']!r} (file_id={c['file_id']!r})" for i, c in enumerate(ambiguous_candidates, start=1)
    )
    message = (
        f"Your search for {query!r} matched multiple Drive files that are each plausibly "
        f"relevant, but their actual content materially conflicts:\n\n{options}\n\n"
        "Which one did you actually mean? You may decline if neither is right."
    )
    requested_schema = {
        "type": "object",
        "properties": {
            "chosen_file_id": {
                "type": "string",
                "title": "Which file did you mean?",
                "description": "The file_id of the candidate that actually answers your search.",
                "enum": candidate_ids,
            }
        },
        "required": ["chosen_file_id"],
    }

    logger.info(
        "search_drive_files: sending elicitation/create (server -> client), %d ambiguous candidates: %s",
        len(ambiguous_candidates),
        candidate_ids,
    )
    result = await ctx.session.elicit_form(message=message, requestedSchema=requested_schema)
    logger.info(
        "search_drive_files: received elicitation response (client -> server), action=%s content=%s",
        result.action,
        result.content,
    )

    if result.action != "accept":
        # decline (explicit "neither of these") or cancel (dismissed with
        # no choice made) -- distinct from each other in the log above,
        # and both distinct from a chosen file_id. Neither is treated as
        # equivalent to picking candidates[0]; see handle_call_tool. Logged
        # at WARNING (not INFO): the ambiguity is now unresolved and the
        # caller gets an abstain-marker instead of an answer -- worth more
        # visibility than routine tool-call bookkeeping, even though
        # declining is a legitimate, correctly-handled outcome and not a
        # malfunction.
        logger.warning(
            "search_drive_files: elicitation not accepted (action=%s) -- ambiguity will be left unresolved",
            result.action,
        )
        return None

    chosen = result.content.get("chosen_file_id") if result.content else None
    if chosen not in candidate_ids:
        # Defense in depth: trust the schema, but verify the client
        # actually honored it rather than assuming compliance -- an
        # out-of-enum answer is treated the same as no answer at all.
        logger.warning(
            "search_drive_files: elicitation accepted but chosen_file_id %r was not one of the "
            "offered candidates %s -- treating as unresolved rather than trusting an "
            "out-of-schema answer",
            chosen,
            candidate_ids,
        )
        return None

    return chosen


async def _search_drive_files_tool(query: str) -> dict:
    # Candidate pool: every file in the account, not a narrow Drive query
    # match -- this account has 4 files, so listing all of them and letting
    # sampling rank/filter is a reasonable demo-scale retrieval strategy.
    # A larger corpus would want a real pre-filter (Drive fullText search)
    # before handing candidates to sampling; that's out of scope here,
    # which is about the ranking primitive, not a full retrieval pipeline.
    response = drive_service.files().list(pageSize=20, fields="files(id, name, mimeType)").execute()
    candidates = [
        {"file_id": file["id"], "name": file["name"], "mimeType": file.get("mimeType")}
        for file in response.get("files", [])
    ]
    for candidate in candidates:
        candidate["snippet"] = await _fetch_relevance_snippet(candidate["file_id"], candidate["mimeType"])

    # Logged before the sampling call so it's a clean "before" to diff
    # against the ranked "after" below -- the actual proof that ranking
    # changed the output, not just that a round-trip happened.
    logger.info("search_drive_files: raw Drive order (unranked): %s", [c["name"] for c in candidates])

    ranked_ids, ambiguous_group = await _rank_candidates_by_relevance(query, candidates)

    by_id = {c["file_id"]: c for c in candidates}
    ranked_matches = [by_id[file_id] for file_id in ranked_ids if file_id in by_id]
    logger.info("search_drive_files: final ranked order: %s", [m["name"] for m in ranked_matches])

    valid_ambiguous_ids = [fid for fid in ambiguous_group if fid in by_id]
    if len(valid_ambiguous_ids) < 2:
        if ambiguous_group:
            logger.warning(
                "search_drive_files: sampling flagged ambiguous_group %s but fewer than 2 resolved "
                "to real candidates -- ignoring, not enough to actually be ambiguous",
                ambiguous_group,
            )
        return _build_matched_result([_public_match(m) for m in ranked_matches])

    ambiguous_candidates = [by_id[fid] for fid in valid_ambiguous_ids]
    logger.info(
        "search_drive_files: sampling flagged genuine ambiguity among %s -- pausing for elicitation/create "
        "instead of guessing",
        [c["name"] for c in ambiguous_candidates],
    )
    chosen_file_id = await _disambiguate_with_human(query, ambiguous_candidates)

    if chosen_file_id is None:
        logger.info(
            "search_drive_files: human did not resolve the ambiguity (declined/cancelled) -- "
            "abstaining rather than guessing which candidate was meant"
        )
        return _build_ambiguous_result(
            [_public_match(c) for c in ambiguous_candidates],
            message=(
                "Multiple files plausibly matched this query with conflicting content, and the "
                "human declined or cancelled disambiguation. Abstaining rather than guessing -- "
                "read each candidate (resources/read on its gdrive:///{file_id} URI) before acting."
            ),
        )

    logger.info("search_drive_files: human chose file_id=%s", chosen_file_id)
    final_ids = [chosen_file_id] + [
        fid for fid in ranked_ids if fid in by_id and fid != chosen_file_id and fid not in valid_ambiguous_ids
    ]
    return _build_matched_result([_public_match(by_id[fid]) for fid in final_ids])


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> dict:
    logger.debug("handling tools/call: name=%s arguments=%s", name, arguments)
    try:
        if name == "download_drive_file":
            return await _download_drive_file(arguments)
        if name != "search_drive_files":
            raise ValueError(f"Unknown tool: {name}")
        return await _search_drive_files_tool(arguments["query"])
    except McpError:
        # Already logged at WARNING inside _rank_candidates_by_relevance --
        # a denied/failed sampling request is an expected, correctly-
        # handled outcome of the approval gate, not a server malfunction,
        # so it doesn't also get an ERROR line here on top of that.
        raise
    except Exception:
        # Anything else escaping a tool call is a real failure, not an
        # expected branch (decline/cancel/denial all return normally or
        # raise McpError, both handled above) -- ERROR, with a traceback,
        # is what build task 3 asks for here. The SDK's own call_tool
        # wrapper still converts this into CallToolResult(isError=True)
        # for the caller; this log line is what makes the failure visible
        # server-side without needing to inspect that response.
        logger.error("tools/call %s failed unexpectedly", name, exc_info=True)
        raise


# --- ASGI app assembly: transport, optionally wrapped in OAuth 2.1 ---------


class _StreamableHTTPEndpoint:
    """Thin ASGI-callable wrapper around the session manager.

    Deliberately a class instance, not a bare `async def` function:
    Starlette's Route treats a plain function endpoint as `func(request)
    -> response` and defaults its allowed methods to GET-only, which
    would silently 405 every POST (the actual JSON-RPC request path).
    A class instance with __call__ is instead treated as a raw ASGI app
    (scope, receive, send) with no method restriction -- the same
    distinction the SDK's own StreamableHTTPASGIApp relies on.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self.session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def build_app(*, host: str, port: int, require_auth: bool) -> Starlette:
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)
    streamable_http_endpoint = _StreamableHTTPEndpoint(session_manager)

    routes: list[Route] = []
    middleware: list[Middleware] = []

    if require_auth:
        base_url = AnyHttpUrl(f"http://{host}:{port}")
        resource_url = AnyHttpUrl(f"{base_url}".rstrip("/") + MCP_PATH)
        signing_secret = secrets.token_urlsafe(48)
        logger.info(
            "OAuth enabled: issuer=%s resource=%s (in-memory signing key, regenerated each run)",
            base_url,
            resource_url,
        )

        # This server acts as its own resource owner (see oauth/provider.py's
        # module docstring for why) -- fetch the authenticated Drive
        # account's own address to use as the token's `sub` claim, rather
        # than a placeholder string.
        try:
            about = drive_service.about().get(fields="user").execute()
            resource_owner_subject = about["user"]["emailAddress"]
        except HttpError:
            resource_owner_subject = "resource-owner"

        provider = DriveOAuthProvider(
            issuer_url=str(base_url),
            resource_url=str(resource_url),
            signing_secret=signing_secret,
            consent_url=f"{str(base_url).rstrip('/')}/consent",
        )
        token_verifier = ProviderTokenVerifier(provider)

        middleware = [
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(token_verifier)),
            Middleware(AuthContextMiddleware),
        ]

        routes.extend(
            create_auth_routes(
                provider=provider,
                issuer_url=base_url,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=[REQUIRED_SCOPE],
                    default_scopes=[REQUIRED_SCOPE],
                ),
            )
        )
        routes.extend(create_consent_routes(provider, resource_owner_subject))

        resource_metadata_url = build_resource_metadata_url(resource_url)
        routes.append(
            Route(
                MCP_PATH,
                endpoint=RequireAuthMiddleware(streamable_http_endpoint, [REQUIRED_SCOPE], resource_metadata_url),
            )
        )
        routes.extend(
            create_protected_resource_routes(
                resource_url=resource_url,
                authorization_servers=[base_url],
                scopes_supported=[REQUIRED_SCOPE],
                resource_name="Inbox Guardian Drive MCP server",
            )
        )
    else:
        logger.warning(
            "OAuth DISABLED (--no-auth) -- transport-only mode for isolating transport bugs from "
            "auth bugs. Not how this server ships; see README."
        )
        routes.append(Route(MCP_PATH, endpoint=streamable_http_endpoint))

    app = Starlette(routes=routes, middleware=middleware, lifespan=lambda _app: session_manager.run())
    return _WireLoggingMiddleware(app)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Serve the /mcp endpoint with no OAuth layer at all -- for isolating transport bugs "
        "from auth bugs during development only. Never how this server is meant to run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reads (search, resources/read, download_drive_file) still execute. Any code path that "
        "would write/send/modify state instead raises DryRunBlocked via _guard_write -- see that "
        "function's docstring. No tool in this server writes anything yet; this flag exists so the "
        "guard rail is built and tested (test_dry_run_guard.py) before Step 9's Dispatcher needs it "
        "for real.",
    )
    parser.add_argument(
        "--log-level",
        default=_DEFAULT_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Overrides the LOG_LEVEL environment variable. DEBUG shows raw JSON-RPC wire frames and "
        "resources/list-tools/list handling on stderr in addition to the jsonrpc.log file (which "
        "always gets the full frames regardless of this setting).",
    )
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    logger.info("log level set to %s", args.log_level)

    global DRY_RUN
    DRY_RUN = args.dry_run
    if DRY_RUN:
        logger.warning("--dry-run is active: any write-capable code path will be refused, not executed")

    global drive_service
    creds = _load_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    logger.info(
        "authenticated with Drive; starting Streamable HTTP server on %s:%d (version=%s)",
        args.host,
        args.port,
        _GIT_SHA,
    )

    app = build_app(host=args.host, port=args.port, require_auth=not args.no_auth)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    uv_server = uvicorn.Server(config)
    try:
        await uv_server.serve()
    finally:
        logger.info("server stopped")
        _rpc_log.close()


if __name__ == "__main__":
    anyio.run(main)
