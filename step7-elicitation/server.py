"""Drive MCP server — Inbox Guardian Step 7, elicitation.

Carries Step 6's sampling-ranked server forward unmodified in every
respect except one: `_rank_candidates_by_relevance`'s sampling call now
asks the client's LLM not just to rank candidates, but to flag when two
or more top candidates are *genuinely* ambiguous -- plausibly relevant to
the query, but materially different in what they'd tell the user (e.g.
two refund-policy docs with conflicting refund windows and remedies).
When that happens, the server does not pick one and does not average
them. It stops mid-tool-call and sends `elicitation/create`, asking a
*human* to choose -- a second, independent inversion of the normal
request direction, alongside sampling.

Sampling and elicitation are easy to conflate ("the server asks the
client for something mid-flight") but they are not the same primitive:

  - sampling/createMessage asks the client's *model* to produce a
    completion -- no human is necessarily involved, and the answer is
    unstructured text the server has to parse.
  - elicitation/create asks the client to collect a *human's* answer,
    against a typed JSON-Schema-shaped response the server declares up
    front (here: an enum of the ambiguous file_ids) -- no model is
    involved, and the answer is schema-validated before the server ever
    sees it.

This server now depends on the client for both, and checks both
independently via `session.check_client_capability(...)` before ever
attempting either call (`_rank_candidates_by_relevance` for sampling,
`_disambiguate_with_human` for elicitation, below) -- exactly Step 6's
pattern, since neither has a slot on `mcp.types.ServerCapabilities`
either (only `ClientCapabilities` declares support for either one).
"""

import argparse
import io
import json
import logging
import secrets
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

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
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from oauth.consent import create_consent_routes
from oauth.provider import DriveOAuthProvider

logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stderr,
    format="%(asctime)s [server] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step5.drive-server")

STEP_DIR = Path(__file__).parent
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
DOWNLOAD_CHUNK_PACING_SECONDS = 0.35  # artificial; see README's "what's synthetic here" note


def _log_wire(direction: str, raw: str) -> None:
    """Record one line crossing the /mcp HTTP boundary, either direction.

    Unlike Steps 1-3's stdio pump (which re-serialized parsed
    SessionMessage objects), this logs the literal bytes Starlette
    reads off/writes to the socket -- POST request bodies (JSON-RPC
    requests) and each SSE `data: ...` write (JSON-RPC responses/
    notifications) -- via ASGI-level receive/send wrapping in
    _WireLoggingMiddleware below.
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


server = Server("inbox-guardian-drive-http")
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
        logger.info("_fetch_drive_file_content: requesting chunk %d of %r", chunk_index, name)

        # abandon_on_cancel=True: if notifications/cancelled arrives while
        # this chunk's network call is in flight, the await raises
        # immediately instead of waiting for the (unkillable) worker
        # thread to finish -- we stop issuing further chunk requests and
        # never build/return a result, which is the real proof cancellation
        # actually stopped work rather than just the caller giving up on
        # waiting for a response that was coming anyway.
        status, done = await anyio.to_thread.run_sync(downloader.next_chunk, abandon_on_cancel=True)

        logger.info(
            "_fetch_drive_file_content: chunk %d received (%d / %s bytes, %.0f%%)",
            chunk_index,
            status.resumable_progress,
            status.total_size,
            status.progress() * 100,
        )

        if report_progress is not None:
            await report_progress(status.resumable_progress, status.total_size)
            # Artificial pacing -- see README's "what's synthetic here" note.
            # Only applied when a caller is actually listening for progress
            # (i.e. the download_drive_file tool, not resources/read): a
            # 128KiB chunk of this file arrives fast enough on a real
            # connection that there's no reliable window to send
            # notifications/cancelled mid-flight without it. The Drive API
            # call itself (MediaIoBaseDownload.next_chunk) is entirely real.
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
        raise ValueError(f"Failed to read Drive file {file_id!r}: {exc}") from exc


# --- Tools: search (sampling-ranked, elicitation on ambiguity) + download_drive_file --


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    logger.debug("handling tools/list")
    return [
        types.Tool(
            name="search_drive_files",
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
        ),
        types.Tool(
            name="download_drive_file",
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
        ),
    ]


async def _download_drive_file(arguments: dict) -> list[types.TextContent]:
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
    return [types.TextContent(type="text", text=f"Downloaded {name!r}: {size} bytes ({mime_type})")]


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

    New this step: the same sampling call is also asked to flag genuine
    ambiguity -- two or more top candidates that are each plausibly what
    the user meant but whose actual content materially conflicts (see
    `_fetch_relevance_snippet` above for why content, not just names, is
    available to judge this). That's still the LLM's judgment call, made
    via sampling; *acting* on it by asking a human is a separate primitive
    (`_disambiguate_with_human`, below), not sampling doing double duty.

    Returns (ranked_file_ids, ambiguous_group) -- ranked_file_ids most
    relevant first with irrelevant candidates dropped, ambiguous_group a
    (possibly empty) list of file_ids the model flagged as genuinely
    conflicting. On an unparseable response, returns the original
    (unranked) order with an empty ambiguous_group, so a malformed
    response degrades to "no ranking, no ambiguity flagged" rather than
    losing the candidates entirely.
    """
    ctx = server.request_context

    # ServerCapabilities has no `sampling` field (see module docstring) --
    # this check against the *client's* declared capabilities is this
    # server's explicit, enforced statement of its dependency on the
    # client for model access, checked before ever attempting the call
    # rather than left to surface as a confusing low-level protocol error.
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
    sampling call above flagged as genuinely ambiguous. This is the second
    inversion this step adds, and deliberately a separate function from
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
        # equivalent to picking candidates[0]; see handle_call_tool.
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


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    logger.debug("handling tools/call: name=%s arguments=%s", name, arguments)

    if name == "download_drive_file":
        return await _download_drive_file(arguments)

    if name != "search_drive_files":
        raise ValueError(f"Unknown tool: {name}")

    query = arguments["query"]

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
        return [types.TextContent(type="text", text=json.dumps([_public_match(m) for m in ranked_matches], indent=2))]

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
        result_payload = {
            "ambiguous": True,
            "resolved": False,
            "candidates": [_public_match(c) for c in ambiguous_candidates],
            "message": (
                "Multiple files plausibly matched this query with conflicting content, and the "
                "human declined or cancelled disambiguation. Abstaining rather than guessing -- "
                "read each candidate (resources/read on its gdrive:///{file_id} URI) before acting."
            ),
        }
        return [types.TextContent(type="text", text=json.dumps(result_payload, indent=2))]

    logger.info("search_drive_files: human chose file_id=%s", chosen_file_id)
    final_ids = [chosen_file_id] + [
        fid for fid in ranked_ids if fid in by_id and fid != chosen_file_id and fid not in valid_ambiguous_ids
    ]
    result_payload = [_public_match(by_id[fid]) for fid in final_ids]
    return [types.TextContent(type="text", text=json.dumps(result_payload, indent=2))]


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
                resource_name="Inbox Guardian Drive MCP server (Step 5)",
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
    args = parser.parse_args()

    global drive_service
    creds = _load_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    logger.info("authenticated with Drive; starting Streamable HTTP server on %s:%d", args.host, args.port)

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
