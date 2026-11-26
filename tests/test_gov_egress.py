"""Offline checks for the 政府 API 專用出口代理 (allowlist, refusals, redirects, address pinning, log hygiene).

Run: python -I tests/test_gov_egress.py   (from the repository root)
No network: upstream calls are replaced by fakes. Real TDX and reverse tests run on the server.
"""
import http.client
import io
import logging
import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "apps" / "fleet_bridge" / "egress"))
import gov_egress as g  # noqa: E402

FREEWAY = "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/Freeway"
TOKEN = "/tdx/auth/realms/TDXConnect/protocol/openid-connect/token"


class TargetTests(unittest.TestCase):
    def ok(self, target):
        return g.parse_target(target)

    def refused(self, target, status):
        with self.assertRaises(g.Refused) as ctx:
            g.parse_target(target)
        self.assertEqual(ctx.exception.status, status, target)

    def test_allowed_paths(self):
        for path in (FREEWAY, "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/Highway",
                     "/tdx/api/basic/v1/Traffic/RoadEvent/Event/Highway",
                     "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/City/Taoyuan",
                     "/tdx/api/basic/v1/Traffic/RoadEvent/Event/City/NewTaipei"):
            source, route, upstream, pairs = self.ok(path + "?$format=JSON&$top=500&$skip=0&$count=true")
            self.assertEqual(source, "tdx")
            g.allowed_method(route, "GET", upstream)
        _s, route, upstream, _p = self.ok(TOKEN)
        g.allowed_method(route, "POST", upstream)

    def test_wrong_method_on_allowed_path(self):
        _s, route, upstream, _p = self.ok(FREEWAY)
        for method in ("POST", "PUT", "DELETE"):
            with self.assertRaises(g.Refused) as ctx:
                g.allowed_method(route, method, upstream)
            self.assertEqual(ctx.exception.status, 405)
        _s, route, upstream, _p = self.ok(TOKEN)
        with self.assertRaises(g.Refused):
            g.allowed_method(route, "GET", upstream)

    def test_denied_targets(self):
        for target in ("http://169.254.169.254/latest/meta-data/", "https://tdx.transportdata.tw/api/basic/v1/x",
                       "//evil.example/x", "tdx.transportdata.tw:443", "*"):
            self.refused(target, 400)
        for target in ("/other/api/basic/v1/Traffic/RoadEvent/LiveEvent/Freeway", "/tdx/api/basic/v2/Bus/Route",
                       "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/Freeway/../../../../admin",
                       "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/%2e%2e/x",
                       "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent//Freeway",
                       "/tdx/api/basic/v1/Traffic/RoadEvent/Authority", "/TDX" + FREEWAY[4:],
                       "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/City/Taoyuan@evil.example",
                       "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/City/127.0.0.1",
                       "/tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/City/NotACity",
                       "/tdx/api/basic/v1/Traffic/RoadEvent/Event/City/Kaohsiung"):
            with self.assertRaises(g.Refused, msg=target) as ctx:
                _s, route, upstream, _p = g.parse_target(target)
                g.allowed_method(route, "GET", upstream)
            self.assertIn(ctx.exception.status, (400, 403), target)

    def test_query_rules(self):
        self.refused(FREEWAY + "?url=http://evil", 403)
        self.refused(FREEWAY + "?$top=1&$top=2", 400)
        self.refused(FREEWAY + "?$filter=a%0Db", 400)
        self.refused(FREEWAY + "?" + "$filter=" + "x" * 3000, 414)
        _s, _r, _u, pairs = self.ok(FREEWAY + "?$filter=Road%20eq%20'國道1號'&$format=JSON")
        self.assertEqual(dict(pairs)["$filter"], "Road eq '國道1號'")


class AddressTests(unittest.TestCase):
    def test_public_ip(self):
        for bad in ("127.0.0.1", "10.1.2.3", "172.19.0.2", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                    "0.0.0.0", "::1", "fe80::1", "fd00::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "224.0.0.1",
                    "2002:0a00:0001::1", "192.88.99.2", "168.63.129.16", "100.100.100.200"):
            self.assertIsNone(g.public_ip(bad), bad)
        for good in ("8.8.8.8", "203.66.1.1", "2001:4860:4860::8888"):
            self.assertIsNotNone(g.public_ip(good), good)

    def test_dns_rebinding_any_private_answer_refuses(self):
        real = socket.getaddrinfo
        try:
            socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("203.66.1.1", 443)),
                                                  (socket.AF_INET, 1, 6, "", ("127.0.0.1", 443))]
            with self.assertRaises(g.Refused) as ctx:
                g.resolve_public("tdx.transportdata.tw")
            self.assertEqual(ctx.exception.code, "upstream_address_blocked")
            socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("203.66.1.1", 443))]
            self.assertEqual(g.resolve_public("tdx.transportdata.tw"), [(socket.AF_INET, ("203.66.1.1", 443))])
        finally:
            socket.getaddrinfo = real


class FakeResponse:
    def __init__(self, status, body=b"{}", headers=None):
        self.status, self._body, self._headers = status, body, headers or {"Content-Type": "application/json"}
        self._offset = 0

    def read(self, n):
        chunk = self._body[self._offset:self._offset + n]
        self._offset += len(chunk)
        return chunk

    def getheader(self, name):
        return self._headers.get(name)


class FakeConnection:
    instances = []
    response = FakeResponse(200)

    def __init__(self, host, family, sockaddr, context):
        self.host, self.sockaddr, self.sent = host, sockaddr, []
        self.context = context
        FakeConnection.instances.append(self)

    def putrequest(self, method, target, skip_host, skip_accept_encoding):
        self.sent.append(("REQ", method, target))

    def putheader(self, name, value):
        self.sent.append((name, value))

    def endheaders(self, body=None):
        self.body = body

    def getresponse(self):
        r = FakeConnection.response
        if isinstance(r, Exception):
            raise r
        return FakeResponse(r.status, r._body, r._headers)

    def close(self):
        pass


class ForwardTests(unittest.TestCase):
    def setUp(self):
        self.real = socket.getaddrinfo
        socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("203.66.1.1", 443))]
        FakeConnection.instances = []

    def tearDown(self):
        socket.getaddrinfo = self.real

    def test_fixed_host_and_verified_tls(self):
        FakeConnection.response = FakeResponse(200, b'{"Items":[]}')
        _s, route, upstream, pairs = g.parse_target(FREEWAY + "?$format=JSON&$top=500")
        status, headers, body = g.forward(route, "GET", upstream, pairs, [("Authorization", "Bearer abc.def")], None,
                                          connection_factory=FakeConnection)
        conn = FakeConnection.instances[0]
        self.assertEqual((status, body), (200, b'{"Items":[]}'))
        self.assertEqual(conn.host, "tdx.transportdata.tw")
        self.assertEqual(conn.sockaddr, ("203.66.1.1", 443))
        self.assertIn(("Host", "tdx.transportdata.tw"), conn.sent)
        self.assertTrue(conn.sent[0][2].startswith("/api/basic/v1/Traffic/RoadEvent/LiveEvent/Freeway?"))
        self.assertTrue(conn.context.check_hostname)
        self.assertEqual(conn.context.verify_mode, g.ssl.CERT_REQUIRED)

    def test_redirect_is_blocked(self):
        for status in (301, 302, 307, 308):
            FakeConnection.response = FakeResponse(status, b"", {"Location": "http://169.254.169.254/"})
            _s, route, upstream, pairs = g.parse_target(FREEWAY)
            with self.assertRaises(g.Refused) as ctx:
                g.forward(route, "GET", upstream, pairs, [], None, connection_factory=FakeConnection)
            self.assertEqual(ctx.exception.code, "upstream_redirect_blocked")
            self.assertEqual(ctx.exception.upstream_bytes, 0)  # provider answered: the ERP still counts the call

    def test_no_resend_after_request_was_sent(self):
        socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("203.66.1.1", 443)),
                                              (socket.AF_INET, 1, 6, "", ("203.66.1.2", 443))]
        FakeConnection.response = ConnectionResetError("reset after send")
        _s, route, upstream, pairs = g.parse_target(FREEWAY)
        with self.assertRaises(g.Refused):
            g.forward(route, "GET", upstream, pairs, [], None, connection_factory=FakeConnection)
        self.assertEqual(len(FakeConnection.instances), 1)

    def test_dns_deadline(self):
        import time as _time
        socket.getaddrinfo = lambda *a, **k: _time.sleep(2)
        with self.assertRaises(g.Refused) as ctx:
            g.resolve_public("tdx.transportdata.tw", timeout=0.2)
        self.assertEqual(ctx.exception.code, "upstream_dns_timeout")

    def test_oversized_response(self):
        FakeConnection.response = FakeResponse(200, b"x" * (g.MAX_RESPONSE_BYTES + 5))
        _s, route, upstream, pairs = g.parse_target(FREEWAY)
        with self.assertRaises(g.Refused) as ctx:
            g.forward(route, "GET", upstream, pairs, [], None, connection_factory=FakeConnection)
        self.assertEqual(ctx.exception.code, "upstream_response_too_large")


class ServerTests(unittest.TestCase):
    """End-to-end over a real socket with upstream faked; also proves secrets never reach the log."""

    @classmethod
    def setUpClass(cls):
        cls.log = io.StringIO()
        handler = logging.StreamHandler(cls.log)
        logging.getLogger("gov-egress").addHandler(handler)
        logging.getLogger("gov-egress").setLevel(logging.INFO)
        cls.real_forward = g.forward
        cls.seen = []

        def fake_forward(route, method, upstream, pairs, headers, body, connection_factory=None):
            cls.seen.append((method, upstream, pairs, headers, body))
            return 200, [("Content-Type", "application/json")], b'{"access_token":"TOKEN-VALUE-XYZ"}'
        g.forward = fake_forward
        cls.server = g.Server(("127.0.0.1", 0))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        g.forward = cls.real_forward

    def request(self, method, target, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, target, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data, response

    def raw(self, request_bytes):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(request_bytes)
            return sock.recv(4096).decode("latin-1").split("\r\n", 1)[0]

    def test_health(self):
        self.assertEqual(self.request("GET", "/healthz")[0], 200)

    def test_token_and_data_flow(self):
        secret = "CLIENT-SECRET-VALUE-123"
        status, data, response = self.request("POST", TOKEN, body=f"grant_type=client_credentials&client_id=a&client_secret={secret}",
                                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader("X-Egress-Upstream-Bytes"), str(len(data)))
        status, _d, _r = self.request("GET", FREEWAY + "?$format=JSON", headers={"Authorization": "Bearer TOKEN-VALUE-XYZ",
                                                                             "Host": "169.254.169.254", "Cookie": "a=b"})
        self.assertEqual(status, 200)
        method, upstream, pairs, headers, body = self.seen[-1]
        names = {n for n, _ in headers}
        self.assertNotIn("Cookie", names)
        self.assertNotIn("Host", names)
        log_text = self.log.getvalue()
        self.assertNotIn(secret, log_text)
        self.assertNotIn("TOKEN-VALUE-XYZ", log_text)
        self.assertNotIn("$format=JSON", log_text)

    def test_refused_paths_never_logged(self):
        marker = "FIXTURE-PATH-MARKER"
        self.assertEqual(self.request("GET", f"/tdx/not-allowed/{marker}")[0], 403)
        self.assertEqual(self.request("GET", f"/tdx/api/basic/v1/Traffic/RoadEvent/Authority?{marker}=1")[0], 403)
        self.assertNotIn(marker, self.log.getvalue())

    def test_busy_reply_before_thread(self):
        taken = [self.server.slots.acquire(blocking=False) for _ in range(g.MAX_CONCURRENCY)]
        try:
            self.assertTrue(all(taken))
            self.assertIn(" 503 ", self.raw(b"GET /healthz HTTP/1.1\r\n\r\n"))
        finally:
            for _ in taken:
                self.server.slots.release()
        self.assertEqual(self.request("GET", "/healthz")[0], 200)

    def test_refusals(self):
        self.assertEqual(self.request("GET", "/tdx/api/basic/v2/Rail/TRA/Station")[0], 403)
        self.assertEqual(self.request("POST", FREEWAY, body="x", headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 405)
        self.assertEqual(self.request("POST", TOKEN, body="x", headers={"Content-Type": "application/json"})[0], 415)
        self.assertEqual(self.request("POST", TOKEN, body="x" * 5000, headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 413)
        self.assertEqual(self.request("GET", FREEWAY, headers={"Authorization": "Basic dXNlcjpwYXNz"})[0], 400)
        self.assertEqual(self.request("DELETE", FREEWAY)[0], 405)
        self.assertIn(" 405 ", self.raw(b"CONNECT tdx.transportdata.tw:443 HTTP/1.1\r\nHost: tdx.transportdata.tw\r\n\r\n"))
        self.assertIn(" 400 ", self.raw(b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: x\r\n\r\n"))
        self.assertIn(" 400 ", self.raw(b"GET //tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/Freeway HTTP/1.1\r\n\r\n"))
        self.assertIn(" 403 ", self.raw(b"GET /tdx/api/basic/v1/Traffic/RoadEvent/LiveEvent/%2e%2e/%2e%2e/x HTTP/1.1\r\n\r\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
