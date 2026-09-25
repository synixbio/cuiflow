"""Entry points: CLI, batch runner, REST service, MCP server. All are thin wrappers over
:class:`cuiflow.api.Extractor`."""

import hmac

#: The bearer token the REST service and the MCP HTTP transport require when it is set.
TOKEN_ENV = "CUIFLOW_API_TOKEN"
#: Hosts ``serve`` and ``mcp`` bind without a token; any other host needs ``TOKEN_ENV`` set.
LOOPBACK = ("127.0.0.1", "localhost", "::1")
#: The longest document the REST service and the MCP server accept, in characters.
MAX_CHARS = 200_000
#: The longest ``doc_id`` the REST service accepts, in characters.
MAX_DOC_ID_CHARS = 1_000
#: The deepest ancestor walk a concept lookup takes (1 = parents).
MAX_ANCESTOR_DEPTH = 10


def bearer_matches(authorization: str | None, token: str) -> bool:
    """Whether an ``Authorization`` header is ``Bearer <token>``. The scheme is required (in any
    case, as RFC 7235 allows); the comparison takes constant time."""
    scheme, _, supplied = (authorization or "").strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(supplied.strip().encode(), token.encode())
