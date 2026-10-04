"""The name proxy: `huey:8940` is a real address for a proxy-aware client.

No mesh is needed to test it. The device in these tests declares `port_local`, so its
"forward" is a port that is already listening locally — which is exactly the state
`open_forward` reports as `already`, and the only state the proxy cares about. The mesh leg
is measured separately in `test_reach.Live`.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, HERE)

import proxy  # noqa: E402
import reach  # noqa: E402

MARKER = b'{"app":"test-upstream","ok":true}'


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = MARKER if self.path == "/api/health" else b'{"error":"no such path"}'
        code = 200 if self.path == "/api/health" else 404
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.up_port = cls.up.server_address[1]
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()

        cls.dir = tempfile.TemporaryDirectory()
        table = os.path.join(cls.dir.name, "devices.json")
        with open(table, "w") as f:
            json.dump({"devices": {"widget": {
                "what": "an upstream that is already listening locally",
                "wallet": "widget", "node_id": "ab" * 32,
                "ports": {"api": cls.up_port},
                "port_local": {str(cls.up_port): cls.up_port},
            }}}, f)
        cls.old = (reach.CE_DEV_TABLE, reach.LOCAL_TABLE, reach.CE_IAM)
        reach.CE_DEV_TABLE = table
        reach.LOCAL_TABLE = os.path.join(cls.dir.name, "absent.json")
        reach.CE_IAM = "/nonexistent/ce-iam-in-proxy-tests"  # table only, deterministic

        cls.srv, _ = proxy.serve_in_thread(0)
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.up.shutdown()
        cls.up.server_close()
        reach.CE_DEV_TABLE, reach.LOCAL_TABLE, reach.CE_IAM = cls.old
        cls.dir.cleanup()

    def speak(self, request, read=4096):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(request)
            out = b""
            while len(out) < read:
                d = s.recv(4096)
                if not d:
                    break
                out += d
            return out
        finally:
            s.close()

    def test_a_device_name_and_its_own_port_number_reach_it(self):
        r = self.speak(b"GET http://widget:%d/api/health HTTP/1.1\r\nHost: widget\r\n\r\n"
                       % self.up_port)
        self.assertIn(b"200", r.split(b"\r\n")[0])
        self.assertIn(MARKER, r)

    def test_a_resolver_style_suffix_is_the_same_device(self):
        for host in (b"widget.ce", b"widget.mesh", b"WIDGET"):
            r = self.speak(b"GET http://%s:%d/api/health HTTP/1.1\r\n\r\n" % (host, self.up_port))
            self.assertIn(MARKER, r, "%s should be the same device" % host)

    def test_connect_tunnels_raw_bytes(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(b"CONNECT widget:%d HTTP/1.1\r\n\r\n" % self.up_port)
            head = s.recv(200)
            self.assertIn(b"200", head, head)
            s.sendall(b"GET /api/health HTTP/1.1\r\nHost: widget\r\nConnection: close\r\n\r\n")
            body = b""
            while True:
                d = s.recv(4096)
                if not d:
                    break
                body += d
            self.assertIn(MARKER, body)
        finally:
            s.close()

    def test_an_unknown_device_is_refused_by_name_with_the_known_ones(self):
        r = self.speak(b"GET http://nope:80/ HTTP/1.1\r\n\r\n")
        self.assertIn(b"502", r.split(b"\r\n")[0])
        self.assertIn(b"no device named 'nope'", r)
        self.assertIn(b"widget", r, "the refusal lists what it does know")

    def test_a_port_the_device_does_not_answer_on_is_a_named_502(self):
        r = self.speak(b"GET http://widget:9/ HTTP/1.1\r\n\r\n")
        self.assertIn(b"502", r.split(b"\r\n")[0])
        self.assertIn(b"widget", r)

    def test_an_origin_form_request_is_refused_with_the_reason(self):
        """A browser pointed here by mistake gets told what this is, not a hang."""
        r = self.speak(b"GET /api/health HTTP/1.1\r\nHost: widget\r\n\r\n")
        self.assertIn(b"400", r.split(b"\r\n")[0])
        self.assertIn(b"absolute URI", r)

    def test_healthz_is_the_one_origin_form_path_and_names_this_app(self):
        r = self.speak(b"GET /healthz HTTP/1.1\r\n\r\n")
        self.assertIn(b"200", r.split(b"\r\n")[0])
        self.assertIn(b"net.device-reach proxy", r)

    def test_the_negative_control_path_is_not_a_200(self):
        """A probe that cannot go red is a decoration: /healthz must not be a catch-all."""
        r = self.speak(b"GET /definitely-not-a-route-xyz HTTP/1.1\r\n\r\n")
        self.assertNotIn(b"200", r.split(b"\r\n")[0])
        self.assertIn(b"400", r.split(b"\r\n")[0])
        self.assertNotIn(b"net.device-reach proxy", r)

    def test_garbage_does_not_crash_the_proxy(self):
        self.speak(b"\x00\x01 not http at all\r\n\r\n")
        # Still serving afterwards: the real assertion.
        r = self.speak(b"GET http://widget:%d/api/health HTTP/1.1\r\n\r\n" % self.up_port)
        self.assertIn(MARKER, r)


if __name__ == "__main__":
    unittest.main()
