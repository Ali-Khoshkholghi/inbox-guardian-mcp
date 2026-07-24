"""The interactive consent screen for Step 5's OAuth authorization server.

`DriveOAuthProvider.authorize()` (see provider.py) doesn't decide
approval itself -- it stashes the pending `/authorize` request and
returns a redirect to `GET /consent?request_id=...`, which this module
serves. Only after a human actually loads that page and clicks Approve
does `POST /consent` call back into the provider to mint an
authorization code and redirect to the client's `redirect_uri`. This is
the "define another handler on the MCP server return flow to perform
the second redirect" step the SDK's `authorize()` docstring describes --
except there's no third-party IdP on the other end, just this server
acting as its own resource owner.

These two routes are added directly to the Starlette app's route list in
server.py (not through create_auth_routes, which only knows about the
spec-defined /authorize, /token, /register, /revoke endpoints) and are
deliberately outside RequireAuthMiddleware -- a browser hitting this page
has no MCP bearer token yet; that's the entire reason it's here.
"""

import html

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from oauth.provider import DriveOAuthProvider

CONSENT_PAGE_TEMPLATE = """\
<!doctype html>
<html>
<head><title>Authorize Drive MCP access</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto;">
  <h1>Authorize access</h1>
  <p><strong>{client_name}</strong> is requesting access to the Drive MCP
  server (Step 5) with scope: <code>{scope}</code>.</p>
  <p>Redirect target after approval: <code>{redirect_uri}</code></p>
  <form method="post" action="/consent">
    <input type="hidden" name="request_id" value="{request_id}">
    <button type="submit" name="decision" value="approve"
            style="padding: 0.5rem 1rem; margin-right: 0.5rem;">Approve</button>
    <button type="submit" name="decision" value="deny"
            style="padding: 0.5rem 1rem;">Deny</button>
  </form>
</body>
</html>
"""


def create_consent_routes(provider: DriveOAuthProvider, resource_owner_subject: str) -> list[Route]:
    async def get_consent(request: Request) -> HTMLResponse | PlainTextResponse:
        request_id = request.query_params.get("request_id")
        pending = provider.pop_pending(request_id) if request_id else None
        if pending is None:
            return PlainTextResponse("Unknown or expired authorization request.", status_code=400)

        client_name = pending.client.client_name or pending.client.client_id
        scope = " ".join(pending.params.scopes or []) or "(none requested)"
        return HTMLResponse(
            CONSENT_PAGE_TEMPLATE.format(
                client_name=html.escape(client_name),
                scope=html.escape(scope),
                redirect_uri=html.escape(str(pending.params.redirect_uri)),
                request_id=html.escape(request_id),
            )
        )

    async def post_consent(request: Request):
        form = await request.form()
        request_id = form.get("request_id")
        decision = form.get("decision")
        if not isinstance(request_id, str) or not isinstance(decision, str):
            return PlainTextResponse("Malformed consent submission.", status_code=400)

        redirect_uri = provider.complete_authorization(
            request_id,
            approved=(decision == "approve"),
            subject=resource_owner_subject,
        )
        if redirect_uri is None:
            return PlainTextResponse("Unknown or expired authorization request.", status_code=400)

        return RedirectResponse(url=redirect_uri, status_code=302, headers={"Cache-Control": "no-store"})

    return [
        Route("/consent", endpoint=get_consent, methods=["GET"]),
        Route("/consent", endpoint=post_consent, methods=["POST"]),
    ]
