"""Tests for net.device-reach. Stdlib unittest, no network needed by default.

Run:  python3 -m unittest discover -s tests -v
Live: REACH_LIVE=1 python3 -m unittest discover -s tests -v
      (also forwards and reads Huey's servo bus over ce-net)

Every check here was broken on purpose once before it was trusted. The ones
that matter most are the refusals: `GateRefuses` deletes the gate and asserts
the mutating route answers 503 with a named reason, because a control whose
failure looks like success is not a control.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, HERE)

import reach  # noqa: E402
import reachd  # noqa: E402

LIVE = os.environ.get("REACH_LIVE") == "1"

TABLE = {
    "devices": {
        "huey": {"what": "test duck", "wallet": "huey", "node_id": "ab" * 32,
                 "user": "arduino", "key": "~/.ssh/nope", "ssh_local": 2238,
                 "lan": "192.168.1.x", "ports": {"servo": 8938, "walk": 8940}},
        "big": {"what": "a device whose port is already above 10000",
                "wallet": "big", "node_id": "cd" * 32, "ports": {"web": 18080}},
    }
}


class TempTable:
    """A device table on disk, pointed at by the env the library reads."""

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "access.json")
        with open(self.path, "w") as f:
            json.dump(TABLE, f)
        self.old = (reach.CE_DEV_TABLE, reach.LOCAL_TABLE)
        reach.CE_DEV_TABLE = self.path
        reach.LOCAL_TABLE = os.path.join(self.dir.name, "absent.json")
        return self

    def __exit__(self, *a):
        reach.CE_DEV_TABLE, reach.LOCAL_TABLE = self.old
        self.dir.cleanup()


class Tables(unittest.TestCase):
    def test_loads_devices_and_notes_nothing_when_fine(self):
        with TempTable():
            devs, notes = reach.load_devices()
        self.assertEqual(sorted(devs), ["big", "huey"])
        self.assertEqual(notes, [], "a healthy table must leave no note")

    def test_missing_required_table_is_a_note_not_an_empty_list(self):
        old = reach.CE_DEV_TABLE
        reach.CE_DEV_TABLE = "/nope/does/not/exist.json"
        try:
            devs, notes = reach.load_devices()
        finally:
            reach.CE_DEV_TABLE = old
        self.assertEqual(devs, {})
        self.assertEqual(len(notes), 1)
        self.assertIn("no table", notes[0].text)

    def test_broken_json_is_a_note_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "access.json")
            open(p, "w").write("{not json")
            old = reach.CE_DEV_TABLE
            reach.CE_DEV_TABLE = p
            try:
                devs, notes = reach.load_devices()
            finally:
                reach.CE_DEV_TABLE = old
        self.assertEqual(devs, {})
        self.assertTrue(notes and "access.json" in notes[0].text)

    def test_the_optional_local_table_absent_is_not_a_note(self):
        with TempTable():
            _, notes = reach.load_devices()
        self.assertEqual(notes, [])


class Ports(unittest.TestCase):
    def test_ssh_is_always_declared(self):
        self.assertEqual(reach.declared_ports(TABLE["devices"]["big"])["ssh"], 22)

    def test_local_port_rule_matches_ce_dev(self):
        """ce-dev uses 10000+remote below 10000; both tools must name one port."""
        d = TABLE["devices"]["huey"]
        self.assertEqual(reach.local_port_for(d, 8938), 18938)
        self.assertEqual(reach.local_port_for(d, 8940), 18940)

    def test_high_remote_ports_are_not_shifted(self):
        self.assertEqual(reach.local_port_for(TABLE["devices"]["big"], 18080), 18080)

    def test_ssh_uses_the_declared_ssh_local(self):
        self.assertEqual(reach.local_port_for(TABLE["devices"]["huey"], 22), 2238)

    def test_port_local_override_wins(self):
        d = dict(TABLE["devices"]["huey"], port_local={"8938": 19999})
        self.assertEqual(reach.local_port_for(d, 8938), 19999)


class Forwards(unittest.TestCase):
    def test_a_device_without_a_wallet_is_refused_by_name(self):
        local, state, detail = reach.open_forward({"ports": {}}, 9999, local=65000)
        self.assertEqual(state, "failed")
        self.assertIn("wallet", detail)

    def test_missing_ce_binary_is_a_named_failure_not_a_silent_pass(self):
        old = reach.CE
        reach.CE = "/nonexistent/ce-binary-xyz"
        try:
            local, state, detail = reach.open_forward({"wallet": "x"}, 9999, local=65001)
        finally:
            reach.CE = old
        self.assertEqual(state, "failed")
        self.assertIn("PATH", detail)


class FakeNode:
    """A stand-in ce node, so the close path can be tested on both node generations.

    `tunnels` None means a node with no registry: /tunnels 404s, which is the state of every
    node built before the close route. Otherwise it is the list of open forwards.
    """

    def __init__(self, tunnels=None, token="tok"):
        self.tunnels = tunnels
        self.token = token
        self.deleted = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/tunnels":
                    if outer.tunnels is None:
                        return self._send(404, {"error": "no such route"})
                    return self._send(200, outer.tunnels)
                if self.path == "/status":
                    return self._send(200, {"node_id": "ab" * 32})
                self._send(404, {"error": "no such route"})

            def do_DELETE(self):
                auth = self.headers.get("Authorization") or ""
                if auth != "Bearer " + outer.token:
                    return self._send(401, {"error": "missing or invalid API token"})
                if outer.tunnels is None:
                    return self._send(404, {"error": "no such route"})
                n = int(self.headers.get("Content-Length") or 0)
                port = json.loads(self.rfile.read(n))["local_port"]
                row = next((t for t in outer.tunnels if t["local_port"] == port), None)
                if row is None:
                    return self._send(404, {"error": "this node holds no forward on 127.0.0.1:%d" % port})
                outer.tunnels.remove(row)
                outer.deleted.append(port)
                self._send(200, {"closed": True, "local_port": port,
                                 "remote_port": row["remote_port"], "node_id": row["node_id"],
                                 "conns": row.get("conns", 0), "open_for_secs": 3})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def __enter__(self):
        self.old = (reach.NODE_API, os.environ.get("CE_API_TOKEN"))
        reach.NODE_API = "http://127.0.0.1:%d" % self.port
        os.environ["CE_API_TOKEN"] = self.token
        return self

    def __exit__(self, *a):
        reach.NODE_API = self.old[0]
        if self.old[1] is None:
            os.environ.pop("CE_API_TOKEN", None)
        else:
            os.environ["CE_API_TOKEN"] = self.old[1]
        self.srv.shutdown()
        self.srv.server_close()


class Closing(unittest.TestCase):
    def test_a_node_without_the_registry_is_named_not_reported_as_empty(self):
        """The failure this guards: 404 read as 'no tunnels open' would silently do nothing."""
        with FakeNode(tunnels=None):
            rows, supported, detail = reach.node_tunnels()
        self.assertEqual(rows, [])
        self.assertIs(supported, False)
        self.assertIn("no tunnel registry", detail)

    def test_closing_on_a_node_without_the_registry_refuses(self):
        with FakeNode(tunnels=None):
            ok, why = reach.close_forward(18938)
        self.assertFalse(ok)
        self.assertIn("cannot be closed", why)

    def test_a_forward_is_listed_and_then_closed(self):
        rows = [{"local_port": 18938, "remote_port": 8938, "node_id": "ab" * 32, "conns": 4}]
        with FakeNode(tunnels=rows) as node:
            got, supported, _ = reach.node_tunnels()
            self.assertIs(supported, True)
            self.assertEqual(got[0]["local_port"], 18938)
            ok, why = reach.close_forward(18938)
            self.assertTrue(ok, why)
            self.assertIn("closed 127.0.0.1:18938", why)
            self.assertEqual(node.deleted, [18938])
            self.assertEqual(reach.node_tunnels()[0], [])

    def test_closing_a_port_the_node_does_not_hold_is_refused_by_name(self):
        with FakeNode(tunnels=[]):
            ok, why = reach.close_forward(65432)
        self.assertFalse(ok)
        self.assertIn("no forward on 127.0.0.1:65432", why)

    def test_without_a_token_the_close_is_refused_not_silently_skipped(self):
        with FakeNode(tunnels=[{"local_port": 18938, "remote_port": 8938, "node_id": "ab", "conns": 0}]):
            os.environ["CE_API_TOKEN"] = "wrong"
            ok, why = reach.close_forward(18938)
        self.assertFalse(ok)
        self.assertIn("API token", why)


class Latency(unittest.TestCase):
    def test_no_answer_is_cannot_determine_never_a_number(self):
        r = reach.latency("http://127.0.0.1:1/", n=2, timeout=0.2)
        self.assertEqual(r["verdict"], "CANNOT DETERMINE")
        self.assertIsNone(r["p50_ms"])
        self.assertIsNone(r["p90_ms"])


class Daemon(unittest.TestCase):
    """The HTTP surface, served for real on a throwaway port."""

    @classmethod
    def setUpClass(cls):
        cls.table = TempTable().__enter__()
        gate, err = reachd.build_gate(0)
        reachd.Handler.gate, reachd.Handler.gate_error = gate, err or "loaded"
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), reachd.Handler)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.table.__exit__()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def post(self, path, body, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health_carries_the_app_marker(self):
        status, doc = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(doc["app"], "net.device-reach")

    def test_unknown_route_404s(self):
        """The negative control. Without this the probe cannot go red."""
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/no-such-route-xyz")
        self.assertEqual(cm.exception.code, 404)

    def test_the_page_is_not_served_for_every_path(self):
        with urllib.request.urlopen(self.url("/"), timeout=5) as r:
            page = r.read()
        self.assertIn(b"net.device-reach", page)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.url("/not-the-page"), timeout=5)

    def test_devices_never_leaks_a_key_path(self):
        _, doc = self.get("/api/devices")
        self.assertNotIn("key", doc["devices"]["huey"])

    def test_up_without_a_capability_is_refused(self):
        status, doc = self.post("/api/up", {"device": "huey", "port": "servo"})
        self.assertIn(status, (401, 403, 503))
        self.assertNotEqual(doc.get("verdict"), "PASS")
        self.assertTrue(doc.get("code") or doc.get("error"), "the refusal must be named")

    def test_down_says_cannot_determine_rather_than_pretending(self):
        status, doc = self.post("/api/down", {})
        # Refused for want of a capability, or answered 501 with the reason.
        self.assertIn(status, (401, 403, 501, 503))
        if status == 501:
            self.assertEqual(doc["verdict"], "CANNOT DETERMINE")

    def test_a_post_without_json_content_type_is_refused(self):
        """cegate checks the transport before the capability. Keep that teeth."""
        req = urllib.request.Request(self.url("/api/up"), data=b"{}",
                                     headers={"Content-Type": "text/plain"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 415)


class GateRefuses(unittest.TestCase):
    """Delete the gate and watch the mutating route refuse, by name."""

    def test_no_gate_means_503_with_a_reason(self):
        old_gate, old_err = reachd.Handler.gate, reachd.Handler.gate_error
        reachd.Handler.gate = None
        reachd.Handler.gate_error = "deliberately removed by the test"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), reachd.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/up" % srv.server_address[1],
                data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 503)
            doc = json.loads(cm.exception.read().decode())
            self.assertEqual(doc["error"], "no_authorization_gate")
            self.assertIn("deliberately removed", doc["reason"])
        finally:
            srv.shutdown()
            srv.server_close()
            reachd.Handler.gate, reachd.Handler.gate_error = old_gate, old_err


@unittest.skipUnless(LIVE, "set REACH_LIVE=1 to reach the real devices")
class Live(unittest.TestCase):
    def test_node_is_up(self):
        up, detail = reach.node_up()
        self.assertTrue(up, "local ce node not answering: %s" % detail)

    def test_huey_servo_answers_over_ce_net(self):
        devs, _ = reach.load_devices()
        self.assertIn("huey", devs)
        dev = devs["huey"]
        local, state, detail = reach.open_forward(dev, dev["ports"]["servo"])
        self.assertNotEqual(state, "failed", detail)
        status, secs, why = reach.http_probe("http://127.0.0.1:%d/api/bus" % local)
        self.assertEqual(status, 200, why)
        self.assertLess(secs, 5.0)


if __name__ == "__main__":
    unittest.main()
