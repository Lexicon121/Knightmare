# c2/agent.py — Knightmare C2 Agent
#
# Connects back to the C2 server (reverse connection) and exposes the local
# Knightmare capabilities (serial modules, payloads) to operators.
# A background thread streams module discovery data to the data store.
#
# Usage:
#   python -m c2.agent --host <c2-ip> --password <password> [--cert server.crt]

import ssl
import socket
import threading
import os
import sys
import platform
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from c2 import protocol as proto
from core.knightmare_controller import KnightmareController


class KnightmareAgent:
    """Reverse-connect agent that exposes KnightmareController over C2."""

    CAPABILITIES = ["serial", "modules", "payloads", "icarus"]

    # How often to push a module/device inventory to the data store (seconds)
    DATA_PUSH_INTERVAL = 60

    def __init__(self, host: str, port: int, password: str, cert: str | None = None):
        self.host     = host
        self.port     = port
        self.password = password
        self.cert     = cert
        self.ctrl     = KnightmareController()
        self._sock    = None
        self._file    = None
        self._wlock   = threading.Lock()
        self._running = False

    # ------------------------------------------------------------------
    # Connection
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
        raw        = socket.create_connection((self.host, self.port))
        self._sock = self._ssl_context().wrap_socket(raw, server_hostname=self.host)
        self._file = self._sock.makefile("rb")

    def _send(self, msg_type: str, **data):
        with self._wlock:
            self._sock.sendall(proto.encode(msg_type, **data))

    def _recv(self) -> dict:
        line = self._file.readline()
        if not line:
            raise ConnectionResetError("Server closed connection")
        return proto.decode(line)

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    def _handshake(self):
        self._send(proto.AUTH,
                   password=self.password,
                   name=f"knightmare-{platform.node()}")
        resp = self._recv()
        if resp.get("type") != proto.AUTH_OK:
            raise ConnectionRefusedError(resp.get("reason", "Auth failed"))

        self._send(proto.REGISTER,
                   platform     = proto.PLATFORM_KNIGHTMARE,
                   hostname     = platform.node(),
                   user         = os.getenv("USER") or os.getenv("USERNAME") or "unknown",
                   capabilities = self.CAPABILITIES)
        resp = self._recv()
        if resp.get("type") != proto.REGISTER_OK:
            raise RuntimeError("Registration failed")
        print(f"[*] Registered as session {resp['session_id']} on {self.host}:{self.port}")

    # ------------------------------------------------------------------
    # Background data push thread
    # ------------------------------------------------------------------

    def _data_push_loop(self):
        """Periodically push module inventory and device list to C2 data store."""
        while self._running:
            try:
                # Push available modules as network records (reuse networks category
                # for module inventory — operators can see what's loaded on each agent)
                modules = self.ctrl.list_modules()
                if modules:
                    records = [
                        {
                            "ssid":       m.get("name", "?"),
                            "bssid":      m.get("path", "?"),
                            "channel":    m.get("icarus", "?"),
                            "signal":     "N/A",
                            "encryption": m.get("description", "?"),
                        }
                        for m in modules
                    ]
                    # Use a dedicated category for module inventory
                    self._send(proto.DATA, category="knightmare_modules", records=records)

                # Push detected serial devices
                devices = self.ctrl.detect_serial_devices()
                if devices:
                    self._send(proto.DATA,
                               category="knightmare_devices",
                               records=[{"device": d} for d in devices])
            except Exception as e:
                print(f"[data-push] Error: {e}")

            for _ in range(self.DATA_PUSH_INTERVAL):
                if not self._running:
                    return
                time.sleep(1)

    # ------------------------------------------------------------------
    # Command dispatcher
    # ------------------------------------------------------------------

    def _dispatch(self, cmd: str, args: dict) -> str:
        parts = cmd.strip().split()
        if not parts:
            return ""
        verb = parts[0].lower()
        rest = " ".join(parts[1:])

        if verb == "use":
            return self.ctrl.load_module(rest)

        elif verb == "set":
            sub = rest.split(None, 1)
            if len(sub) != 2:
                return "Usage: set <option> <value>"
            return self.ctrl.set_option(sub[0], sub[1])

        elif verb == "run":
            return self.ctrl.run_payload(rest)

        elif verb == "connect":
            return self.ctrl.connect(rest)

        elif verb == "info":
            info = self.ctrl.get_module_info()
            if isinstance(info, dict):
                lines = [
                    f"Module      : {info.get('name', 'N/A')}",
                    f"Description : {info.get('description', 'N/A')}",
                    f"ICARUS      : {info.get('icarus', 'N/A')}",
                    "Options:",
                ]
                for k, v in info.get("options", {}).items():
                    lines.append(f"  {k} = {v}")
                lines.append("Payloads:")
                for p in info.get("payloads", []):
                    lines.append(f"  {p}")
                return "\n".join(lines)
            return str(info)

        elif verb == "show":
            info = self.ctrl.get_module_info()
            if not isinstance(info, dict):
                return "No module loaded."
            if rest == "options":
                return "\n".join(f"{k} = {v}" for k, v in info.get("options", {}).items())
            elif rest == "payloads":
                return "\n".join(info.get("payloads", []))
            return "Usage: show options | show payloads"

        elif verb == "list":
            modules = self.ctrl.list_modules()
            if not modules:
                return "No modules found."
            return "\n".join(
                f"  {m['path']:<35} [{m.get('icarus','N/A')}]  {m.get('description','')}"
                for m in modules
            )

        elif verb == "devices":
            devs = self.ctrl.detect_serial_devices()
            return "\n".join(devs) if devs else "No serial devices detected."

        elif verb == "icarus":
            descriptions = {
                "I": "Integrated Threat Intelligence: adversary profiling, CVEs, telemetry.",
                "C": "Cybersecurity TTPs: ATT&CK tactics adapted for drones/robots/IoT.",
                "A": "Aerial and Aquatic Defense: GPS spoofing, anti-jamming, telemetry hijack.",
                "R": "Robotic System Resilience: firmware hardening, recovery protocols.",
                "U": "Unmanned System Operations: SOP enforcement, comm security.",
                "S": "Systems Monitoring & Response: anomaly detection, C2 feedback loops.",
            }
            pillar = rest.strip().upper()
            return descriptions.get(pillar, "Usage: icarus <I|C|A|R|U|S>")

        elif verb == "help":
            return (
                "Commands:\n"
                "  list                    — list available modules\n"
                "  use <module>            — load a module\n"
                "  info                    — show loaded module details\n"
                "  show options|payloads   — show options or payloads\n"
                "  set <option> <value>    — set a module option\n"
                "  connect <device>        — connect to a serial device\n"
                "  devices                 — list detected serial devices\n"
                "  run <payload>           — execute a payload\n"
                "  icarus <I|C|A|R|U|S>   — ICARUS pillar reference\n"
            )

        else:
            return f"Unknown command: {verb}. Type 'help'."

    # ------------------------------------------------------------------
    # Task handler
    # ------------------------------------------------------------------

    def _handle_task(self, role: str, config: dict):
        """Agent received a task assignment — acknowledge and note it."""
        print(f"[*] Task assigned: role={role} config={config}")
        self._send(proto.TASK_ACK, role=role)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self):
        print(f"[*] Connecting to {self.host}:{self.port}…")
        self.connect()
        self._handshake()
        self._running = True

        # Start background data push
        threading.Thread(target=self._data_push_loop, daemon=True,
                         name="data-push").start()

        print("[*] Waiting for operator commands…")
        while True:
            try:
                msg = self._recv()
            except (ConnectionResetError, OSError):
                print("[-] Lost connection to C2 server.")
                break

            mtype = msg.get("type")

            if mtype == proto.COMMAND:
                cmd  = msg.get("cmd", "")
                args = msg.get("args", {})
                try:
                    output = self._dispatch(cmd, args)
                except Exception as e:
                    output = f"[!] Error: {e}"
                for line in (output or "").splitlines():
                    self._send(proto.OUTPUT, data=line + "\n")
                self._send(proto.DONE)

            elif mtype == proto.TASK:
                self._handle_task(msg.get("role", proto.ROLE_IDLE),
                                  msg.get("config", {}))

        self._running = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Knightmare C2 Agent")
    parser.add_argument("--host",     required=True)
    parser.add_argument("--port",     type=int, default=proto.AGENT_PORT, metavar="PORT")
    parser.add_argument("--password", required=True)
    parser.add_argument("--cert",     default=None, help="Path to server.crt")
    args = parser.parse_args()

    agent = KnightmareAgent(args.host, args.port, args.password, args.cert)
    agent.run()
