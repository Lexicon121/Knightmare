# c2/operator.py — Knightmare C2 Operator Console
#
# Interactive multi-operator CLI.  Connects to the C2 server operator port,
# lists / interacts with agent sessions, and forwards commands.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from c2 import protocol as proto

BANNER = r"""
██╗  ██╗███╗   ██╗██╗ ██████╗ ██╗  ██╗████████╗███╗   ███╗ █████╗ ██████╗ ███████╗
██║ ██╔╝████╗  ██║██║██╔════╝ ██║  ██║╚══██╔══╝████╗ ████║██╔══██╗██╔══██╗██╔════╝
█████╔╝ ██╔██╗ ██║██║██║  ███╗███████║   ██║   ██╔████╔██║███████║██████╔╝█████╗
██╔═██╗ ██║╚██╗██║██║██║   ██║██╔══██║   ██║   ██║╚██╔╝██║██╔══██║██╔══██╗██╔══╝
██║  ██╗██║ ╚████║██║╚██████╔╝██║  ██║   ██║   ██║ ╚═╝ ██║██║  ██║██║  ██║███████╗
╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝

  Operator Console — Tengu Edition
  Type 'help' for commands.
"""


# ---------------------------------------------------------------------------
# Low-level client
# ---------------------------------------------------------------------------

class OperatorClient:
    """Synchronous TLS client.  Recv loop runs in a background thread."""

    def __init__(self, host: str, port: int, password: str,
                 name: str, cert: str | None = None):
        self.host     = host
        self.port     = port
        self.password = password
        self.name     = name
        self.cert     = cert
        self._sock    = None
        self._file    = None
        self._lock    = threading.Lock()  # serialize writes

        # Messages received asynchronously (SESSION_NEW, SESSION_GONE, OPERATORS)
        self._async_msgs: list[dict] = []
        self._async_lock = threading.Lock()

        # Synchronous response slot — filled by recv thread for expected replies
        self._pending: dict | None = None
        self._pending_event        = threading.Event()

        # Output accumulator for commands (filled until DONE arrives)
        self._cmd_output: list[str] = []
        self._cmd_done               = threading.Event()

        self._running = False

    # ------------------------------------------------------------------

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
        ctx        = self._ssl_context()
        self._sock = ctx.wrap_socket(raw, server_hostname=self.host)
        self._file = self._sock.makefile("rb")

    def _write(self, msg_type: str, **data):
        with self._lock:
            self._sock.sendall(proto.encode(msg_type, **data))

    def handshake(self):
        self._write(proto.AUTH, password=self.password, name=self.name)
        line = self._file.readline()
        resp = proto.decode(line)
        if resp.get("type") != proto.AUTH_OK:
            raise ConnectionRefusedError(resp.get("reason", "Auth failed"))

    # ------------------------------------------------------------------
    # Background recv thread
    # ------------------------------------------------------------------

    def _recv_loop(self):
        """Runs in a daemon thread.  Routes incoming messages appropriately."""
        while self._running:
            try:
                line = self._file.readline()
                if not line:
                    break
                msg   = proto.decode(line)
                mtype = msg.get("type")

                # --- streaming command output ---------------------------
                if mtype == proto.OUTPUT:
                    self._cmd_output.append(msg.get("data", ""))

                elif mtype == proto.DONE:
                    self._cmd_done.set()

                # --- synchronous replies --------------------------------
                elif mtype in (proto.SESSIONS, proto.OPERATORS,
                               proto.INTERACT_OK, proto.INTERACT_FAIL,
                               proto.AUTH_OK, proto.AUTH_FAIL,
                               proto.ERROR, proto.PONG):
                    self._pending = msg
                    self._pending_event.set()

                # --- server-push events (print immediately) -------------
                elif mtype in (proto.SESSION_NEW, proto.SESSION_GONE):
                    with self._async_lock:
                        self._async_msgs.append(msg)

            except Exception:
                break
        self._running = False

    def start_recv(self):
        self._running = True
        t = threading.Thread(target=self._recv_loop, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Request / response helpers
    # ------------------------------------------------------------------

    def _request(self, msg_type: str, timeout: float = 5.0, **data) -> dict:
        self._pending       = None
        self._pending_event.clear()
        self._write(msg_type, **data)
        if not self._pending_event.wait(timeout):
            return {"type": "error", "reason": "Timed out waiting for server response"}
        return self._pending

    def _run_command(self, cmd_str: str, timeout: float = 30.0) -> str:
        self._cmd_output.clear()
        self._cmd_done.clear()
        self._write(proto.COMMAND, cmd=cmd_str, args={})
        self._cmd_done.wait(timeout)
        return "".join(self._cmd_output)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_sessions(self) -> list:
        resp = self._request(proto.SESSIONS)
        return resp.get("data", [])

    def get_operators(self) -> list:
        resp = self._request(proto.OPERATORS)
        return resp.get("data", [])

    def interact(self, session_id: str) -> dict:
        return self._request(proto.INTERACT, session_id=session_id)

    def release(self):
        self._write(proto.RELEASE)

    def disconnect(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass

    def drain_async(self) -> list[dict]:
        with self._async_lock:
            msgs, self._async_msgs = self._async_msgs, []
        return msgs


# ---------------------------------------------------------------------------
# Operator CLI
# ---------------------------------------------------------------------------

class OperatorCLI(cmd.Cmd):
    intro  = BANNER
    prompt = "knightmare> "

    def __init__(self, client: OperatorClient):
        super().__init__()
        self.client       = client
        self._session     = None   # currently interacted session dict

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _print_async(self):
        """Flush any server-push events before showing a prompt."""
        for msg in self.client.drain_async():
            mtype = msg.get("type")
            if mtype == proto.SESSION_NEW:
                s = msg.get("session", {})
                print(f"\n[+] New session {s.get('id')} — "
                      f"{s.get('platform')}@{s.get('hostname')} ({s.get('user')})")
            elif mtype == proto.SESSION_GONE:
                sid = msg.get("session_id")
                print(f"\n[-] Session {sid} disconnected")
                if self._session and self._session.get("id") == sid:
                    self._session = None
                    self.prompt   = "knightmare> "
                    print("[!] Your active session was lost.")

    def precmd(self, line):
        self._print_async()
        return line

    # ------------------------------------------------------------------
    # Core commands
    # ------------------------------------------------------------------

    def do_sessions(self, _):
        """List all active agent sessions."""
        data = self.client.get_sessions()
        if not data:
            print("[*] No active sessions.")
            return
        hdr = f"{'ID':<10} {'PLATFORM':<14} {'HOSTNAME':<22} {'USER':<14} {'CONNECTED':<22} LOCKED BY"
        print(hdr)
        print("─" * len(hdr))
        for s in data:
            print(f"{s['id']:<10} {s['platform']:<14} {s['hostname']:<22} "
                  f"{s['user']:<14} {s['connected_at']:<22} {s.get('locked_by') or '—'}")

    def do_operators(self, _):
        """List connected operators."""
        data = self.client.get_operators()
        if not data:
            print("[*] No operators connected.")
            return
        hdr = f"{'ID':<10} {'NAME':<22} {'CONNECTED':<22} SESSION"
        print(hdr)
        print("─" * len(hdr))
        for op in data:
            print(f"{op['id']:<10} {op['name']:<22} {op['connected_at']:<22} "
                  f"{op.get('session_id') or '—'}")

    def do_interact(self, line):
        """interact <session_id>  — lock and interact with a session."""
        sid = line.strip()
        if not sid:
            print("Usage: interact <session_id>")
            return
        resp = self.client.interact(sid)
        if resp.get("type") == proto.INTERACT_OK:
            s = resp["session"]
            self._session = s
            self.prompt   = f"[{s['platform']}:{s['hostname']}]> "
            print(f"[*] Interacting with session {s['id']} "
                  f"({s['platform']}@{s['hostname']})")
            print("[*] Type commands to send. 'background' to return to main prompt.")
        else:
            print(f"[!] {resp.get('reason', 'Failed to interact.')}")

    def do_background(self, _):
        """Background current session (keep lock, return to main prompt)."""
        if not self._session:
            print("[!] No active session.")
            return
        print(f"[*] Session {self._session['id']} backgrounded.")
        self._session = None
        self.prompt   = "knightmare> "

    def do_release(self, _):
        """Release and unlock the current session."""
        if not self._session:
            print("[!] No active session.")
            return
        self.client.release()
        print(f"[*] Released session {self._session['id']}.")
        self._session = None
        self.prompt   = "knightmare> "

    def do_exit(self, _):
        """Exit the operator console."""
        self.client.disconnect()
        print("[*] Disconnected.")
        return True

    def do_EOF(self, line):
        print()
        return self.do_exit(line)

    # ------------------------------------------------------------------
    # Forward unknown commands to active session
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
    # Help
    # ------------------------------------------------------------------

    def do_help(self, arg):
        if arg:
            super().do_help(arg)
            return
        print("""
Main console commands:
  sessions               — list active agent sessions
  operators              — list connected operators
  interact <id>          — attach to a session (locks it)
  background             — return to main prompt (session stays locked)
  release                — unlock current session
  exit                   — disconnect and exit

When interacting with a Knightmare session:
  list                   — list available exploit modules
  use <module>           — load a module (e.g. esp32/deauth_attack)
  info                   — show loaded module info
  show options           — show module options
  show payloads          — show available payloads
  set <option> <value>   — set a module option
  connect <device>       — connect to serial device
  devices                — list detected serial devices
  run <payload>          — execute payload
  icarus <I|C|A|R|U|S>   — ICARUS pillar reference

When interacting with a TMS session:
  drive forward|back|left|right|stop
  marauder <command>     — send a Marauder command
  scan network|wifi|bluetooth|rf
  portscan <target> [flags]
  ping <host>
  dns <host>
  status                 — system status (CPU/RAM/disk/GPS)
""")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Knightmare C2 Operator Console")
    parser.add_argument("--host",     default="127.0.0.1",                 help="C2 server IP/hostname")
    parser.add_argument("--port",     type=int, default=proto.OPERATOR_PORT, metavar="PORT")
    parser.add_argument("--password", required=True,                        help="Shared C2 password")
    parser.add_argument("--name",     default="operator",                   help="Your operator name")
    parser.add_argument("--cert",     default=None,                         help="Path to server.crt")
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
