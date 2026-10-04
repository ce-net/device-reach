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
import time
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
    """A device table on disk, pointed at by the env the library reads.

    `table` overrides the default fixture. `names=False` also points the ce-iam binary at a
    path that does not exist, so the test sees only the table and this machine's real signed
    bindings cannot leak into the result.
    """

    def __init__(self, table=None, names=True):
        self.table = TABLE if table is None else table
        self.names = names

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "access.json")
        with open(self.path, "w") as f:
            json.dump(self.table, f)
        self.old = (reach.CE_DEV_TABLE, reach.LOCAL_TABLE, reach.CE_IAM)
        reach.CE_DEV_TABLE = self.path
        reach.LOCAL_TABLE = os.path.join(self.dir.name, "absent.json")
        if not self.names:
            reach.CE_IAM = os.path.join(self.dir.name, "no-ce-iam-here")
        return self

    def __exit__(self, *a):
        reach.CE_DEV_TABLE, reach.LOCAL_TABLE, reach.CE_IAM = self.old
        self.dir.cleanup()


class Tables(unittest.TestCase):
    def test_loads_devices_and_notes_nothing_when_fine(self):
        with TempTable():
            devs, notes = reach.load_devices(names=False)
        self.assertEqual(sorted(devs), ["big", "huey"])
        self.assertEqual(notes, [], "a healthy table must leave no note")

    def test_missing_required_table_is_a_note_not_an_empty_list(self):
        old = reach.CE_DEV_TABLE
        reach.CE_DEV_TABLE = "/nope/does/not/exist.json"
        try:
            devs, notes = reach.load_devices(names=False)
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
                devs, notes = reach.load_devices(names=False)
            finally:
                reach.CE_DEV_TABLE = old
        self.assertEqual(devs, {})
        self.assertTrue(notes and "access.json" in notes[0].text)

    def test_the_optional_local_table_absent_is_not_a_note(self):
        with TempTable():
            _, notes = reach.load_devices(names=False)
        self.assertEqual(notes, [])


class FakeCeIam:
    """A stand-in `ce-iam`, so the signed-name source is testable without the real one."""

    def __init__(self, lines, rc=0):
        self.script = "#!/bin/sh\ncat <<'EOF'\n%s\nEOF\nexit %d\n" % ("\n".join(lines), rc)

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        p = os.path.join(self.dir.name, "ce-iam")
        with open(p, "w") as f:
            f.write(self.script)
        os.chmod(p, 0o755)
        self.old = reach.CE_IAM
        reach.CE_IAM = p
        return self

    def __exit__(self, *a):
        reach.CE_IAM = self.old
        self.dir.cleanup()


class Names(unittest.TestCase):
    LINE = ("huey                 -> " + "75" * 32 +
            "  issuer 0e95015b…  expires 1822659205")

    def test_a_signed_binding_becomes_a_reachable_device(self):
        with FakeCeIam([self.LINE]), TempTable():
            reach.CE_DEV_TABLE = "/nope/absent.json"  # names only
            devs, _ = reach.load_devices()
        self.assertIn("huey", devs)
        self.assertEqual(devs["huey"]["node_id"], "75" * 32)
        self.assertTrue(devs["huey"]["name_bound"])

    def test_a_name_only_device_declares_no_ports(self):
        """Not even ssh: the binding says which node, never what it serves."""
        with FakeCeIam([self.LINE]):
            bound, note = reach.name_bindings()
        self.assertIsNone(note)
        dev = {"node_id": bound["huey"], "_source": "ce-iam name"}
        self.assertEqual(reach.declared_ports(dev), {})

    def test_a_disagreement_keeps_the_table_and_leaves_a_note(self):
        with FakeCeIam([self.LINE]), TempTable():
            devs, notes = reach.load_devices()
        self.assertEqual(devs["huey"]["node_id"], "ab" * 32, "the table's node id wins")
        self.assertTrue(devs["huey"].get("name_conflict"))
        self.assertTrue(any("signed binding" in n.text for n in notes),
                        "a disagreement must be visible, not resolved in silence")

    def test_no_ce_iam_is_a_note_not_a_crash(self):
        old = reach.CE_IAM
        reach.CE_IAM = "/nonexistent/ce-iam-xyz"
        try:
            bound, note = reach.name_bindings()
        finally:
            reach.CE_IAM = old
        self.assertEqual(bound, {})
        self.assertIn("PATH", note.text)

    def test_garbage_lines_are_ignored_not_guessed_at(self):
        with FakeCeIam(["(no name bindings held; `ce-iam name bind <name> <node>` to add one)",
                        "CAPS-NAME -> " + "aa" * 32,
                        "short -> abc"]):
            bound, _ = reach.name_bindings()
        self.assertEqual(bound, {}, "only a lowercase name and a 64-hex node id is a binding")


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

    def __init__(self, tunnels=None, token="tok", netgraph=None):
        self.tunnels = tunnels
        self.token = token
        self.netgraph = netgraph
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
                if self.path == "/netgraph":
                    if outer.netgraph is None:
                        return self._send(404, {"error": "no such route"})
                    return self._send(200, outer.netgraph)
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


class FakeWallet:
    """A stand-in `ce-iam wallet`, so the policy check can be run against grants we choose."""

    def __init__(self, grants):
        self.grants = grants      # {alias: scope dict}

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        for alias, g in self.grants.items():
            with open(os.path.join(self.dir.name, alias + ".json"), "w") as f:
                json.dump(g, f)
        p = os.path.join(self.dir.name, "ce-iam")
        listing = "".join("echo '%s'\n" % a for a in self.grants)
        with open(p, "w") as f:
            f.write("#!/bin/sh\n"
                    'if [ "$2" = "list" ]; then\n' + listing + "exit 0\nfi\n"
                    'if [ "$2" = "show" ]; then cat "' + self.dir.name + '/$3.json"; exit 0; fi\n'
                    "exit 1\n")
        os.chmod(p, 0o755)
        self.old = reach.CE_IAM
        reach.CE_IAM = p
        return self

    def __exit__(self, *a):
        reach.CE_IAM = self.old
        self.dir.cleanup()


class TempPolicy:
    def __init__(self, doc):
        self.doc = doc

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "policy.json")
        with open(self.path, "w") as f:
            json.dump(self.doc, f)
        self.old = (reach.LOCAL_POLICY, reach.CE_DEV_POLICY)
        reach.LOCAL_POLICY = self.path
        reach.CE_DEV_POLICY = os.path.join(self.dir.name, "absent.json")
        return self

    def __exit__(self, *a):
        reach.LOCAL_POLICY, reach.CE_DEV_POLICY = self.old
        self.dir.cleanup()


def grant(alias, node, abilities, ports=None, not_after=0, nonce=7):
    link = {"abilities": abilities, "issuer": node, "nonce": nonce,
            "not_after": not_after, "resource": node}
    if ports is not None:
        link["allowed_ports"] = ports
    return {"alias": alias, "node_id": node,
            "scope": {"abilities": abilities, "resource": node, "not_after": not_after,
                      "root_issuer": node, "links": [link]}}


class Policy(unittest.TestCase):
    """A capability nobody looks at is the one that stays too wide. These check that the
    looking is real: a wider grant must fail, and a missing field must not read as a limit."""

    NODE = "75" * 32
    TABLE = {"devices": {"huey": {"what": "the duck", "wallet": "huey", "node_id": NODE,
                                  "ports": {"servo": 8938, "walk": 8940}}}}
    POL = {"version": 1, "defaults": {"max_days": 90, "warn_days": 14},
           "devices": {"huey": {"hold": {"abilities": ["tunnel"], "ports": [8938, 8940]}}}}

    def report(self, grants, policy=None, table=None):
        with TempTable(table or self.TABLE, names=False), \
                TempPolicy(policy or self.POL), FakeWallet(grants):
            return reach.policy_report()

    def test_a_grant_that_matches_the_policy_passes(self):
        soon = int(time.time()) + 60 * 86400
        rows, detail = self.report({"huey": grant("huey", self.NODE, ["tunnel"],
                                                  ports=[8938, 8940], not_after=soon)})
        self.assertEqual(detail, "")
        self.assertEqual(rows[0]["verdict"], reach.PASS_V, rows[0]["findings"])
        self.assertIsNone(rows[0]["fix"], "nothing to fix means no command to run")

    def test_a_grant_with_more_abilities_than_the_job_fails(self):
        soon = int(time.time()) + 60 * 86400
        rows, _ = self.report({"huey": grant("huey", self.NODE,
                                             ["tunnel", "exec", "delete"],
                                             ports=[8938, 8940], not_after=soon)})
        self.assertEqual(rows[0]["verdict"], reach.FAIL_V)
        joined = " ".join(rows[0]["findings"])
        self.assertIn("delete", joined)
        self.assertIn("exec", joined)

    def test_no_port_restriction_is_every_port_not_no_port(self):
        """The field is absent on a grant that restricts nothing. Reading absent as 'fine'
        is how a tunnel grant for two ports quietly becomes one for all 65535."""
        soon = int(time.time()) + 60 * 86400
        rows, _ = self.report({"huey": grant("huey", self.NODE, ["tunnel"], not_after=soon)})
        self.assertEqual(rows[0]["verdict"], reach.FAIL_V)
        self.assertIn("ANY port", " ".join(rows[0]["findings"]))

    def test_a_grant_that_never_expires_fails_and_the_fix_sets_an_expiry(self):
        rows, _ = self.report({"huey": grant("huey", self.NODE, ["tunnel"],
                                             ports=[8938, 8940], not_after=0)})
        self.assertEqual(rows[0]["verdict"], reach.FAIL_V)
        self.assertIn("never expires", " ".join(rows[0]["findings"]))
        self.assertIn("--expires-in 7776000", rows[0]["fix"])
        self.assertIn("--allowed-port 8938", rows[0]["fix"])

    def test_a_grant_about_to_expire_is_named_with_the_days_left(self):
        rows, _ = self.report({"huey": grant("huey", self.NODE, ["tunnel"], ports=[8938, 8940],
                                             not_after=int(time.time()) + 3 * 86400)})
        self.assertEqual(rows[0]["verdict"], reach.FAIL_V)
        self.assertIn("expires in 3", " ".join(rows[0]["findings"]))

    def test_several_grants_on_one_device_are_each_judged(self):
        """One grant covers the job and another is far too wide. Passing on the first and
        never looking at the second is the failure this guards."""
        soon = int(time.time()) + 60 * 86400
        rows, _ = self.report({
            "huey": grant("huey", self.NODE, ["tunnel"], ports=[8938, 8940], not_after=soon),
            "home-control": grant("home-control", self.NODE, ["home:door"], not_after=soon),
        })
        self.assertEqual(rows[0]["verdict"], reach.FAIL_V)
        self.assertIn("home-control", " ".join(rows[0]["findings"]))

    def test_a_device_with_no_grant_is_a_fail_with_the_command_that_fixes_it(self):
        rows, _ = self.report({})
        self.assertEqual(rows[0]["verdict"], reach.FAIL_V)
        self.assertIn("no grant held", rows[0]["findings"][0])
        self.assertIn("--action tunnel", rows[0]["fix"])

    def test_grants_outside_the_policy_are_listed_not_judged(self):
        soon = int(time.time()) + 60 * 86400
        rows, _ = self.report({
            "huey": grant("huey", self.NODE, ["tunnel"], ports=[8938, 8940], not_after=soon),
            "media": grant("media", "ab" * 32, ["media:get"], not_after=soon),
        })
        last = rows[-1]
        self.assertIsNone(last["device"])
        self.assertEqual(last["verdict"], reach.UNKNOWN_V)
        self.assertIn("media", " ".join(last["findings"]))

    def test_no_policy_file_is_cannot_determine_with_the_path(self):
        with TempPolicy({"devices": {}}) as tp:
            os.remove(tp.path)
            rows, detail = reach.policy_report()
        self.assertEqual(rows, [])
        self.assertIn("no grant policy", detail)


class Paths(unittest.TestCase):
    """A measurement without its path is half an answer, and the half it is missing is the
    one that explains the number."""

    # This machine's own node, printed by `ce status`: the node id and the peer id it derives
    # to. If the derivation ever drifts, this pair catches it without a network.
    NODE = "0e95015b2d034b3c973e1fe22e2e811067c409daa24e01ac8d9eadb1543ed493"
    PEER = "12D3KooWAoHf6V77YJQ6bo21BVT5YwSwigkcR8hG2DUjfq6DqVht"

    def test_a_node_id_derives_its_peer_id_with_no_lookup(self):
        self.assertEqual(reach.peer_id_for(self.NODE), self.PEER)

    def test_something_that_is_not_a_node_id_derives_nothing(self):
        for bad in ("", "beef", "zz" * 32, None, "ab" * 31):
            self.assertIsNone(reach.peer_id_for(bad), "%r is not a node id" % (bad,))

    def test_an_old_node_is_reported_as_unable_to_say_not_as_no_relays(self):
        """The failure mode this guards: reading a missing field as False and publishing
        'every path is direct' off a node that was never asked."""
        old = [{"peer": self.PEER, "rtt_ms": 128.0, "samples": 9, "last_seen_secs": 1}]
        with FakeNode(netgraph=old):
            rows, supported, detail = reach.net_paths()
        self.assertIs(supported, False)
        self.assertIn("netgraph-path", detail)
        self.assertIsNone(rows[0]["relayed"], "unknown, never False")

    def test_a_new_node_reports_the_path_and_names_the_device(self):
        tbl = {"devices": {"mine": {"what": "this machine", "wallet": "w",
                                    "node_id": self.NODE, "ports": {"api": 1}}}}
        rows = [{"peer": self.PEER, "rtt_ms": 0.9, "samples": 120, "last_seen_secs": 1,
                 "addr": "/ip4/192.168.1.9/tcp/4001", "relayed": False},
                {"peer": "12D3KooWHkCw1zQX67N444WUVQokFy5hPVougDGKRJsMxoyoBYMS",
                 "rtt_ms": 79.3, "samples": 6157, "last_seen_secs": 1,
                 "addr": "/ip4/1.2.3.4/tcp/4001/p2p/QmR/p2p-circuit/p2p/QmT", "relayed": True}]
        with TempTable(tbl, names=False), FakeNode(netgraph=rows):
            out, supported, detail = reach.net_paths()
        self.assertIs(supported, True)
        self.assertEqual(detail, "")
        self.assertEqual(out[0]["device"], "mine", "a peer id maps back to the device name")
        self.assertIs(out[0]["relayed"], False)
        self.assertIs(out[1]["relayed"], True)
        self.assertIsNone(out[1]["device"], "an unknown peer is not guessed at")

    def test_a_node_with_no_peers_is_cannot_determine_not_a_clean_pass(self):
        with FakeNode(netgraph=[]):
            rows, supported, detail = reach.net_paths()
        self.assertEqual(rows, [])
        self.assertIsNone(supported)
        self.assertIn("no connected peers", detail)

    def test_a_node_that_does_not_answer_is_cannot_determine(self):
        with FakeNode(netgraph=None):   # /netgraph 404s
            rows, supported, detail = reach.net_paths()
        self.assertIsNone(supported)
        self.assertTrue(detail)


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
