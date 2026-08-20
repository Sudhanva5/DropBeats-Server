"""CORS that covers the music endpoints and deliberately skips the rest.

ALLOWED_ORIGINS defaults to "*" with allow_credentials=True, which was
harmless while every response this service produced was public music
metadata. /license/validate now returns a customer's name, email and country,
so under a wildcard policy any web page -- a phishing page prompting for a
licence key, say -- could call it cross-origin and *read* the reply.

The native macOS client is not a browser: it sends no Origin and CORS does
nothing for it. So the licensing and webhook routes opt out entirely. Without
the response headers a browser refuses to hand the body to script, and the
preflight for anything non-simple falls through to the router and is refused.
Gumroad posts server-side and is likewise unaffected.
"""

from starlette.middleware.cors import CORSMiddleware

# Prefix match, so this also covers any future route under these roots.
EXEMPT_PREFIXES = ("/license/", "/webhooks/")


class ScopedCORSMiddleware(CORSMiddleware):
    """CORSMiddleware that passes EXEMPT_PREFIXES straight through."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(
            EXEMPT_PREFIXES
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
