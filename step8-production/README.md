# Step 8 — Production hardening

Every step through 7 proved a *capability* works. This step proves
nothing new works — `server.py` carries Steps 5-7's actual behavior
(Streamable HTTP, OAuth 2.1, sampling-ranked search, elicitation on
ambiguity, progress, cancellation) forward completely unmodified — and
instead hardens what's already there so a stranger can run it against a
real account without babysitting it: honest tool annotations, validated
structured output, real logging, a tested `--dry-run` guard rail, and
git-SHA versioning.

## Files

- `server.py` — Step 7's server, with the five build tasks below layered
  on. No tool's actual behavior changed; `handle_call_tool` was split
  into a thin dispatcher (`handle_call_tool`) plus the pre-existing
  search logic (now `_search_drive_files_tool`) purely so both branches
  share one place to log unexpected failures at ERROR — see "Real
  logging" below.
- `oauth/` — unchanged from Steps 5-7.
- `client.py` — Step 7's client, plus printing `serverInfo.version` and
  each tool's annotations at `initialize`/`tools/list` time (the two
  things a stranger connecting would actually want to see), and reading
  `result.structuredContent` directly instead of re-parsing
  `result.content[0].text` now that the server publishes it natively.
- `test_output_schemas.py` — in-process proof that a malformed tool
  result is rejected at the schema boundary, two independent ways.
- `test_dry_run_guard.py` — in-process proof `--dry-run` actually blocks
  a (synthetic) write-capable code path, not just documents that it
  would.

## 1. Tool annotations

Both tools got `types.ToolAnnotations` in `handle_list_tools`:

```python
annotations=types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
),
```

Reasoning, not a guess — per spec (`mcp.types.ToolAnnotations`'
docstring), `destructiveHint`/`idempotentHint` are only *meaningful*
when `readOnlyHint=False`; both tools genuinely never write anything
(`SCOPES` is still `drive.readonly`, unchanged since Step 3), so
`readOnlyHint=True` is the load-bearing hint and the other three are set
for a reader who wants them anyway: `destructiveHint=False` (no updates
at all, let alone destructive ones), `idempotentHint=True` (repeated
calls with the same arguments cause no additional effect on the
*environment* — the pitfall this build task calls out is annotating
optimistically, and this is deliberately about environment side
effects, not output stability: `search_drive_files`' answer can differ
call to call because LLM ranking isn't perfectly deterministic and an
ambiguous query needs a fresh human decision each time, but neither of
those is a side effect the hint is claiming doesn't exist), and
`openWorldHint=True` (both talk to a real external system — Google
Drive, plus whatever LLM the connected client's sampling handler uses).

**The pattern for a future write tool**, documented now since none
exists yet (`_guard_write`'s docstring in `server.py`, quoted here):

```python
types.Tool(
    name="send_email",
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,   # an unsent state can't be recovered by calling it again
        idempotentHint=False,   # calling it twice sends two emails, not one
        openWorldHint=True,
    ),
)
```

Per spec, a client is expected to require explicit human confirmation
before calling any tool with `destructiveHint=True` — the same
confirmation discipline this project already uses for sampling approval
(Step 6) and elicitation (Step 7).

**Verified on the wire**, not just in source — `jsonrpc.log` line 7
(`tools/list` response, real run against `--no-auth --log-level DEBUG`
on port 8781):

```json
{"name":"search_drive_files", ..., "annotations":{"title":"Search Drive files (sampling-ranked)","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}}
{"name":"download_drive_file", ..., "annotations":{"title":"Download a Drive file","readOnlyHint":true,"destructiveHint":false,"idempotentHint":true,"openWorldHint":true}}
```

And from `client.py`'s own printed output, reading only what
`tools/list` returned (no `server.py` open anywhere in this terminal):

```
=== tools/list: annotations alone, no source file read ===
  - search_drive_files
      readOnlyHint=True destructiveHint=False idempotentHint=True openWorldHint=True
      => safe to call automatically (read-only)
  - download_drive_file
      readOnlyHint=True destructiveHint=False idempotentHint=True openWorldHint=True
      => safe to call automatically (read-only)
```

## 2. Structured output schemas

Three Pydantic models in `server.py`: `DriveFileMatch` (one match),
`SearchDriveFilesResult` (`search_drive_files`' result — see below for
why this is one shape now, not two), and `DownloadDriveFileResult`
(`download_drive_file`'s result, `size_bytes` constrained `ge=0`).

**One shape, not two.** Step 7's `search_drive_files` returned a bare
`list` on a normal match and a `dict` with `"ambiguous": true` when the
human didn't resolve a conflict — a client validating against a single
declared `outputSchema` needs one shape to check against, not two to
guess between. `SearchDriveFilesResult` now always returns one object,
discriminated by `status`:

```python
class SearchDriveFilesResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["matched", "ambiguous"]
    matches: list[DriveFileMatch] = Field(default_factory=list)
    message: str | None = None
```

Each `Tool` publishes its model's `model_json_schema()` as
`outputSchema` (`jsonrpc.log` line 7, quoted in full there — includes
`$defs`/`$ref` for the nested `DriveFileMatch`, and
`"additionalProperties": false` from `ConfigDict(extra="forbid")`, so an
extra/typo'd field is rejected too, not just a missing one).

**Validated before returning, twice, independently** — not assumed:

1. `_build_matched_result`/`_build_ambiguous_result`/
   `_build_download_result` construct the Pydantic model first;
   `pydantic.ValidationError` on bad data happens *before* any dict is
   ever built, let alone returned.
2. The MCP SDK's own low-level `Server.call_tool` wrapper independently
   runs `jsonschema.validate` against the declared `outputSchema` on
   whatever a tool handler returns — a second, protocol-level check
   that would catch the same malformed payload even if the Pydantic
   layer were somehow bypassed.

**Proven with a deliberately malformed value**, not assumed — real run,
`test_output_schemas.py`:

```
OK: a well-formed search result validates and round-trips through the schema
OK: a search result missing required fields is rejected by Pydantic before it's ever returned
OK: the same malformed search result is independently rejected by the declared outputSchema
OK: a wrong-typed 'matches' field is rejected by Pydantic
OK: a well-formed download result validates and round-trips through the schema
OK: a negative size_bytes is rejected by Pydantic before it's ever returned
OK: the same negative size_bytes is independently rejected by the declared outputSchema
```

**On the wire**, a real `download_drive_file` result carries both the
legacy text content and the new structured content side by side
(`jsonrpc.log` line 45):

```json
{"content":[{"type":"text","text":"{\n  \"file_id\": \"...\",\n  \"name\": \"UCLA&MIT.pdf\",\n  \"mimeType\": \"application/pdf\",\n  \"size_bytes\": 1903362\n}"}],
 "structuredContent":{"file_id":"1fi_TlrWlCISvErrGVEFTN-ZhqeN_32UQ","name":"UCLA&MIT.pdf","mimeType":"application/pdf","size_bytes":1903362},
 "isError":false}
```

## 3. Real logging, not print-debugging

Steps 5-7 hardcoded `logging.basicConfig(level=logging.DEBUG, ...)` —
every raw JSON-RPC frame always printed to stderr, whether anyone wanted
it or not (client.py hardcoded `INFO`). This step:

- Defaults to `INFO`, configurable via `LOG_LEVEL` env var or
  `--log-level {DEBUG,INFO,WARNING,ERROR}` (server.py; client.py takes
  `LOG_LEVEL` the same way). The CLI flag overrides the env var, applied
  via `logging.getLogger().setLevel(args.log_level)` in `main()` after
  argparse runs, not baked into the module-level `basicConfig` call.
- Real levels: DEBUG for the raw `/mcp` wire frames (`_log_wire`'s
  stderr echo — always written to `jsonrpc.log` in full regardless of
  `--log-level`; only the human-readable console echo is gated) and the
  per-chunk `_fetch_drive_file_content` detail; INFO for tool-call-level
  events (`download_drive_file: starting/complete`,
  `search_drive_files: raw order/final order`, auth/startup milestones);
  WARNING for elicitation declines/cancels and sampling
  denials/failures (see `_rank_candidates_by_relevance`'s `except
  McpError` and `_disambiguate_with_human`'s `if result.action !=
  "accept"`); ERROR for genuinely unexpected `tools/call` failures
  (`handle_call_tool`'s `except Exception`, with `exc_info=True`) and
  for a blocked `--dry-run` write attempt.

**Verified: INFO is quiet, DEBUG shows the frames — same server, same
call, two runs.** Full default-`INFO` run (`download_drive_file` on the
1.9MB PDF, full download + a cancelled one) — the *entire* server log:

```
2026-07-28 14:28:11,422 [server] INFO log level set to INFO
2026-07-28 14:28:11,467 [server] INFO authenticated with Drive; starting Streamable HTTP server on 127.0.0.1:8781 (version=c5421ab75806968e576f35b12320666d80b82832-dirty)
2026-07-28 14:28:11,467 [server] WARNING OAuth DISABLED (--no-auth) -- transport-only mode for isolating transport bugs from auth bugs. Not how this server ships; see README.
2026-07-28 14:28:11,572 [server] INFO StreamableHTTP session manager started
2026-07-28 14:28:31,495 [server] INFO Created new transport with session ID: 996d8ef5e0cb4d4a9e95ff97c5d552d2
2026-07-28 14:28:31,505 [server] INFO Processing request of type ListToolsRequest
2026-07-28 14:28:31,509 [server] INFO Processing request of type ListResourcesRequest
2026-07-28 14:28:32,013 [server] INFO Processing request of type CallToolRequest
2026-07-28 14:28:32,017 [server] INFO download_drive_file: starting file_id=1fi_TlrWlCISvErrGVEFTN-ZhqeN_32UQ
2026-07-28 14:28:54,600 [server] INFO download_drive_file: complete, 'UCLA&MIT.pdf', 1903362 bytes total
2026-07-28 14:28:54,617 [server] INFO Processing request of type CallToolRequest
2026-07-28 14:28:54,619 [server] INFO download_drive_file: starting file_id=1fi_TlrWlCISvErrGVEFTN-ZhqeN_32UQ
2026-07-28 14:28:56,327 [server] INFO Request 4 cancelled - duplicate response suppressed
2026-07-28 14:28:56,332 [server] INFO Terminating session: 996d8ef5e0cb4d4a9e95ff97c5d552d2
2026-07-28 14:29:25,604 [server] INFO StreamableHTTP session manager shutting down
```

No chunk-level detail, no wire frames — 16 lines start to finish for
one full download and one cancelled one. The identical scenario re-run
with `--log-level DEBUG` shows, among 169 lines, the raw frames and
per-chunk detail on demand:

```
2026-07-28 14:29:53,839 [server] DEBUG URL being requested: GET https://www.googleapis.com/drive/v3/files/.../fields=name%2C+mimeType&alt=json
2026-07-28 14:29:53,839 [server] DEBUG URL being requested: GET https://www.googleapis.com/drive/v3/files/...?alt=media
2026-07-28 14:29:54,107 [server] DEBUG _fetch_drive_file_content: requesting chunk 1 of 'UCLA&MIT.pdf'
2026-07-28 14:29:54,601 [server] DEBUG _fetch_drive_file_content: chunk 1 received (131072 / 1903362 bytes, 7%)
2026-07-28 14:29:54,602 [server] DEBUG <<< /mcp data: {"method":"notifications/progress","params":{"progressToken":4,"progress":131072.0,"total":1903362.0},"jsonrpc":"2.0"}
2026-07-28 14:29:54,953 [server] DEBUG _fetch_drive_file_content: requesting chunk 2 of 'UCLA&MIT.pdf'
2026-07-28 14:29:55,505 [server] DEBUG _fetch_drive_file_content: chunk 2 received (262144 / 1903362 bytes, 14%)
2026-07-28 14:29:55,506 [server] DEBUG <<< /mcp data: {"method":"notifications/progress","params":{"progressToken":4,"progress":262144.0,"total":1903362.0},"jsonrpc":"2.0"}
2026-07-28 14:29:55,511 [server] DEBUG >>> POST /mcp {"method":"notifications/cancelled","params":{"requestId":4,"reason":"step8 acceptance test"},"jsonrpc":"2.0"}
2026-07-28 14:29:55,512 [server] INFO Request 4 cancelled - duplicate response suppressed
```

Same claim Step 5 first established — no `requesting chunk 3` line
after the cancellation — still holds, and is now something `--log-level
DEBUG` makes visible on demand rather than something that's always in
the way. `jsonrpc.log` lines 46-53 (the wire-level record, always
written in full regardless of `--log-level`) show the same thing: only
two `notifications/progress` for `progressToken=4` before
`notifications/cancelled`.

## 4. `--dry-run` mode

`_guard_write(action)` in `server.py`: raises `DryRunBlocked` (logged at
ERROR) when the module-level `DRY_RUN` flag is set, otherwise logs INFO
and lets the caller proceed. No tool calls it yet — `SCOPES` is still
`drive.readonly`, and there is no write-capable tool in this server —
which is exactly the pitfall this build task calls out: a `--dry-run`
flag with nothing real to block is a no-op dressed up as a safety
feature. `_synthetic_write_probe` exists solely so this can be tested
against *something*, standing in for whatever Step 9's Dispatcher
eventually calls (a real Gmail send):

```python
async def _synthetic_write_probe() -> str:
    _guard_write("synthetic: send an email / write a Drive file")
    return "synthetic write executed (should never happen while --dry-run is active)"
```

**Tested by attempting one, both directions** — `test_dry_run_guard.py`,
real run:

```
2026-07-28 14:24:39,031 [server] ERROR dry-run: blocked a write-capable action: synthetic: send an email / write a Drive file
2026-07-28 14:24:39,031 [server] INFO write-capable action proceeding (dry-run not active): synthetic: send an email / write a Drive file
OK: --dry-run (DRY_RUN=True) blocked the synthetic write path via DryRunBlocked, as required
OK: normal mode (DRY_RUN=False) let the same synthetic write path proceed, as expected
```

Both branches genuinely execute: `DRY_RUN=True` raises before the
"executed" string is ever returned (the ERROR log line and the raised
exception happen together, in that order); `DRY_RUN=False` returns
normally and logs INFO instead. Not just documented as blocking —
actually attempted, both ways, in-process.

**The CLI flag itself wired through**, not just the underlying function
— real startup log, `server.py --no-auth --dry-run`:

```
2026-07-28 14:34:52,641 [server] INFO log level set to INFO
2026-07-28 14:34:52,642 [server] WARNING --dry-run is active: any write-capable code path will be refused, not executed
```

## 5. Versioning

`_resolve_git_sha()` runs `git rev-parse HEAD` against the repo root at
*startup* (not a value written once into a constant), appending
`-dirty` if `git status --porcelain` is non-empty. The result is passed
as `Server("inbox-guardian-drive-http", version=_GIT_SHA)` — the
low-level SDK's own `create_initialization_options` puts a non-empty
`self.version` directly into `serverInfo.version`, no extra plumbing
needed.

**Verified against `git rev-parse HEAD` directly, not hardcoded**:

```
$ git rev-parse HEAD
c5421ab75806968e576f35b12320666d80b82832
```

```json
"serverInfo":{"name":"inbox-guardian-drive-http","version":"c5421ab75806968e576f35b12320666d80b82832-dirty"}
```

(`jsonrpc.log` line 3, real `initialize` response.) The `-dirty` suffix
is correct for the moment this was captured: this step's own new files
(`step8-production/`) were untracked at verification time, so
`git status --porcelain` was non-empty. Once this commit lands, the
working tree returns to clean and the next server start reports the new
HEAD SHA with no suffix — the point of computing this at startup rather
than baking it in once is exactly that it tracks reality across both
states, not just the one it happened to be written during.

## Running it

Drive credentials (`~/.drive-mcp/token.json`, from Step 3) must exist;
`CEREBRAS_API_KEY` is only needed if you exercise `search_drive_files`
(sampling/elicitation are unchanged from Steps 6-7 and not re-verified
here — see those steps' READMEs for that evidence).

```
../.venv/bin/python server.py                              # OAuth required (default), INFO logging
../.venv/bin/python server.py --no-auth --log-level DEBUG   # transport-only + raw wire frames on stderr
../.venv/bin/python server.py --dry-run                     # any write-capable path refuses instead of executing
../.venv/bin/python client.py                                # full demo: annotations, version, sampling, elicitation, progress, cancellation
../.venv/bin/python test_output_schemas.py                   # malformed output rejected at the schema boundary, two ways
../.venv/bin/python test_dry_run_guard.py                    # --dry-run genuinely blocks a synthetic write, both directions
```

`LOG_LEVEL=DEBUG` (env var) is equivalent to `--log-level DEBUG` for
either script; the flag overrides the env var if both are set.

## MCP Inspector — acceptance test

Connect fresh, open `tools/list`, and read only the `annotations` block
for each tool — without opening `server.py` at all. Both tools show
`readOnlyHint: true, destructiveHint: false` and each declares an
`outputSchema` with a validator you can check a live result against
without reading a line of this project's code. That's the actual
acceptance bar this build task sets, and it's exactly what the
`client.py` output quoted under "Tool annotations" above demonstrates
end to end (real run, not a mockup of what Inspector would show).

## Review checklist

- [x] Every tool's annotations checked against source, not assumed —
      see "Tool annotations" above for the `readOnlyHint`/
      `idempotentHint` reasoning tied to actual behavior (no write
      capability exists), verified again on the real wire (`jsonrpc.log`
      line 7) and in `client.py`'s own annotation-only printout.
- [x] Structured output schema exists for every tool
      (`SEARCH_DRIVE_FILES_OUTPUT_SCHEMA`, `DOWNLOAD_DRIVE_FILE_OUTPUT_SCHEMA`,
      both published as `outputSchema`), and a deliberately malformed
      return value fails at the boundary, not downstream — proven twice
      (Pydantic and the SDK's own `jsonschema` check against
      `outputSchema`) in `test_output_schemas.py`, not assumed.
- [x] `--dry-run` genuinely blocks a write path, tested by attempting
      one (synthetically, since no real write tool exists yet) and
      confirming it's refused — `test_dry_run_guard.py`, both the
      blocked and allowed direction, plus the CLI flag itself verified
      wired through at startup.
- [x] Log verbosity is configurable (`LOG_LEVEL` env var / `--log-level`
      flag), and DEBUG actually shows raw JSON-RPC frames on demand —
      same scenario run twice, INFO (16 lines, no frames) vs. DEBUG (169
      lines, frames and per-chunk detail included), both quoted above.
- [x] `initialize` response includes a real, current git SHA, verified
      against `git rev-parse HEAD` directly (not hardcoded once) — see
      "Versioning" above, including what happens to the value across a
      dirty vs. clean working tree.

## Pitfalls addressed

- **Annotating tools optimistically ("it's basically read-only") rather
  than precisely** — every hint traces to an actual, checked property
  of the code (no write scope, no accumulating side effect, real
  external system contacted), not a guess; see "Tool annotations"
  above for the idempotentHint reasoning specifically, since that one is
  the easiest to hand-wave.
- **`--dry-run` that's a no-op because there's nothing to block yet** —
  avoided by building `_synthetic_write_probe` specifically so
  `_guard_write` has something real to be tested against now, before
  Step 9's Dispatcher exists to give it an actual job; both the blocked
  and allowed paths are exercised in `test_dry_run_guard.py`, not just
  the blocked one.
- **A malformed tool result silently passed downstream** — avoided by
  validating before returning (Pydantic model construction) and
  publishing the same shape as `outputSchema` for the SDK's own
  independent `jsonschema` check to catch the same bug a second way;
  both layers proven to actually reject bad data in
  `test_output_schemas.py`, not assumed to.
