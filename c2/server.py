# c2/server.py — Knightmare C2 Server
#
# Two listeners:
#   AGENT_PORT    — reverse TLS connections from Knightmare / TMS agents
#   OPERATOR_PORT — operator consoles (multi-operator, shared sessions)
#
# Key behaviours:
#   • Session locking  — one operator interacts with one agent at a time
#   • Task assignment  — operators assign roles (kismet, bettercap, etc.) to agents
#   • Broadcast        — send a command to all agents or a filtered subset
#   • Data store       — agents push parsed findings; operators query the store

import asyncio
import ssl
import os
import uuid
import logging
import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from . import protocol as proto

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("knightmare.c2")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Session:
    id: str
    platform: str
    hostname: str
    user: str
    capabilities: list
    connected_at: str
    role: str                       # current assigned task role
    locked_by: Optional[str]        # operator id currently interacting
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "platform":     self.platform,
            "hostname":     self.hostname,
            "user":         self.user,
            "capabilities": self.capabilities,
            "connected_at": self.connected_at,
            "role":         self.role,
            "locked_by":    self.locked_by,
        }


@dataclass
class Operator:
    id: str
    name: str
    connected_at: str
    session_id: Optional[str]
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "name":         self.name,
            "connected_at": self.connected_at,
            "session_id":   self.session_id,
        }


class DataStore:
    """
    In-memory store for structured findings pushed by agents.

    Records are keyed by category. Each record carries the source
    session_id, hostname, and UTC timestamp so operators can see
    which unit produced which data.
    """

    def __init__(self):
        # { category: [ {session_id, hostname, timestamp, ...record_fields} ] }
        self._store: dict[str, list] = defaultdict(list)
        self._lock  = asyncio.Lock()

    async def ingest(self, session_id: str, hostname: str,
                     category: str, records: list):
        ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        tagged = [
            {"session_id": session_id, "hostname": hostname,
             "timestamp": ts, **r}
            for r in records
        ]
        async with self._lock:
            self._store[category].extend(tagged)
            # Rolling window — keep last 10 000 records per category
            if len(self._store[category]) > 10_000:
                self._store[category] = self._store[category][-10_000:]

    async def query(self, category: str,
                    session_id: str | None = None,
                    limit: int = 500) -> list:
        async with self._lock:
            records = self._store.get(category, [])
            if session_id:
                records = [r for r in records if r.get("session_id") == session_id]
            return records[-limit:]

    async def summary(self) -> dict:
        """Return record counts per category and per unit."""
        async with self._lock:
            out = {}
            for cat, records in self._store.items():
                by_unit: dict[str, int] = defaultdict(int)
                for r in records:
                    by_unit[r.get("hostname", "?")] += 1
                out[cat] = {"total": len(records), "by_unit": dict(by_unit)}
            return out


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class C2Server:
    def __init__(self, password: str,
                 agent_port: int    = proto.AGENT_PORT,
                 operator_port: int = proto.OPERATOR_PORT):
        self.password_hash  = proto.hash_password(password)
        self.agent_port     = agent_port
        self.operator_port  = operator_port
        self.sessions:  dict[str, Session]  = {}
        self.operators: dict[str, Operator] = {}
        self.data_store = DataStore()
        self._state_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _write(self, writer: asyncio.StreamWriter,
                     lock: asyncio.Lock,
                     msg_type: str, **data):
        async with lock:
            try:
                writer.write(proto.encode(msg_type, **data))
                await writer.drain()
            except Exception:
                pass

    async def _broadcast_operators(self, msg_type: str, **data):
        async with self._state_lock:
            ops = list(self.operators.values())
        for op in ops:
            await self._write(op.writer, op.write_lock, msg_type, **data)

    # ------------------------------------------------------------------
    # Agent handler
    # ------------------------------------------------------------------

    async def handle_agent(self,
                           reader: asyncio.StreamReader,
                           writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        log.info(f"Agent connection from {peer}")
        session: Optional[Session] = None
        write_lock = asyncio.Lock()

        try:
            # Auth
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            msg  = proto.decode(line)
            if (msg.get("type") != proto.AUTH or
                    proto.hash_password(msg.get("password", "")) != self.password_hash):
                await self._write(writer, write_lock, proto.AUTH_FAIL, reason="Bad password")
                return
            await self._write(writer, write_lock, proto.AUTH_OK)

            # Register
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            msg  = proto.decode(line)
            if msg.get("type") != proto.REGISTER:
                return

            session_id = str(uuid.uuid4())[:8].upper()
            session = Session(
                id           = session_id,
                platform     = msg.get("platform",     "unknown"),
                hostname     = msg.get("hostname",     "unknown"),
                user         = msg.get("user",         "unknown"),
                capabilities = msg.get("capabilities", []),
                connected_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                role         = proto.ROLE_IDLE,
                locked_by    = None,
                reader       = reader,
                writer       = writer,
                write_lock   = write_lock,
            )
            async with self._state_lock:
                self.sessions[session_id] = session
            await self._write(writer, write_lock, proto.REGISTER_OK, session_id=session_id)
            log.info(f"[+] Session {session_id} — {session.platform}@{session.hostname} ({session.user})")
            await self._broadcast_operators(proto.SESSION_NEW, session=session.to_dict())

            # Message loop — route output to operators, ingest data records
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg   = proto.decode(line)
                mtype = msg.get("type")

                # Structured data push from agent
                if mtype == proto.DATA:
                    category = msg.get("category", "unknown")
                    records  = msg.get("records",  [])
                    await self.data_store.ingest(
                        session.id, session.hostname, category, records)
                    # Broadcast a lightweight notification to all operators
                    await self._broadcast_operators(
                        proto.DATA,
                        category   = category,
                        session_id = session.id,
                        hostname   = session.hostname,
                        count      = len(records),
                    )

                elif mtype == proto.TASK_ACK:
                    role = msg.get("role", "")
                    async with self._state_lock:
                        session.role = role
                    log.info(f"Session {session.id} task ACK: {role}")
                    await self._broadcast_operators(
                        proto.SESSION_NEW, session=session.to_dict())

                # Command output / done — route to locked operator
                elif mtype in (proto.OUTPUT, proto.DONE, proto.ERROR):
                    async with self._state_lock:
                        op_id = session.locked_by
                        op    = self.operators.get(op_id) if op_id else None
                    if op:
                        await self._write(op.writer, op.write_lock, mtype,
                                          **{k: v for k, v in msg.items()
                                             if k != "type"})

        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
            pass
        except Exception as e:
            log.error(f"Agent handler error: {e}")
        finally:
            if session:
                async with self._state_lock:
                    self.sessions.pop(session.id, None)
                    op_id = session.locked_by
                    if op_id and op_id in self.operators:
                        self.operators[op_id].session_id = None
                log.info(f"[-] Session {session.id} disconnected")
                await self._broadcast_operators(proto.SESSION_GONE,
                                                session_id=session.id)
            writer.close()

    # ------------------------------------------------------------------
    # Operator handler
    # ------------------------------------------------------------------

    async def handle_operator(self,
                               reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        log.info(f"Operator connection from {peer}")
        operator: Optional[Operator] = None
        write_lock = asyncio.Lock()

        try:
            # Auth
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            msg  = proto.decode(line)
            if (msg.get("type") != proto.AUTH or
                    proto.hash_password(msg.get("password", "")) != self.password_hash):
                await self._write(writer, write_lock, proto.AUTH_FAIL, reason="Bad password")
                return
            await self._write(writer, write_lock, proto.AUTH_OK)

            op_id   = str(uuid.uuid4())[:8].upper()
            op_name = msg.get("name", f"op-{op_id}")
            operator = Operator(
                id           = op_id,
                name         = op_name,
                connected_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                session_id   = None,
                reader       = reader,
                writer       = writer,
                write_lock   = write_lock,
            )
            async with self._state_lock:
                self.operators[op_id] = operator
            log.info(f"[+] Operator {op_name} ({op_id}) connected")
            await self._broadcast_operators(
                proto.OPERATORS,
                data=[o.to_dict() for o in self.operators.values()])

            # Command loop
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg   = proto.decode(line)
                mtype = msg.get("type")

                # sessions
                if mtype == proto.SESSIONS:
                    async with self._state_lock:
                        data = [s.to_dict() for s in self.sessions.values()]
                    await self._write(writer, write_lock, proto.SESSIONS, data=data)

                # operators
                elif mtype == proto.OPERATORS:
                    async with self._state_lock:
                        data = [o.to_dict() for o in self.operators.values()]
                    await self._write(writer, write_lock, proto.OPERATORS, data=data)

                # interact
                elif mtype == proto.INTERACT:
                    sid = msg.get("session_id", "")
                    async with self._state_lock:
                        sess = self.sessions.get(sid)
                        if not sess:
                            err = f"Session {sid} not found"
                        elif sess.locked_by and sess.locked_by != op_id:
                            locker = self.operators.get(sess.locked_by)
                            err = f"Locked by {locker.name if locker else sess.locked_by}"
                        else:
                            if operator.session_id and operator.session_id in self.sessions:
                                self.sessions[operator.session_id].locked_by = None
                            sess.locked_by      = op_id
                            operator.session_id = sid
                            err = None
                    if err:
                        await self._write(writer, write_lock, proto.INTERACT_FAIL, reason=err)
                    else:
                        await self._write(writer, write_lock, proto.INTERACT_OK,
                                          session=sess.to_dict())

                # release
                elif mtype == proto.RELEASE:
                    async with self._state_lock:
                        if operator.session_id and operator.session_id in self.sessions:
                            self.sessions[operator.session_id].locked_by = None
                        operator.session_id = None
                    await self._write(writer, write_lock, proto.DONE)

                # forward command to locked agent
                elif mtype == proto.COMMAND:
                    async with self._state_lock:
                        sid  = operator.session_id
                        sess = self.sessions.get(sid) if sid else None
                    if not sess:
                        await self._write(writer, write_lock, proto.ERROR,
                                          reason="No active session. Use 'interact <id>' first.")
                    else:
                        await self._write(sess.writer, sess.write_lock, proto.COMMAND,
                                          cmd=msg.get("cmd", ""), args=msg.get("args", {}))

                # assign task/role to a session
                elif mtype == proto.TASK_ASSIGN:
                    sid  = msg.get("session_id", "")
                    role = msg.get("role", proto.ROLE_IDLE)
                    cfg  = msg.get("config", {})
                    async with self._state_lock:
                        sess = self.sessions.get(sid)
                    if not sess:
                        await self._write(writer, write_lock, proto.ERROR,
                                          reason=f"Session {sid} not found")
                    elif role not in proto.ALL_ROLES:
                        await self._write(writer, write_lock, proto.ERROR,
                                          reason=f"Unknown role '{role}'. Valid: {proto.ALL_ROLES}")
                    else:
                        await self._write(sess.writer, sess.write_lock,
                                          proto.TASK, role=role, config=cfg)
                        await self._write(writer, write_lock, proto.TASK_ACK,
                                          session_id=sid, role=role)
                        log.info(f"Task '{role}' assigned to session {sid} by {operator.name}")

                # list current task roles
                elif mtype == proto.TASKS:
                    async with self._state_lock:
                        data = [
                            {"session_id": s.id, "hostname": s.hostname,
                             "platform": s.platform, "role": s.role}
                            for s in self.sessions.values()
                        ]
                    await self._write(writer, write_lock, proto.TASKS, data=data)

                # broadcast command to group of agents
                elif mtype == proto.BROADCAST:
                    filt    = msg.get("filter", "all")   # "all" | "tms" | "knightmare" | role name
                    cmd_str = msg.get("cmd", "")
                    async with self._state_lock:
                        targets = self._filter_sessions(filt)
                    sent_to = []
                    for sess in targets:
                        await self._write(sess.writer, sess.write_lock,
                                          proto.COMMAND, cmd=cmd_str, args={})
                        sent_to.append(sess.id)
                    await self._write(writer, write_lock, proto.BROADCAST_OK,
                                      sent_to=sent_to, count=len(sent_to))

                # data store query
                elif mtype == proto.DATA_QUERY:
                    category   = msg.get("category",   "")
                    session_flt= msg.get("session_id", None)
                    limit      = msg.get("limit",      500)

                    if category == "summary":
                        summary = await self.data_store.summary()
                        await self._write(writer, write_lock, proto.DATA_RESP,
                                          category="summary", records=summary)
                    elif category in proto.ALL_CATEGORIES:
                        records = await self.data_store.query(category, session_flt, limit)
                        await self._write(writer, write_lock, proto.DATA_RESP,
                                          category=category, records=records,
                                          total=len(records))
                    else:
                        await self._write(writer, write_lock, proto.ERROR,
                                          reason=f"Unknown category '{category}'. "
                                                 f"Valid: summary, {', '.join(proto.ALL_CATEGORIES)}")

                elif mtype == proto.PING:
                    await self._write(writer, write_lock, proto.PONG)

        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
            pass
        except Exception as e:
            log.error(f"Operator handler error: {e}")
        finally:
            if operator:
                async with self._state_lock:
                    if operator.session_id and operator.session_id in self.sessions:
                        self.sessions[operator.session_id].locked_by = None
                    self.operators.pop(operator.id, None)
                log.info(f"[-] Operator {operator.name} disconnected")
            writer.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _filter_sessions(self, filt: str) -> list[Session]:
        """Return sessions matching filter. Must be called with _state_lock held."""
        sessions = list(self.sessions.values())
        if filt == "all":
            return sessions
        if filt in (proto.PLATFORM_TMS, proto.PLATFORM_KNIGHTMARE):
            return [s for s in sessions if s.platform == filt]
        if filt in proto.ALL_ROLES:
            return [s for s in sessions if s.role == filt]
        # Try exact session ID
        return [s for s in sessions if s.id == filt]

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    async def start(self):
        cert_path, key_path = _ensure_certs()

        def _make_ssl() -> ssl.SSLContext:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert_path, key_path)
            return ctx

        agent_srv    = await asyncio.start_server(
            self.handle_agent,    "0.0.0.0", self.agent_port,    ssl=_make_ssl())
        operator_srv = await asyncio.start_server(
            self.handle_operator, "0.0.0.0", self.operator_port, ssl=_make_ssl())

        print(_banner())
        log.info(f"Agent listener    : 0.0.0.0:{self.agent_port}")
        log.info(f"Operator listener : 0.0.0.0:{self.operator_port}")
        log.info(f"Certificate       : {cert_path}")
        log.info("Ready — waiting for agents and operators.")

        async with agent_srv, operator_srv:
            await asyncio.gather(
                agent_srv.serve_forever(),
                operator_srv.serve_forever(),
            )


# ---------------------------------------------------------------------------
# TLS certificate helpers
# ---------------------------------------------------------------------------

def _ensure_certs() -> tuple[str, str]:
    cert_dir  = os.path.join(os.path.dirname(__file__), "certs")
    os.makedirs(cert_dir, exist_ok=True)
    cert_path = os.path.join(cert_dir, "server.crt")
    key_path  = os.path.join(cert_dir, "server.key")
    if not os.path.exists(cert_path) or not os.path.exists(key_path):
        _generate_self_signed(cert_path, key_path)
    return cert_path, key_path


def _generate_self_signed(cert_path: str, key_path: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    log.info("Generating self-signed TLS certificate (10-year validity)…")
    key  = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "knightmare-c2")])
    now  = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    log.info(f"Certificate saved  → {cert_path}")
    log.info("Distribute server.crt to all agents and operators (--cert flag).")


def _banner() -> str:
    return """
██╗  ██╗███╗   ██╗██╗ ██████╗ ██╗  ██╗████████╗███╗   ███╗ █████╗ ██████╗ ███████╗
██║ ██╔╝████╗  ██║██║██╔════╝ ██║  ██║╚══██╔══╝████╗ ████║██╔══██╗██╔══██╗██╔════╝
█████╔╝ ██╔██╗ ██║██║██║  ███╗███████║   ██║   ██╔████╔██║███████║██████╔╝█████╗
██╔═██╗ ██║╚██╗██║██║██║   ██║██╔══██║   ██║   ██║╚██╔╝██║██╔══██║██╔══██╗██╔══╝
██║  ██╗██║ ╚████║██║╚██████╔╝██║  ██║   ██║   ██║ ╚═╝ ██║██║  ██║██║  ██║███████╗
╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝

  C2 Teamserver — Tengu Edition  |  Multi-operator  |  WPA3 Research
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Knightmare C2 Teamserver")
    parser.add_argument("--password",      required=True,
                        help="Shared operator/agent password")
    parser.add_argument("--agent-port",    type=int, default=proto.AGENT_PORT,    metavar="PORT")
    parser.add_argument("--operator-port", type=int, default=proto.OPERATOR_PORT, metavar="PORT")
    args = parser.parse_args()

    server = C2Server(args.password, args.agent_port, args.operator_port)
    asyncio.run(server.start())
