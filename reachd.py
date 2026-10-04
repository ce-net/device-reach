"""reachd — the net.device-reach daemon: keeps the forwards up, answers for them.

Loopback only. Read routes are open on 127.0.0.1; every route that CHANGES
something (opening a forward) goes through the ce authorization system
(`ce-py/cegate.py`) and FAILS CLOSED: no gate, no capability, no verifier, no
root of trust means REFUSE with a named reason, never allow.

Routes
  GET  /                      the machines page
  GET  /api/health            {"app": "net.device-reach", ...}  (the marker)
  GET  /api/devices           the device table, merged, with its notes
  GET  /api/status[?probe=1]  every declared forward and whether it is bound
  POST /api/up                {"device": "huey", "port": "servo"?}   device-reach:up
  POST /api/down              refuses, and says what closing would take
  anything else               404 JSON  (the negative control for the probe)
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import reach  # noqa: E402

CE_PY = os.environ.get("CE_PY_DIR", os.path.expanduser("~/dev/ce-py"))
if os.path.isdir(CE_PY) and CE_PY not in sys.path:
    sys.path.insert(0, CE_PY)

GATE = None
GATE_ERROR = "not built yet"
try:
    import cegate  # noqa: E402
except Exception as _e:  # pragma: no cover - measured by test_gate_absent
    cegate = None
    GATE_ERROR = "%s: %s" % (type(_e).__name__, _e)

KEEPALIVE_S = float(os.environ.get("REACH_KEEPALIVE_S", "30"))
AUTOUP = os.environ.get("REACH_AUTOUP", "1") == "1"
_last_keepalive = {"ts": None, "opened": [], "failed": []}


def build_gate(port):
    """The one door. Returns (gate, error). A missing cegate is an ERROR, not a pass."""
    if cegate is None:
        return None, GATE_ERROR
    try:
        return cegate.Gate(app="device-reach", port=port, ttl=30.0), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def keepalive_once():
    """Re-open any declared forward that is not bound. Returns what happened."""
    devs, _ = reach.load_devices()
    opened, failed = [], []
    for name, dev in sorted(devs.items()):
        for pname, remote in sorted(reach.declared_ports(dev).items()):
            local = reach.local_port_for(dev, remote)
            if reach.listening(local):
                continue
            _, state, detail = reach.open_forward(dev, remote)
            (opened if state == "opened" else failed).append(
                {"device": name, "port": pname, "local": local, "detail": detail})
    _last_keepalive.update({"ts": time.time(), "opened": opened, "failed": failed})
    return _last_keepalive


def _keepalive_loop(stop):
    while not stop.wait(KEEPALIVE_S):
        try:
            keepalive_once()
        except Exception as e:  # a supervisor that dies is worse than one that logs
            _last_keepalive.update({"ts": time.time(), "opened": [],
                                    "failed": [{"detail": "keepalive raised %s: %s"
                                                % (type(e).__name__, e)}]})


PAGE = """<!doctype html><meta charset=utf-8><title>Devices · net.device-reach</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#14181b;--mut:#687076;--line:#e3e8ea;--up:#2f7d52;--down:#b3bec3}
@media (prefers-color-scheme:dark){:root{--bg:#14181b;--fg:#ecedee;--mut:#9ba1a6;--line:#2a3135}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,sans-serif;margin:0;padding:24px 16px}
main{max-width:860px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}p.sub{color:var(--mut);margin:0 0 24px}
table{width:100%;border-collapse:collapse;margin-bottom:28px}
th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px}
.up{background:var(--up)}.down{background:var(--down)}
code{font:13px ui-monospace,Menlo,monospace}a{color:inherit}
.note{color:var(--mut);font-size:13px}
</style><main>
<h1>Devices</h1><p class=sub id=sub>loading…</p>
<div id=out></div>
<p class=note>Forwards are opened by the local ce node over libp2p, direct on a LAN and relayed
elsewhere, and only with a <code>tunnel</code> capability for the target. Nothing here drives
anything: this page reads.</p>
</main><script>
async function load(){
 const s=await (await fetch('/api/status')).json();
 document.getElementById('sub').textContent =
   (s.node.reachable?'ce node reachable':'ce node NOT reachable') + ' · ' + s.devices.length + ' device(s)';
 let h='';
 for(const d of s.devices){
  h+='<h2 style="font-size:16px;margin:0 0 6px">'+d.name+'</h2>';
  h+='<p class=note style="margin:0 0 8px">'+(d.what||'')+'<br><code>'+(d.node_id||'').slice(0,24)+'…</code></p>';
  h+='<table><tr><th>Port</th><th>Remote</th><th>Reach it at</th><th>State</th></tr>';
  for(const p of d.ports){
   const u=p.url?'<a href="'+p.url+'"><code>'+p.url+'</code></a>':'<code>ssh 127.0.0.1:'+p.local+'</code>';
   h+='<tr><td>'+p.port+'</td><td>'+p.remote+'</td><td>'+u+'</td><td><span class="dot '+
      (p.state==='up'?'up':'down')+'"></span>'+p.state+'</td></tr>';
  }
  h+='</table>';
 }
 for(const n of s.notes) h+='<p class=note>note · '+n.source+': '+n.note+'</p>';
 document.getElementById('out').innerHTML=h;
}
load(); setInterval(load,10000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "device-reach/" + reach.VERSION
    gate = None
    gate_error = GATE_ERROR

    def log_message(self, fmt, *a):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % a))

    def _send(self, status, obj, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self._send(404, {"error": "no_such_route", "path": self.path,
                         "app": reach.APP,
                         "routes": ["/", "/api/health", "/api/devices", "/api/status",
                                    "POST /api/up", "POST /api/down"]})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/health":
            up, detail = reach.node_up()
            devs, notes = reach.load_devices()
            return self._send(200, {
                "app": reach.APP, "version": reach.VERSION, "ok": True,
                "ce_node": {"reachable": up, "detail": detail[:80]},
                "devices": len(devs),
                "authorization": {"gate": self.gate is not None,
                                  "error": self.gate_error,
                                  "actions": ["device-reach:up", "device-reach:down"],
                                  "posture": "fail-closed"},
                "keepalive": _last_keepalive,
                "notes": [n.as_dict() for n in notes],
            })
        if path == "/api/devices":
            devs, notes = reach.load_devices()
            return self._send(200, {
                "app": reach.APP,
                "devices": {n: {k: v for k, v in d.items() if k != "key"} for n, d in devs.items()},
                "notes": [n.as_dict() for n in notes]})
        if path == "/api/status":
            return self._send(200, reach.status(probe="probe=1" in query))
        return self._not_found()

    def _guard(self, action):
        """Returns the Principal, or None having already answered. Fails closed."""
        if self.gate is None:
            self._send(503, {"error": "no_authorization_gate",
                             "reason": "the ce authorization gate did not load (%s), so every "
                                       "mutating route refuses" % self.gate_error,
                             "action": action})
            return None
        return self.gate.require(self, action)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/down":
            if self._guard("device-reach:down") is None:
                return
            return self._send(501, {
                "verdict": "CANNOT DETERMINE",
                "error": "close_not_supported",
                "reason": "the listener lives inside the ce node (POST /tunnel) and the node keeps "
                          "no registry of open tunnels and exposes no close route",
                "today": "restarting the local ce node closes every forward",
                "fix": "a tunnels registry in ApiState plus GET /tunnels and DELETE /tunnel in "
                       "ce/crates/ce-node/src/api.rs"})
        if path != "/api/up":
            return self._not_found()
        who = self._guard("device-reach:up")
        if who is None:
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError) as e:
            return self._send(400, {"error": "bad_json", "detail": str(e)})
        devs, _ = reach.load_devices()
        name = body.get("device")
        if name not in devs:
            return self._send(404, {"error": "no_such_device", "device": name,
                                    "known": sorted(devs)})
        dev = devs[name]
        ports = reach.declared_ports(dev)
        want = body.get("port")
        if want is not None:
            if want in ports:
                ports = {want: ports[want]}
            else:
                try:
                    ports = {str(want): int(want)}
                except (TypeError, ValueError):
                    return self._send(404, {"error": "no_such_port", "port": want,
                                            "known": sorted(ports)})
        out = []
        for pname, remote in sorted(ports.items()):
            local, state, detail = reach.open_forward(dev, remote)
            out.append({"port": pname, "remote": remote, "local": local,
                        "state": state, "detail": detail,
                        "url": "http://127.0.0.1:%d" % local})
        return self._send(200, {"device": name, "by": who.display if who else None,
                                "forwards": out})


def serve(port=8943):
    gate, err = build_gate(port)
    Handler.gate, Handler.gate_error = gate, err or "loaded"
    if gate is None:
        sys.stderr.write("device-reach: NO AUTHORIZATION GATE (%s) -> /api/up refuses\n" % err)
    stop = threading.Event()
    if AUTOUP:
        threading.Thread(target=keepalive_once, daemon=True).start()
    threading.Thread(target=_keepalive_loop, args=(stop,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    sys.stderr.write("device-reach %s on http://127.0.0.1:%d/  (gate: %s)\n"
                     % (reach.VERSION, port, "on" if gate else "REFUSING"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(serve(int(sys.argv[1]) if len(sys.argv) > 1 else
                   int(os.environ.get("REACH_PORT", "8943"))))
