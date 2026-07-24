"""Regression test: resources/read and the download_drive_file tool must
share one Drive-fetching implementation, not two copies that happen to
agree.

This is the exact bug reported against this step: download_drive_file
called files().get_media() unconditionally, so it worked for regular
files but raised a 403 ("Only files with binary content can be
downloaded. Use Export with Docs Editors files.") on a Google-native
Doc/Sheet/Slide -- a branch handle_read_resource already handled
correctly. Two independent copies of "how to fetch a Drive file's
content" had already drifted once (one had the mimeType branch, the
other didn't); comparing their outputs on a single file type wouldn't
have caught that, the same way Step 2's shared-lookup incident showed a
same-answer test can pass while the underlying logic has quietly forked.

This test proves a shared code path, not just shared output: it patches
server._fetch_drive_file_content with a spy and asserts BOTH
handle_read_resource and _download_drive_file call it, for both a
regular file (a PDF, exercising the get_media/MediaIoBaseDownload
branch) and a Google-native file (a Doc, exercising the export branch).
If either caller is ever changed to reimplement the mimeType branching
itself instead of going through the shared helper, the spy's count for
that file/caller pair drops and this test fails.

Runs against the real, authenticated Drive account (same token.json
Step 3's drive-server uses) -- this project's established preference for
real API calls over mocks, same reasoning as
step3-real-servers/drive-server/README.md's "Verified" section.
"""

import asyncio
from unittest.mock import patch

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext

import server


def _authenticate_drive_service() -> None:
    creds = Credentials.from_authorized_user_file(str(server.TOKEN_PATH), server.SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())
    server.drive_service = build("drive", "v3", credentials=creds)


def _find_file_id(drive_query: str) -> str:
    response = server.drive_service.files().list(q=drive_query, pageSize=1, fields="files(id, name)").execute()
    files = response.get("files", [])
    assert files, f"no Drive file found matching {drive_query!r} -- needed to exercise this test"
    return files[0]["id"]


async def _call_download_tool(file_id: str) -> None:
    # download_drive_file reads server.request_context (a contextvar the
    # low-level Server populates per-request during a real session) for
    # ctx.meta.progressToken -- outside of a real request, set a minimal
    # stand-in with meta=None, which the handler already treats as "no
    # progress token supplied" (see its guard before send_progress_notification).
    fake_ctx = RequestContext(request_id=1, meta=None, session=None, lifespan_context=None)
    token = request_ctx.set(fake_ctx)
    try:
        await server._download_drive_file({"file_id": file_id})
    finally:
        request_ctx.reset(token)


def test_resources_read_and_download_tool_share_fetch_helper() -> None:
    _authenticate_drive_service()

    pdf_file_id = _find_file_id("mimeType='application/pdf'")
    doc_file_id = _find_file_id("mimeType='application/vnd.google-apps.document'")

    with patch.object(server, "_fetch_drive_file_content", wraps=server._fetch_drive_file_content) as spy:
        asyncio.run(server.handle_read_resource(server.AnyUrl(server._gdrive_uri(pdf_file_id))))
        asyncio.run(_call_download_tool(pdf_file_id))
        asyncio.run(server.handle_read_resource(server.AnyUrl(server._gdrive_uri(doc_file_id))))
        asyncio.run(_call_download_tool(doc_file_id))

        called_file_ids = [call.args[0] for call in spy.call_args_list]

    assert called_file_ids.count(pdf_file_id) >= 2, (
        "expected both resources/read and download_drive_file to call "
        f"_fetch_drive_file_content({pdf_file_id!r}) for the regular-file (PDF) case; "
        f"saw calls: {called_file_ids!r}"
    )
    assert called_file_ids.count(doc_file_id) >= 2, (
        "expected both resources/read and download_drive_file to call "
        f"_fetch_drive_file_content({doc_file_id!r}) for the Google-native (Doc) case; "
        f"saw calls: {called_file_ids!r}. If this fails, one of the two callers has "
        "started reimplementing the mimeType branch directly instead of going "
        "through the shared helper -- the exact bug this test guards against."
    )

    print(
        "OK: resources/read and download_drive_file both routed through "
        "_fetch_drive_file_content, for both a regular file (PDF) and a "
        "Google-native file (Doc) -- shared code path confirmed, not just "
        "coincidentally-equal output."
    )


if __name__ == "__main__":
    test_resources_read_and_download_tool_share_fetch_helper()
