"""Authentication and per-organisation authorization.

`scope.py` is the shape of an authenticated request, `dependencies.py` is the
single resolver every route declares against, `sessions.py` is the cookie and
the rows behind it, `service.py` is signing in and account administration, and
`ratelimit.py` is the per-address throttle on the sign-in endpoint.
"""

from app.auth.scope import AccessDenied, AuthError, NotAuthenticated, Scope

__all__ = ["AccessDenied", "AuthError", "NotAuthenticated", "Scope"]
