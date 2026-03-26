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

# Task assignment (operator -> server -> agent)
# Tells an agent to start running a named role autonomously
TASK_ASSIGN = "task_assign"   # operator -> server
TASK        = "task"          # server -> agent
TASK_ACK    = "task_ack"      # agent -> server (confirmation)
TASK_STOP   = "task_stop"     # server -> agent (halt current task)
TASKS       = "tasks"         # operator requests task list from server

# Broadcast (operator -> server -> group of agents)
BROADCAST    = "broadcast"    # operator sends; server fans out
BROADCAST_OK = "broadcast_ok" # server confirms how many agents received it

# Streaming data (agent -> server -> data store)
# Agents push structured findings continuously; operators query the store
DATA       = "data"           # agent -> server (push records)
DATA_QUERY = "data_query"     # operator -> server (query store)
DATA_RESP  = "data_resp"      # server -> operator (query results)

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
# Data categories (used in DATA / DATA_QUERY messages)
# ---------------------------------------------------------------------------
CAT_NETWORKS    = "networks"     # SSIDs / BSSIDs seen by Kismet or iw scan
CAT_HANDSHAKES  = "handshakes"   # WPA2 EAPOL or WPA3 SAE handshakes captured
CAT_SAE_TIMING  = "sae_timing"   # Dragonfly SAE commit/confirm timing measurements
CAT_PORTALS     = "portals"      # Evil portal credential captures
CAT_BLUETOOTH   = "bluetooth"    # Bluetooth devices seen
CAT_RF          = "rf"           # RF signals (rtl_433)
CAT_CLIENTS     = "clients"      # WiFi client stations
CAT_PRESENCE    = "presence"     # ESPectre CSI-based motion/presence detection

ALL_CATEGORIES = [
    CAT_NETWORKS, CAT_HANDSHAKES, CAT_SAE_TIMING,
    CAT_PORTALS, CAT_BLUETOOTH, CAT_RF, CAT_CLIENTS, CAT_PRESENCE,
]

# ---------------------------------------------------------------------------
# Task roles
# ---------------------------------------------------------------------------
ROLE_KISMET     = "kismet"
ROLE_BETTERCAP  = "bettercap"
ROLE_EVIL_PORTAL= "evil_portal"
ROLE_DRAGONFLY  = "dragonfly"
ROLE_ESPECTRE   = "espectre"    # CSI-based WiFi motion/presence detection
ROLE_SCAN       = "scan"
ROLE_IDLE       = "idle"

ALL_ROLES = [ROLE_KISMET, ROLE_BETTERCAP, ROLE_EVIL_PORTAL,
             ROLE_DRAGONFLY, ROLE_ESPECTRE, ROLE_SCAN, ROLE_IDLE]

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
