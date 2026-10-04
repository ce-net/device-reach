"""The name proxy: make `huey:8940` work, with no root and no edit to /etc/hosts.

Tailscale gives every device an address, so a client types the device's own port number and
the operating system routes it. We cannot do that without root: `127.0.0.1` is the only
loopback address macOS routes by default, a second one needs `ifconfig lo0 alias`, and both
/etc/hosts and /etc/resolver need sudo. Asking for root to read a duck's leg angle is the
wrong trade.

So this is the other way round: one HTTP proxy that resolves device names itself. Point a
client at it and `huey:8940` is a real address for that client:

    http_proxy=http://127.0.0.1:8944 curl http://huey:8940/api/state
    ssh -o ProxyCommand='nc -X connect -x 127.0.0.1:8944 %h %p' arduino@huey

It speaks both proxy forms: absolute-URI requests (plain HTTP) and CONNECT (anything else,
TLS and ssh included). A name is resolved the same way `reach` resolves it — signed ce-iam
bindings first, then the device tables — and the forward is opened on demand.

What it is not: a transparent VPN. A client that does not honour a proxy still needs the
loopback port. That gap closes with a per-device loopback alias, which needs root.
"""

import json
import os
import re
import select
import socket
import socketserver
import sys
import threading

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import reach  # noqa: E402

# "GET http://huey:8940/api/state HTTP/1.1" or "CONNECT huey:8940 HTTP/1.1"
REQ_RE = re.compile(r"^(?P<method>[A-Z]+)\s+(?P<target>\S+)\s+(?P<ver>HTTP/\d\.\d)\s*$")
ABS_RE = re.compile(r"^http://(?P<host>[^/:]+)(?::(?P<port>\d+))?(?P<path>/.*)?$", re.I)
AUTHORITY_RE = re.compile(r"^(?P<host>[^:]+):(?P<port>\d+)$")
# Headers a proxy must not pass through unchanged.
HOP_BY_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive",
              "te", "trailer", "transfer-encoding", "upgrade"}
IDLE_S = float(os.environ.get("REACH_PROXY_IDLE_S", "300"))
HEALTH_PATH = "/healthz"
HEALTH_MARKER = "net.device-reach proxy"

_stats = {"requests": 0, "connect": 0, "refused": 0}


def resolve(host, port):
    """(local_port, detail). Map a device name and port to the loopback port that reaches it.

    `huey`, `huey.ce` and `huey.mesh` are the same device: a suffix is how a resolver-based
    setup would spell it, and refusing it would only surprise people.
    """
    name = host.lower()
    for suffix in (".ce", ".mesh", ".ce-net"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    devs, _ = reach.load_devices()
    dev = devs.get(name)
    if dev is None:
        return None, "no device named %r (known: %s)" % (name, ", ".join(sorted(devs)) or "none")
    local, state, detail = reach.open_forward(dev, port)
    if state == "failed":
        return None, "cannot forward %s:%d: %s" % (name, port, detail)
    return local, "%s:%d -> 127.0.0.1:%d (%s)" % (name, port, local, state)


def splice(a, b):
    """Pump bytes both ways until either side closes or goes idle."""
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, IDLE_S)
            if x or not r:
                return
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                try:
                    (b if s is a else a).sendall(data)
                except OSError:
                    return
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


class Handler(socketserver.StreamRequestHandler):
    timeout = 30

    def _refuse(self, code, reason):
        _stats["refused"] += 1
        body = ("device-reach proxy: %s\n" % reason).encode()
        try:
            self.wfile.write(
                b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                b"Connection: close\r\n\r\n" % (code, b"Error", len(body)) + body)
        except OSError:
            pass

    def _health(self):
        """The one origin-form path this proxy answers about itself."""
        body = json.dumps({"app": HEALTH_MARKER, "ok": True, "stats": dict(_stats)}).encode()
        try:
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                b"Connection: close\r\n\r\n" % len(body) + body)
        except OSError:
            pass

    def handle(self):
        try:
            line = self.rfile.readline(8192).decode("latin-1").rstrip("\r\n")
        except (OSError, UnicodeDecodeError):
            return
        m = REQ_RE.match(line)
        if not m:
            return self._refuse(400, "not a proxy request line: %r" % line[:120])
        method, target = m.group("method"), m.group("target")

        headers = []
        while True:
            try:
                h = self.rfile.readline(8192).decode("latin-1")
            except OSError:
                return
            if h in ("\r\n", "\n", ""):
                break
            headers.append(h.rstrip("\r\n"))

        if method == "CONNECT":
            a = AUTHORITY_RE.match(target)
            if not a:
                return self._refuse(400, "CONNECT needs host:port, got %r" % target[:80])
            return self._connect(a.group("host"), int(a.group("port")))

        u = ABS_RE.match(target)
        if not u:
            # One origin-form path answers about the proxy itself, so a monitor has
            # something it can read. Everything else in origin form is an error, which
            # is what makes that one path a falsifiable probe rather than a catch-all.
            if method == "GET" and target.split("?", 1)[0] == HEALTH_PATH:
                return self._health()
            return self._refuse(
                400, "this is a proxy: the request line needs an absolute URI "
                     "(http://<device>:<port>/path), got %r" % target[:80])
        port = int(u.group("port") or 80)
        return self._forward(u.group("host"), port, method, u.group("path") or "/",
                             m.group("ver"), headers)

    def _open_upstream(self, host, port):
        local, detail = resolve(host, port)
        if local is None:
            self._refuse(502, detail)
            return None
        try:
            up = socket.create_connection(("127.0.0.1", local), timeout=15)
            up.settimeout(None)
            return up
        except OSError as e:
            self._refuse(502, "the forward for %s is bound but 127.0.0.1:%d refused: %s"
                         % (host, local, e))
            return None

    def _connect(self, host, port):
        _stats["connect"] += 1
        up = self._open_upstream(host, port)
        if up is None:
            return
        try:
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
        except OSError:
            up.close()
            return
        splice(self.connection, up)

    def _forward(self, host, port, method, path, ver, headers):
        _stats["requests"] += 1
        up = self._open_upstream(host, port)
        if up is None:
            return
        out = ["%s %s %s" % (method, path, ver)]
        saw_host = False
        for h in headers:
            name = h.split(":", 1)[0].strip().lower()
            if name in HOP_BY_HOP:
                continue
            if name == "host":
                saw_host = True
            out.append(h)
        if not saw_host:
            out.append("Host: %s:%d" % (host, port))
        # One request per connection: after the headers this becomes a raw splice, so a second
        # request on the same connection could be for a different device. Saying so in the
        # protocol is better than guessing at the boundary of a body we did not parse.
        out.append("Connection: close")
        try:
            up.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
        except OSError as e:
            up.close()
            return self._refuse(502, "upstream closed while sending the request: %s" % e)
        splice(self.connection, up)


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_in_thread(port=8944):
    """Start the proxy on 127.0.0.1:<port>. Returns (server, thread) or raises OSError."""
    srv = Proxy(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def stats():
    return dict(_stats)


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("REACH_PROXY_PORT", "8944"))
    srv, _ = serve_in_thread(p)
    sys.stderr.write("device-reach name proxy on http://127.0.0.1:%d/ "
                     "(http_proxy=http://127.0.0.1:%d)\n" % (p, p))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
