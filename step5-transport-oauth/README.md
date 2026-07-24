# Step 5 — Transport & streaming: stdio → Streamable HTTP + real MCP OAuth 2.1

A new variant of the Drive server (Step 3's `step3-real-servers/drive-server/server.py`
copied, not edited in place — that copy stays stdio and stays signed off
as-is). This one swaps the transport to Streamable HTTP and puts real
MCP-level OAuth 2.1 in front of it, then adds progress notifications and
cancellation to a genuinely slow operation. Gmail and the toy server were
untouched this step.

## Files

- `server.py` — the Drive server over Streamable HTTP, with the OAuth
  layer wired in. `--no-auth` disables auth entirely, for isolating
  transport bugs from auth bugs (never how this server ships).
- `oauth/provider.py` — the authorization-server implementation
  (`OAuthAuthorizationServerProvider`): client registry, the interactive
  consent hand-off, and JWT access/refresh token issuance/verification
  via `joserfc` (authlib's own JOSE implementation).
- `oauth/consent.py` — the interactive consent screen (`GET`/`POST
  /consent`) a human actually has to approve before a code is issued.
- `client.py` — connects over Streamable HTTP, performs the real OAuth
  2.1 dance via the SDK's own `OAuthClientProvider`, then demonstrates
  progress notifications and cancellation.
- `test_shared_download.py` — regression test proving
  `resources/read` and the `download_drive_file` tool share one Drive-
  fetching implementation (see "Bug found and fixed" below).
- `jsonrpc.log` — every HTTP request/response body crossing this
  server's boundary, tagged by path, gitignored (contains real Drive/
  OAuth content).

## Why this step needed OAuth 2.1 and Step 3 didn't

stdio has no network trust boundary. The "client" in Steps 1-4 is a
subprocess *this same machine* spawned, talking over a pipe nothing else
can reach — there is no one else on the other end to authenticate, so
"is this caller allowed to act as this resource owner" isn't a question
stdio ever has to answer.

Streamable HTTP puts this server on a socket: `http://127.0.0.1:8770/mcp`
is reachable by anything that can open a TCP connection to that port,
not just a process this script spawned itself. The transport genuinely
changed what's on the other end of the wire, from "a child process I
just started" to "an arbitrary caller" — and RFC 9728 protected-resource
discovery + PKCE-based auth code flow + per-request bearer token
validation is what answers "is this caller actually authorized" for that
case. This is Step 3's deferred question, answered with a real
implementation instead of a note to revisit later.

## Build order (and why it matters)

The task list says to get the transport working unauthenticated first,
prove progress/cancellation, and only then add OAuth — deliberately, so
a bug never gets misattributed to the wrong layer. This was worth
following literally:

1. **Transport, unauthenticated** (`--no-auth`):

   ```
   curl -s -i -X POST http://127.0.0.1:8770/mcp \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-test","version":"0.1"}}}'
   ```

   Returned a real `initialize` JSON-RPC response over SSE (`event:
   message` / `data: {...}`), confirmed before writing any client code.
   The first attempt at this actually 405'd — Starlette treats a plain
   `async def` endpoint as `func(request) -> response` and defaults its
   allowed methods to GET-only, silently rejecting every POST (the
   entire JSON-RPC request path). Fixed by wrapping the session
   manager's `handle_request` in a class instance with `__call__`
   (`_StreamableHTTPEndpoint`), the same pattern the SDK's own
   `StreamableHTTPASGIApp` uses — a class instance is treated as a raw
   ASGI app with no method restriction, a bare function isn't.

2. **Progress + cancellation**, still unauthenticated, isolating those
   from OAuth entirely (see below).

3. **OAuth 2.1**, layered on top only once 1 and 2 were independently
   solid.

## Progress notifications

`download_drive_file` downloads a real Drive file in 128KiB chunks via
`googleapiclient.http.MediaIoBaseDownload`, reporting genuine
byte-level progress (not synthetic percentages) after every chunk via
`notifications/progress`, keyed to the `progressToken` the client
supplies in `_meta`. A real run against a 1.9MB PDF:

```
[progress] 131072/1903362.0 bytes (7%)
[progress] 262144/1903362.0 bytes (14%)
...
[progress] 1903362/1903362.0 bytes (100%)
final result: Downloaded 'UCLA&MIT.pdf': 1903362 bytes (application/pdf)
OK: received 15 progress notifications before the final response.
```

This is the first primitive in the whole project that's asynchronous
relative to its request/response pair: 15 `notifications/progress`
frames arrive on the SSE stream *before* the single `tools/call`
response that request eventually gets — `client.py`'s progress
callback prints each one as it lands, not after the call returns.

**What's synthetic here:** a 128KiB chunk of this file arrives fast
enough on a real connection that there's no reliable window to send
`notifications/cancelled` mid-flight without some pacing. `server.py`
adds an artificial `await anyio.sleep(0.35)` between chunks — gated so
it only applies when a caller is actually listening for progress (i.e.
the tool, not `resources/read`) — purely to make the demo/cancellation
window observable. The Drive API calls themselves
(`MediaIoBaseDownload.next_chunk`) are entirely real; only the pacing
between them is added.

## Cancellation

`client.py` calls `download_drive_file`, and after the 2nd progress
notification, sends `notifications/cancelled` referencing that
request's id — read directly off `session._request_id` immediately
before issuing the call, since `send_request()` assigns it
synchronously and the public API has no other way to learn a call's id
ahead of completion.

The first version of this test asserted the wrong thing: it assumed the
server would never respond to a cancelled request, so it force-cancelled
the client's own wait locally. Running it against the real server showed
that assumption was wrong — the SDK's own session logic
(`RequestResponder.cancel`) answers a cancelled request with an explicit
`{"error": {"code": 0, "message": "Request cancelled"}}` *immediately*,
independent of whether the handler has actually stopped. That response
alone is **not** proof the server-side work stopped — only the
handler's own log is:

```
[server] INFO _fetch_drive_file_content: requesting chunk 1 of 'UCLA&MIT.pdf'
[server] INFO _fetch_drive_file_content: chunk 1 received (131072 / 1903362 bytes, 7%)
[server] INFO _fetch_drive_file_content: requesting chunk 2 of 'UCLA&MIT.pdf'
[server] INFO _fetch_drive_file_content: chunk 2 received (262144 / 1903362 bytes, 14%)
[server] DEBUG >>> POST /mcp {"method":"notifications/cancelled","params":{"requestId":4,...}}
[server] INFO Request 4 cancelled - duplicate response suppressed
[server] DEBUG <<< /mcp data: {"jsonrpc":"2.0","id":4,"error":{"code":0,"message":"Request cancelled"}}
```

No `requesting chunk 3` line ever appears. The handler's `while not
done` loop, awaiting `anyio.to_thread.run_sync(downloader.next_chunk,
abandon_on_cancel=True)`, was cancelled *before* issuing its next Drive
API call — real work stopped, not just a response arriving quickly
while a download kept running in the background. `client.py`'s own
`except McpError` branch (catching the SDK's immediate error response)
is genuinely exercised, not dead code — an earlier version of this
demo had that branch unreachable because the client force-cancelled
itself before the server's response could ever arrive.

## OAuth 2.1

- **RFC 9728 protected-resource metadata** — `GET
  /.well-known/oauth-protected-resource/mcp`:
  ```json
  {"resource": "http://127.0.0.1:8770/mcp",
   "authorization_servers": ["http://127.0.0.1:8770/"],
   "scopes_supported": ["drive:read"],
   "bearer_methods_supported": ["header"],
   "resource_name": "Inbox Guardian Drive MCP server (Step 5)"}
  ```
- **RFC 8414 authorization-server metadata** — `GET
  /.well-known/oauth-authorization-server` advertises `/authorize`,
  `/token`, `/register`, `code_challenge_methods_supported: ["S256"]`.
- **No exceptions, including initialize** — the entire `/mcp` route is
  wrapped in the SDK's own `RequireAuthMiddleware`; a request with no
  token gets:
  ```
  HTTP/1.1 401 Unauthorized
  www-authenticate: Bearer error="invalid_token", error_description="Authentication required",
    resource_metadata="http://127.0.0.1:8770/.well-known/oauth-protected-resource/mcp"
  {"error": "invalid_token", "error_description": "Authentication required"}
  ```
  A structured JSON body and a spec-correct `WWW-Authenticate` pointer —
  not a crash, not an opaque error. This is how a real MCP client knows
  where to go authenticate.
- **PKCE + dynamic client registration** — `oauth/provider.py`
  implements the `OAuthAuthorizationServerProvider` protocol
  (`register_client`, `authorize`, `exchange_authorization_code`,
  `load_access_token`, etc.). PKCE verification itself (matching the
  token request's `code_verifier` against the `/authorize` request's
  `code_challenge`) is **not** reimplemented — the SDK's own
  `TokenHandler` already does that (SHA256 + base64url compare per RFC
  7636) before `exchange_authorization_code` is ever called.
- **Interactive consent** — this server is both its own authorization
  server and its own resource owner (no third-party IdP to federate to
  for a single-user portfolio project). `authorize()` doesn't decide
  approval itself; it stashes the pending request and redirects to
  `oauth/consent.py`'s own `GET /consent` page, which only calls back
  into the provider to mint a code once a human actually clicks Approve.
- **Token issuance/verification via `joserfc`** — `authlib`'s own JOSE
  implementation (its dependency, and the module `authlib.jose` itself
  now points people to instead, to avoid its deprecation warning).
  Access tokens are short-lived signed JWTs carrying `aud`, `scope`,
  `sub`, `jti`. No token cryptography is hand-rolled.

### Audience/signature/revocation — not just "a token exists"

The pitfall named for this step is confusing "the server has a bearer
token" with "the token is valid for this resource." Tested directly
against `DriveOAuthProvider.load_access_token`, not inferred:

```
correctly-audienced token -> AccessToken(...)
wrong-audience (but validly signed) token -> None
tampered-signature token -> None
same token after revoke_token() -> None
ALL AUDIENCE/SIGNATURE/REVOCATION CHECKS PASSED
```

The second case is the important one: a token signed with the *same*
key (so its signature is genuinely valid) but minted for a different
`resource_url` is still rejected, because `load_access_token` checks
`claims.get("aud") != self._resource_url` explicitly — a syntactically
and cryptographically valid token is not automatically a token valid
*here*.

### Fail closed, not silently unauthenticated

`load_access_token` wraps verification in a single broad
`except Exception: return None` — any decode failure, expired token, or
unexpected error all produce the same outcome: reject. There is no code
path where an OAuth-layer error results in the request being treated as
authenticated, or in falling through to unauthenticated access. A
garbage bearer token (`Authorization: Bearer this.is.garbage`) gets a
clean `401`, never a 500 or a bypass:

```
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer error="invalid_token", ...
```

### Full flow, via our own client

`client.py` uses `mcp.client.auth.oauth2.OAuthClientProvider` (an
`httpx.Auth` implementation already part of the `mcp` SDK — SDK
machinery, not a vendored auth layer) to perform the entire dance. A
real automated run (browser interaction stubbed out with direct HTTP
calls that do exactly what a human clicking through the consent page
would do, so the mechanics are exercised without popping an unexpected
browser window during automated verification):

```
POST /mcp -> 401
GET /.well-known/oauth-protected-resource/mcp -> 200
GET /.well-known/oauth-authorization-server -> 200
POST /register -> 201 Created
GET /authorize?...code_challenge=...&code_challenge_method=S256... -> 302 -> /consent
GET /consent?request_id=... -> 200 (renders Approve/Deny)
POST /consent (approve) -> 302 -> http://127.0.0.1:8771/callback?code=...&state=...
GET http://127.0.0.1:8771/callback?... -> 200 (local callback server captures code)
POST /token -> 200 (JWT access + refresh token)
POST /mcp (Authorization: Bearer ...) -> 200 (initialize succeeds)
```

Every subsequent call (`tools/list`, `search_drive_files`,
`download_drive_file` with progress, the cancellation demo) then ran
authenticated, identically to the unauthenticated runs used to prove
the transport and progress/cancellation layers independently.

## MCP Inspector — independent verification (not skipped)

The task's own pitfall list warns against trusting only your own
client's success as evidence — the same mistake Step 2's shared-lookup
incident already taught this project not to make. Inspector was run
against this server directly (`Streamable HTTP`, `http://127.0.0.1:8770/mcp`),
independent of `client.py`, and it found two real bugs `client.py`
alone never would have:

**1. Inspector's own proxy, not our server, on the first attempt.**
Inspector reported `404: Cannot POST /register` — but that phrasing is
Express.js's default 404, not anything Starlette produces. Direct
`curl`/CORS-preflight/browser-header checks against our server's
`/register` all succeeded (`201 Created`), so the failure wasn't ours.
Checking the browser's Network tab confirmed it: the failing request
was `GET http://localhost:6277/.well-known/oauth-protected-resource/mcp?...`
— `6277` is Inspector's own local proxy port, not `8770`. A fresh
Inspector instance (same latest published version, 1.0.0, clean
process state) resolved it on retry. Worth recording as a real finding
from using Inspector independently, even though the fix wasn't ours to
make.

**2. `download_drive_file` 403'd on a Google-native file.** Calling the
tool against a real Google Doc through Inspector surfaced `HttpError
403: Only files with binary content can be downloaded. Use Export with
Docs Editors files.` — `_download_drive_file` called `files().get_media()`
unconditionally, never branching on mimeType the way `resources/read`
already did correctly. **Fixed** by extracting one shared helper,
`_fetch_drive_file_content(file_id, *, report_progress=None)`, that
both `resources/read` and `download_drive_file` now call — not two
copies of "how to fetch a Drive file" that can silently drift apart
again, the same principle as Step 2's `_read_ticket_resource` fix.
`test_shared_download.py` proves the shared path (not just matching
output) by patching the helper with a `wraps=` spy and asserting both
callers invoke it, for both a real PDF and a real Google Doc.

After the fix, the full sequence completed cleanly via a fresh Inspector
instance, independent of `client.py`:

- 401 → protected-resource discovery → AS discovery → dynamic client
  registration → PKCE `/authorize` → consent screen rendered and
  approved by a human → token exchange → retried request succeeded.
- Inspector's `tools/list` matched `client.py`'s exactly
  (`search_drive_files`, `download_drive_file`).
- `download_drive_file` called against the Google Doc through
  Inspector — a genuinely separate client implementation exercising the
  exact code path that was broken — returned `Downloaded 'Refund
  Policy': 1799 bytes (text/plain)`, no error. This is the strongest
  evidence in this step: not "Inspector connected," but Inspector
  independently succeeding at the specific call that used to fail.

## Running it

Drive credentials must already exist at `~/.drive-mcp/token.json` (Step
3's `auth_setup.py`). From this directory:

```
../.venv/bin/python server.py                 # OAuth required (default, how this ships)
../.venv/bin/python server.py --no-auth        # transport-only, isolating transport from auth bugs
../.venv/bin/python client.py                  # full OAuth dance + progress + cancellation demo
../.venv/bin/python test_shared_download.py    # shared-implementation regression test
```

`client.py` opens a real browser for consent; approve the request shown
(`drive:read` scope) to let the flow complete.

## Review checklist

- [x] Transport verified working unauthenticated before OAuth was
      added — bare `curl POST /mcp` returned a real `initialize`
      response with `--no-auth`, before any client code existed.
- [x] Progress notifications observed arriving during a request, not
      just present in code — 15 real `notifications/progress` frames
      printed live, before the final `tools/call` response.
- [x] Cancellation actually stops server-side work, proven via
      logs — no `requesting chunk 3` line ever appears after
      `notifications/cancelled` is sent; the SDK's immediate
      cancellation-error response is not, by itself, that proof.
- [x] A request with no token gets a proper 401 + resource-metadata
      pointer, not a crash or opaque error — confirmed via curl, and a
      garbage bearer token gets the same clean 401 (fail closed).
- [x] Can explain why this step needed OAuth 2.1 and Step 3 didn't:
      stdio has no network trust boundary (the "client" is a subprocess
      this process spawned itself); Streamable HTTP puts this server on
      a socket anything can reach, making "is this caller authorized"
      a real question for the first time. See above.

## Pitfalls addressed

- **Testing OAuth against only our own client** — avoided by running
  Inspector independently, which found two real bugs (its own proxy
  issue, and the `download_drive_file` 403) that never surfaced through
  `client.py` alone.
- **Silently falling back to unauthenticated access on an OAuth-layer
  error** — `load_access_token`'s single broad `except Exception:
  return None` fails closed in every case; there is no path from a
  verification error to treating a request as authenticated.
- **Confusing "has a bearer token" with "valid for this resource"** —
  `load_access_token` checks `aud` against this server's own
  `resource_url` explicitly; tested directly with a validly-signed
  token minted for a different resource, and rejected.
- **Untested-branch trap (again)** — `download_drive_file`'s
  Google-native-file bug is exactly this: code that looked plausible
  but was never run against a Doc/Sheet/Slide until Inspector's
  independent click-through exercised it.
