"""
Remote MCP server wrapper for this repo's own main.py, deployed as a
subdirectory of the same repo it wraps.

Runs the FastMCP tools defined in ../main.py over streamable-http instead of
stdio, and validates every request against a Cloudflare Access JWT before
letting it through to the MCP app.

Deploy with Railway's "Shared Monorepo" pattern (see
https://docs.railway.com/deployments/monorepo): do NOT set a Root Directory
on the service (that would stop the repo root - and therefore main.py and
current_mlb_teams.csv - from being pulled into the build). Instead set:
    Build Command: pip install -r railway-wrapper/requirements.txt
    Start Command: python railway-wrapper/server.py
This way the wrapper always runs against the exact commit that was pushed -
no separate git-clone-of-this-same-repo, no version drift to track.

Env vars expected:
    PORT                    - set automatically by Railway
    CF_ACCESS_TEAM_DOMAIN   - e.g. "yourteam.cloudflareaccess.com"
    CF_ACCESS_AUD           - the Application Audience (AUD) tag from your
                              Cloudflare Access application
    LOG_LEVEL               - "INFO" (default) or "DEBUG". DEBUG also turns
                              on raw wire logging for every outbound HTTP
                              request this process makes.
"""

import logging
import os
import sys

_WRAPPER_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_WRAPPER_DIR)

# main.py, mlb_api.py, and current_mlb_teams.csv all live at the repo root,
# one level up from this wrapper subdirectory. Add the repo root to
# sys.path so `import main` finds them, and chdir there so mlb_api.py's own
# `open("current_mlb_teams.csv")` (a bare relative path) resolves correctly
# regardless of what cwd Railway actually invokes the start command from.
sys.path.insert(0, _REPO_ROOT)
os.chdir(_REPO_ROOT)

import jwt
from jwt import PyJWKClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
for _logger_name in ("fastmcp", "mlbstatsapi", "urllib3", "httpx", "pybaseball"):
    logging.getLogger(_logger_name).setLevel(LOG_LEVEL)

if LOG_LEVEL == "DEBUG":
    # http.client is what urllib3 (and therefore requests) sits on top of,
    # so this catches every outbound request/response line - method, URL,
    # headers, and status - regardless of which HTTP client is used
    # internally for a given data source.
    import http.client

    http.client.HTTPConnection.debuglevel = 1

log = logging.getLogger("mlb_wrapper")

# Importing main executes it top to bottom, which calls setup_mlb_tools(mcp)
# and setup_generic_tools(mcp) unconditionally at module scope (not gated
# behind `if __name__ == "__main__":`), building a fully-populated `mcp`
# FastMCP instance. This import must happen before mcp.http_app() below or
# you'll get a server with zero tools.
from main import mcp

CF_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN")
CF_AUD = os.environ.get("CF_ACCESS_AUD")

if not CF_TEAM_DOMAIN or not CF_AUD:
    log.error(
        "Missing CF_ACCESS_TEAM_DOMAIN or CF_ACCESS_AUD env vars. "
        "Requests will not be verifiable, refusing to start."
    )
    sys.exit(1)

CERTS_URL = f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs"

# PyJWKClient handles fetching and caching Cloudflare's signing keys for us,
# including rotating to a new key if Cloudflare rotates theirs. A timeout
# keeps a slow/unreachable certs endpoint from hanging every request.
jwk_client = PyJWKClient(CERTS_URL, timeout=10)


class CloudflareAccessMiddleware:
    """
    Raw ASGI middleware that rejects any request without a valid
    Cf-Access-Jwt-Assertion header before it reaches the MCP app.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            # Let non-HTTP scopes (e.g. lifespan) pass through untouched.
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        token = headers.get(b"cf-access-jwt-assertion")

        if not token:
            await self._reject(send, "Missing Cloudflare Access token")
            return

        token = token.decode("utf-8")

        try:
            signing_key = jwk_client.get_signing_key_from_jwt(token)
            jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=CF_AUD,
                issuer=f"https://{CF_TEAM_DOMAIN}",
            )
        except jwt.PyJWTError as exc:
            # Log the real reason server-side only; the client gets a generic
            # message so JWT internals (audience, issuer, key IDs) don't leak
            # to unauthenticated probes.
            log.warning("Rejected Cloudflare Access token: %s", exc)
            await self._reject(send, "Invalid Cloudflare Access token")
            return

        # Token checks out, let the request through to the MCP app.
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, message: str):
        response = JSONResponse({"error": message}, status_code=401)
        await response(
            {"type": "http", "headers": []},  # scope is unused by Response.__call__
            None,
            send,
        )


def build_app() -> Starlette:
    # mlb-api-mcp uses the third-party `fastmcp` package (not the official
    # MCP SDK's mcp.server.fastmcp), which builds its ASGI app via
    # http_app() rather than streamable_http_app().
    inner_app = mcp.http_app()
    return CloudflareAccessMiddleware(inner_app)


app = build_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
