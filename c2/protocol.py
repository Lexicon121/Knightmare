# c2/protocol.py — Shared message protocol for Knightmare C2

import json
import hashlib

# ---------------------------------------------------------------------------
# Default ports
# ---------------------------------------------------------------------------
AGENT_PORT    = 31337
OPERATOR_PORT = 31338

# ---------------------------------------------------------------------------
# Message type constants
# ---------------------------------------------------------------------------

# Handshake
AUTH        = "auth"
AUTH_OK     = "auth_ok"
AUTH_FAIL   = "auth_fail"

# Agent lifecycle
REGISTER    = "register"
REGISTER_OK = "register_ok"

# Operator queries
SESSIONS    = "sessions"
OPERATORS   = "operators"

# Session control
INTERACT      = "interact"
INTERACT_OK   = "interact_ok"
INTERACT_FAIL = "interact_fail"
RELEASE       = "release"
BACKGROUND    = "background"

# Command / output
COMMAND = "command"
OUTPUT  = "output"
DONE    = "done"

# Server-push events
SESSION_NEW  = "session_new"
SESSION_GONE = "session_gone"

# Misc
ERROR = "error"
PING  = "ping"
PONG  = "pong"

# ---------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------
PLATFORM_KNIGHTMARE = "knightmare"
PLATFORM_TMS        = "tms"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def encode(msg_type: str, **data) -> bytes:
    """Serialize a message to a newline-terminated JSON bytes object."""
    return (json.dumps({"type": msg_type, **data}) + "\n").encode()


def decode(line: bytes) -> dict:
    """Deserialize a newline-terminated JSON bytes object to a dict."""
    return json.loads(line.decode().strip())
