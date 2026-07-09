"""LAN networking for IronStack: discover peers and stream a live meeting from a
Host to one or more Viewers.

Two roles, same app:
  - Host   opens a TCP server and periodically broadcasts a small UDP beacon so
           Viewers on the same LAN can find it. During a meeting it pushes JSON
           messages -- the interviewer's questions, the candidate's spoken
           answers, and the AI answer -- to every connected Viewer.
  - Viewer listens for beacons (discovery), connects to a Host's TCP server with
           a pairing code, and receives the JSON message stream, replaying each
           message onto its own overlay.

The clean trick: the transcriber/answerer already talk to a tiny "console"
interface on the overlay (info / partial / show_question / begin_answer /
answer_delta / end_answer). `Broadcaster` implements that same interface and, for
each call, updates the local overlay AND emits a network message. On the Viewer,
`apply_message` calls the very same overlay methods from the received messages --
so a meeting mirrors across the wire with almost no change to the pipeline.

Wire format: newline-delimited UTF-8 JSON, one message object per line.
"""

import json
import time
import uuid
import socket
import threading

from logsetup import get_logger

# --- protocol constants ---
DISCOVERY_PORT = 48920      # UDP beacon port (Host -> LAN broadcast)
DEFAULT_TCP_PORT = 48921    # TCP stream port (Viewer connects here)
BEACON_INTERVAL = 2.0       # seconds between UDP beacons
BEACON_TTL = 6.0            # a host not heard from within this is dropped from discovery
MAGIC = "IRONSTACK1"        # protocol tag, so we ignore unrelated UDP traffic


# --- small wire helpers ---

def _send_line(sock, msg):
    """Serialize one message object and write it as a single newline-framed line."""
    sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))


def _recv_line(conn, timeout=10):
    """Read a single newline-framed line (used only for the short handshake)."""
    conn.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:      # a handshake line is tiny; anything huge is bogus
            break
    line, _, _ = buf.partition(b"\n")
    return line


# ======================================================================
# Discovery (UDP)
# ======================================================================

class Beacon:
    """Host side: broadcast our presence on the LAN a few times a second so
    Viewers can discover us without knowing our IP."""

    def __init__(self, name, host_id, tcp_port):
        self.name = name
        self.host_id = host_id
        self.tcp_port = tcp_port
        self.in_meeting = False
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                packet = json.dumps({
                    "magic": MAGIC,
                    "name": self.name,
                    "host_id": self.host_id,
                    "tcp_port": self.tcp_port,
                    "in_meeting": self.in_meeting,
                }).encode("utf-8")
                try:
                    sock.sendto(packet, ("255.255.255.255", DISCOVERY_PORT))
                except OSError:
                    pass   # e.g. transient "network unreachable" -- try again next tick
                self._stop.wait(BEACON_INTERVAL)
        finally:
            sock.close()


class Discovery:
    """Viewer side: listen for host beacons and keep a live, TTL-pruned list of
    the hosts currently visible on the LAN."""

    def __init__(self):
        self._peers = {}        # (ip, tcp_port) -> peer dict incl. "last" monotonic time
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def peers(self):
        """Hosts seen within the TTL window, newest-broadcast first."""
        now = time.monotonic()
        with self._lock:
            live = [dict(p) for p in self._peers.values() if now - p["last"] <= BEACON_TTL]
        live.sort(key=lambda p: p["last"], reverse=True)
        return live

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError:
            get_logger().exception("discovery: could not bind UDP %d", DISCOVERY_PORT)
            sock.close()
            return
        sock.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if msg.get("magic") != MAGIC:
                    continue
                ip = addr[0]
                port = int(msg.get("tcp_port", DEFAULT_TCP_PORT))
                with self._lock:
                    self._peers[(ip, port)] = {
                        "name": msg.get("name", "IronStack"),
                        "host_id": msg.get("host_id", ""),
                        "ip": ip,
                        "tcp_port": port,
                        "in_meeting": bool(msg.get("in_meeting")),
                        "last": time.monotonic(),
                    }
        finally:
            sock.close()


# ======================================================================
# TCP stream: server (Host) and client (Viewer)
# ======================================================================

class NetServer:
    """Host side: accept Viewer connections (each approved by the host via
    `on_approve`) and broadcast meeting messages to all of them."""

    def __init__(self, tcp_port, name="IronStack", on_approve=None, on_message=None):
        self.tcp_port = tcp_port
        self.name = name
        # on_approve(peer_dict) -> bool. Called on the handshake thread when a
        # viewer connects; may block until the host decides. None == auto-accept.
        self.on_approve = on_approve
        # on_message(msg). Called when a connected viewer sends a message back
        # (e.g. a shared-notepad edit). The server also relays it to the other
        # viewers so everyone stays in sync.
        self.on_message = on_message
        self.in_meeting = False
        self._clients = []          # list of connected sockets
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # No SO_REUSEADDR here: on Windows it would let a second host silently
        # bind the same port. We WANT a clean "address in use" error so the UI
        # can tell the user another host is already running on this machine.
        sock.bind(("", self.tcp_port))     # raises OSError if the port is taken
        sock.listen(5)
        self._sock = sock
        threading.Thread(target=self._accept_loop, daemon=True).start()
        get_logger().info("net: host server listening on TCP %d", self.tcp_port)

    def _accept_loop(self):
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            get_logger().info("net: incoming TCP connection from %s", addr[0])
            threading.Thread(target=self._handshake, args=(conn, addr),
                             daemon=True).start()

    def _handshake(self, conn, addr):
        try:
            line = _recv_line(conn, timeout=15)
            hello = json.loads(line.decode("utf-8")) if line else {}
            peer = {"name": hello.get("name", "Viewer"), "ip": addr[0]}
            get_logger().info("net: hello from %s (name=%r) -> awaiting host approval",
                              addr[0], peer["name"])

            # Ask the host to approve this viewer (an Accept/Reject dialog on the
            # host). This can block until the host clicks. No approver == accept.
            approved = True
            if self.on_approve is not None:
                try:
                    approved = bool(self.on_approve(peer))
                except Exception:
                    get_logger().exception("net: approval callback failed")
                    approved = False
            if not approved:
                _send_line(conn, {"type": "denied",
                                  "reason": "the host declined the connection"})
                conn.close()
                get_logger().info("net: viewer %s (%s) declined by host",
                                  peer["name"], addr[0])
                return

            _send_line(conn, {"type": "welcome", "name": self.name,
                              "in_meeting": self.in_meeting})
            with self._lock:
                self._clients.append(conn)
            get_logger().info("net: viewer connected from %s (name=%r)",
                              addr[0], peer["name"])
            # Full-duplex: read messages this viewer sends back (shared notepad).
            self._client_recv(conn)
        except Exception:
            get_logger().exception("net: handshake failed for %s", addr[0])
            try:
                conn.close()
            except OSError:
                pass

    def _client_recv(self, conn):
        """Receive-loop for one connected viewer. Relays each message to the
        other viewers and hands it to the host's on_message handler. Runs on this
        connection's handshake thread until the viewer disconnects."""
        buf = b""
        try:
            conn.settimeout(0.5)
        except OSError:
            return
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self.send(msg, exclude=conn)    # keep other viewers in sync
                if self.on_message:
                    try:
                        self.on_message(msg)
                    except Exception:
                        get_logger().exception("net: host on_message failed")
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
        try:
            conn.close()
        except OSError:
            pass
        get_logger().info("net: viewer disconnected")

    def send(self, msg, exclude=None):
        """Broadcast one message to every connected viewer (optionally skipping
        `exclude`, the viewer it came from); drop any that error."""
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with self._lock:
            clients = list(self._clients)
        dead = []
        for c in clients:
            if c is exclude:
                continue
            try:
                c.sendall(data)
            except OSError:
                dead.append(c)
        if dead:
            with self._lock:
                for c in dead:
                    if c in self._clients:
                        self._clients.remove(c)
                    try:
                        c.close()
                    except OSError:
                        pass

    def client_count(self):
        with self._lock:
            return len(self._clients)

    def stop(self):
        self._stop.set()
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


class NetClient:
    """Viewer side: connect to a Host, complete the pairing handshake, and hand
    each received message to `on_message` (called on the receive thread)."""

    def __init__(self, host, port, code="", name="", on_message=None, on_status=None):
        self.host = host
        self.port = int(port)
        self.code = str(code or "")
        self.name = name
        self.on_message = on_message
        self.on_status = on_status
        self._stop = threading.Event()
        self._sock = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, msg):
        """Send a message back to the host (e.g. a shared-notepad edit)."""
        sock = self._sock
        if sock is None:
            return
        try:
            _send_line(sock, msg)
        except OSError:
            pass

    def _status(self, text):
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                get_logger().exception("net: viewer status callback failed")

    def _run(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
        except OSError as exc:
            self._status(f"connection failed: {exc}")
            return
        self._sock = sock
        try:
            _send_line(sock, {"type": "hello", "code": self.code, "name": self.name})
        except OSError as exc:
            self._status(f"connection failed: {exc}")
            sock.close()
            return
        # TCP is up and the hello is sent; now we block until the host accepts.
        self._status("waiting for host to accept…")

        buf = b""
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                self._status("host closed the connection")
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._dispatch(msg)
        try:
            sock.close()
        except OSError:
            pass

    def _dispatch(self, msg):
        t = msg.get("type")
        if t == "denied":
            self._status("denied: " + msg.get("reason", "wrong code"))
            self._stop.set()
            return
        if t == "welcome":
            self._status("connected to " + msg.get("name", "host"))
            return
        if self.on_message:
            try:
                self.on_message(msg)
            except Exception:
                get_logger().exception("net: viewer message handler failed")

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


# ======================================================================
# Bridge between the overlay "console" interface and the network
# ======================================================================

class Broadcaster:
    """Console shim used on the Host. It mirrors every display call to the local
    overlay AND, when a server is attached, to all connected Viewers. The
    transcriber and answerer talk to this exactly as they would to the overlay,
    so networking needs no changes inside the pipeline."""

    def __init__(self, overlay, server=None):
        self.overlay = overlay
        self.server = server

    def _emit(self, type_, **data):
        if self.server is not None:
            try:
                self.server.send({"type": type_, **data})
            except Exception:
                get_logger().exception("net: broadcast failed (type=%s)", type_)

    # --- interviewer caption + status (transcriber) ---
    def info(self, text):
        self.overlay.info(text)
        self._emit("info", text=str(text))

    def partial(self, text):
        self.overlay.partial(text)
        self._emit("partial", text=text or "")

    # --- interviewer question + AI answer (answerer) ---
    def show_question(self, text):
        self.overlay.show_question(text)
        self._emit("question", text=text)

    def begin_answer(self):
        self.overlay.begin_answer()
        self._emit("answer_begin")

    def answer_delta(self, delta):
        self.overlay.answer_delta(delta)
        self._emit("answer_delta", delta=delta)

    def end_answer(self, note=""):
        self.overlay.end_answer(note)
        self._emit("answer_end", note=note or "")

    # --- candidate's own spoken answer (mic transcriber) ---
    # The candidate's own talking is shown only on the Viewers, never on the Host,
    # so these are net-only (no local overlay update).
    def user_partial(self, text):
        self._emit("user_partial", text=text or "")

    def show_user_answer(self, text):
        self._emit("user_answer", text=text)

    # --- shared notepad (host's local edits -> viewers) ---
    def share_text(self, text):
        self._emit("shared_text", text=text)

    def meeting_start(self, language=""):
        self._emit("meeting_start", language=language)

    def meeting_end(self):
        self._emit("meeting_end")

    def close(self):
        try:
            self.overlay.close()
        except Exception:
            pass


def apply_message(overlay, msg):
    """Viewer side: replay one received message onto the local overlay. The
    overlay's methods are all thread-safe (they emit Qt signals), so this can be
    called straight from the receive thread."""
    t = msg.get("type")
    if t == "info":
        overlay.info(msg.get("text", ""))
    elif t == "partial":
        overlay.partial(msg.get("text", ""))
    elif t == "question":
        overlay.show_question(msg.get("text", ""))
    elif t == "answer_begin":
        overlay.begin_answer()
    elif t == "answer_delta":
        overlay.answer_delta(msg.get("delta", ""))
    elif t == "answer_end":
        overlay.end_answer(msg.get("note", ""))
    elif t == "user_partial":
        overlay.user_partial(msg.get("text", ""))
    elif t == "user_answer":
        overlay.show_user_answer(msg.get("text", ""))
    elif t == "shared_text":
        overlay.set_shared_text(msg.get("text", ""))
    elif t == "meeting_start":
        overlay.info("[meeting started on host]")
    elif t == "meeting_end":
        overlay.partial("")
        overlay.info("[meeting ended on host]")


# ======================================================================
# App-level controller: owns identity, hosting and discovery
# ======================================================================

def _default_name():
    try:
        return socket.gethostname() or "IronStack"
    except Exception:
        return "IronStack"


def local_ip():
    """Best-effort LAN IP of this machine (for showing the host address). Falls
    back to loopback."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))     # no packet sent; just picks the outbound iface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class _HostNet:
    """Bundle of the host's TCP server + UDP beacon, started/stopped together."""

    def __init__(self, name, host_id, tcp_port, on_approve=None):
        self.server = NetServer(tcp_port, name=name, on_approve=on_approve)
        self.beacon = Beacon(name, host_id, tcp_port)

    def start(self):
        self.server.start()       # may raise OSError (port in use) -> caller handles
        self.beacon.start()

    def stop(self):
        self.beacon.stop()
        self.server.stop()

    def set_name(self, name):
        self.server.name = name
        self.beacon.name = name

    def set_in_meeting(self, value):
        self.server.in_meeting = value
        self.beacon.in_meeting = value

    def send(self, msg):
        self.server.send(msg)


class NetController:
    """Single object the UI and main loop share. Holds this instance's stable
    identity (id, name, pairing code) and manages hosting + discovery lifecycles.

    Identity is persisted in the settings table so a host keeps the same name and
    pairing code across launches (a Viewer doesn't need a fresh code each time).
    """

    def __init__(self, database):
        self._db = database
        self.host_id = database.get_setting("net_host_id", "")
        if not self.host_id:
            self.host_id = uuid.uuid4().hex[:12]
            database.set_setting("net_host_id", self.host_id)
        self.name = database.get_setting("instance_name", "") or _default_name()
        # Host listen port is configurable so it can match a VPN/router port-
        # forward (e.g. an Astrill port-forwarding port), not just the default.
        try:
            self.tcp_port = int(database.get_setting("net_host_port", str(DEFAULT_TCP_PORT)))
        except (TypeError, ValueError):
            self.tcp_port = DEFAULT_TCP_PORT

        self._hostnet = None
        self._discovery = None

    # --- identity ---
    def set_name(self, name):
        name = (name or "").strip() or _default_name()
        self.name = name
        self._db.set_setting("instance_name", name)
        if self._hostnet is not None:
            self._hostnet.set_name(name)

    def set_tcp_port(self, port):
        """Change the host listen port (persisted). Applies the next time hosting
        starts, since the server binds the port at start."""
        self.tcp_port = int(port)
        self._db.set_setting("net_host_port", str(int(port)))

    def local_ip(self):
        return local_ip()

    # --- hosting ---
    def is_hosting(self):
        return self._hostnet is not None

    def start_hosting(self, on_approve=None):
        """Start the TCP server + beacon. `on_approve(peer)` is called to accept
        or reject each connecting viewer. Returns None on success or an error
        string (e.g. the port is already in use -> another host on this PC).

        Also tries to open the port on the router automatically over UPnP, so the
        host can be reached from the internet with no manual port-forwarding."""
        if self._hostnet is not None:
            return None
        hostnet = _HostNet(self.name, self.host_id, self.tcp_port, on_approve)
        try:
            hostnet.start()
        except OSError as exc:
            get_logger().exception("net: failed to start hosting")
            return f"{exc}"
        self._hostnet = hostnet
        threading.Thread(target=self._try_upnp, args=(self.tcp_port,),
                         daemon=True).start()
        return None

    def _try_upnp(self, port):
        """Best-effort automatic router port-opening (runs off the GUI thread)."""
        try:
            from services import upnp
            external_ip, err = upnp.add_port_mapping(port, local_ip())
        except Exception:
            get_logger().exception("upnp: mapping attempt crashed")
            return
        if err:
            get_logger().warning(
                "upnp: could NOT auto-open port %d (%s). Viewers on the internet "
                "won't reach this host until you port-forward TCP %d to this PC on "
                "the router, or enable UPnP on the router.", port, err, port)
        else:
            where = f" -> reachable at {external_ip}:{port}" if external_ip else ""
            get_logger().info("upnp: opened router port %d automatically%s", port, where)

    def stop_hosting(self):
        if self._hostnet is not None:
            self._hostnet.stop()
            self._hostnet = None
            # Best-effort: remove the UPnP mapping we added (off the GUI thread).
            port = self.tcp_port

            def _cleanup():
                try:
                    from services import upnp
                    upnp.delete_port_mapping(port)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, daemon=True).start()

    def send(self, msg):
        if self._hostnet is not None:
            self._hostnet.send(msg)

    def set_in_meeting(self, value):
        if self._hostnet is not None:
            self._hostnet.set_in_meeting(value)

    @property
    def server(self):
        """The live NetServer (for a Broadcaster), or None when not hosting."""
        return self._hostnet.server if self._hostnet is not None else None

    def viewer_count(self):
        return self._hostnet.server.client_count() if self._hostnet is not None else 0

    # --- discovery ---
    def start_discovery(self):
        if self._discovery is None:
            self._discovery = Discovery()
            self._discovery.start()

    def peers(self):
        return self._discovery.peers() if self._discovery is not None else []

    def shutdown(self):
        self.stop_hosting()
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
