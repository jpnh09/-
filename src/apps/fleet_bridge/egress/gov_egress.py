#!/usr/bin/env python3
"""政府 API 專用出口代理 (government open-data egress gateway).

Runs in its own container. It shares one internal Docker network with the ERP
queue worker and has one outbound bridge network. It is NOT a generic proxy:

* The request target is "/<source>/<upstream path>"; each source has one fixed
  upstream host. Clients cannot choose a host, scheme or port.
* Only (method, path regex) pairs listed in ROUTES pass. Default deny.
* No CONNECT, no absolute-form targets, no percent-encoded or dot segments.
* Redirects are never followed; an upstream 3xx becomes 502.
* Every upstream address must be a public (global) IP. The socket connects to
  the checked address and TLS verifies the fixed hostname, which blocks DNS
  rebinding and SSRF towards the host, metadata or private services.
* Logs hold method, source, upstream path, status, size and duration only:
  never query values, headers, bodies, client secrets or access tokens.

A new source needs a code change, review and redeploy (owner approval).
Stdlib only, so it runs from the existing ERP image without new downloads.
"""
import http.client
import http.server
import ipaddress
import json
import logging
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse

LISTEN_PORT = 8080
MAX_RESPONSE_BYTES = 16 * 1024 * 1024   # 4 concurrent buffers stay well under the 256 MiB container limit
MAX_REQUEST_BODY = 4096
MAX_QUERY_LENGTH = 2048
MAX_QUERY_VALUE = 1024
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
DNS_TIMEOUT = 10
TOTAL_DEADLINE = 90   # seconds for DNS + connect + TLS + full response, per request
MAX_CONCURRENCY = 4
USER_AGENT = "fleet-gov-egress/1"

# Official {City} enums of the TDX 道路事件 v1 spec (LiveEvent has Kaohsiung, Event does not).
TDX_LIVE_CITIES = ("Taipei", "NewTaipei", "Taoyuan", "Taichung", "Tainan", "Kaohsiung", "Keelung", "MiaoliCounty",
                   "ChiayiCounty", "Chiayi", "PingtungCounty", "YilanCounty", "KinmenCounty")
TDX_PREVIEW_CITIES = tuple(c for c in TDX_LIVE_CITIES if c != "Kaohsiung")
# Public-but-special addresses an upstream name must never resolve to (6to4 relay anycast, cloud host endpoints).
DENY_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("192.88.99.0/24", "168.63.129.16/32", "100.100.100.200/32"))

# The only outbound destinations. Keep each rule as narrow as the integration needs.
ROUTES = {
    "tdx": {
        "host": "tdx.transportdata.tw",
        "rules": (
            ("POST", re.compile(r"/auth/realms/TDXConnect/protocol/openid-connect/token")),
            ("GET", re.compile(r"/api/basic/v1/Traffic/RoadEvent/(?:"
                               r"LiveEvent/(?:Freeway|Highway|City/(?:" + "|".join(TDX_LIVE_CITIES) + r"))"
                               r"|Event/(?:Highway|City/(?:" + "|".join(TDX_PREVIEW_CITIES) + r")))")),
            # Freeway main-line lane counts (VD static data) for the tractor-trailer lane rule; owner-approved
            # 2026-11-26. Only the full freeway list, refreshed monthly.
            ("GET", re.compile(r"/api/basic/v2/Road/Traffic/VD/Freeway")),
        ),
        "query_keys": frozenset({"$format", "$top", "$skip", "$count", "$select", "$filter", "$orderby"}),
        "post_content_type": "application/x-www-form-urlencoded",
    },
}

FORWARD_RESPONSE_HEADERS = ("Content-Type", "Content-Encoding", "Retry-After", "Last-Modified", "ETag")
TARGET_RE = re.compile(r"/([a-z][a-z0-9]{1,15})(/[A-Za-z0-9/_.-]{1,512})")

log = logging.getLogger("gov-egress")


class Refused(Exception):
    """upstream_bytes is set when the provider did answer but the gateway discarded the answer,
    so the ERP can still count the call against the provider quota."""

    def __init__(self, status, code, upstream_bytes=None):
        super().__init__(code)
        self.status, self.code, self.upstream_bytes = status, code, upstream_bytes


def public_ip(text):
    """Return the address if it is globally routable, else None."""
    ip = ipaddress.ip_address(text.split("%", 1)[0])
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo:
            return None
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private:
        return None
    if any(ip.version == net.version and ip in net for net in DENY_NETWORKS):
        return None
    return ip


def _getaddrinfo(host, port, timeout):
    """getaddrinfo has no timeout of its own; run it in a helper thread and stop waiting at the deadline."""
    box = {}

    def lookup():
        try:
            box["infos"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        except OSError as error:
            box["error"] = error
    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise Refused(504, "upstream_dns_timeout")
    if "error" in box:
        raise Refused(502, "upstream_dns_failed")
    return box["infos"]


def resolve_public(host, port=443, timeout=DNS_TIMEOUT):
    """All resolved addresses must be public; one private answer refuses the whole request."""
    infos = _getaddrinfo(host, port, timeout)
    addresses = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if public_ip(sockaddr[0]) is None:
            raise Refused(502, "upstream_address_blocked")
        if (family, sockaddr) not in addresses:
            addresses.append((family, sockaddr))
    if not addresses:
        raise Refused(502, "upstream_dns_failed")
    return addresses


def parse_target(target):
    """Split "/<source>/<path>?<query>" and return (source, route, upstream_path, query_pairs)."""
    if not target.startswith("/") or target.startswith("//") or "#" in target:
        raise Refused(400, "bad_target")
    path, _, query = target.partition("?")
    match = TARGET_RE.fullmatch(path)
    if not match:
        raise Refused(403, "path_not_allowed")
    source, upstream_path = match.groups()
    if "//" in upstream_path or any(seg in (".", "..") for seg in upstream_path.split("/")):
        raise Refused(403, "path_not_allowed")
    route = ROUTES.get(source)
    if route is None:
        raise Refused(403, "source_not_allowed")
    if len(query) > MAX_QUERY_LENGTH:
        raise Refused(414, "query_too_long")
    pairs = []
    if query:
        try:
            pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True, errors="strict")
        except (ValueError, UnicodeDecodeError):
            raise Refused(400, "bad_query")
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise Refused(400, "duplicate_query_key")
        for key, value in pairs:
            if key not in route["query_keys"]:
                raise Refused(403, "query_key_not_allowed")
            if len(value) > MAX_QUERY_VALUE or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                raise Refused(400, "bad_query_value")
    return source, route, upstream_path, pairs


def allowed_method(route, method, upstream_path):
    path_ok = False
    for rule_method, pattern in route["rules"]:
        if pattern.fullmatch(upstream_path):
            path_ok = True
            if rule_method == method:
                return True
    raise Refused(405 if path_ok else 403, "method_not_allowed" if path_ok else "path_not_allowed")


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to an already-checked IP while verifying the certificate for the fixed hostname."""

    def __init__(self, host, family, sockaddr, context):
        super().__init__(host, 443, timeout=READ_TIMEOUT, context=context)
        self._pinned = (family, sockaddr)

    def connect(self):
        family, sockaddr = self._pinned
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(sockaddr)
            sock.settimeout(READ_TIMEOUT)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def tls_context():
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def forward(route, method, upstream_path, pairs, headers, body, connection_factory=None):
    """Send one request upstream; returns (status, response_headers, body_bytes)."""
    host = route["host"]
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote, safe="$,'()") if pairs else ""
    target = upstream_path + ("?" + query if query else "")
    context = tls_context()
    last_error = None
    deadline = time.monotonic() + TOTAL_DEADLINE
    for family, sockaddr in resolve_public(host):
        if time.monotonic() > deadline:
            raise Refused(504, "upstream_deadline")
        conn = (connection_factory or PinnedHTTPSConnection)(host, family, sockaddr, context)
        sent = False
        try:
            conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            conn.putheader("User-Agent", USER_AGENT)
            for name, value in headers:
                conn.putheader(name, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            sent = True
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise Refused(502, "upstream_redirect_blocked", upstream_bytes=0)
            chunks, total = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise Refused(504, "upstream_deadline", upstream_bytes=total)
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise Refused(502, "upstream_response_too_large", upstream_bytes=total)
                chunks.append(chunk)
            kept = [(name, response.getheader(name)) for name in FORWARD_RESPONSE_HEADERS if response.getheader(name)]
            return response.status, kept, b"".join(chunks)
        except Refused:
            raise
        except ssl.SSLCertVerificationError:
            raise Refused(502, "upstream_tls_verify_failed")
        except (socket.timeout, TimeoutError):
            last_error = Refused(504, "upstream_timeout")
        except (OSError, http.client.HTTPException):
            last_error = Refused(502, "upstream_connect_failed")
        finally:
            conn.close()
        if sent:
            break  # the request may have reached the provider; do not resend it to another address
    raise last_error or Refused(502, "upstream_connect_failed")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "gov-egress"
    sys_version = ""
    timeout = 30

    def log_request(self, code="-", size="-"):
        pass  # replaced by the sanitized line in _finish

    def log_message(self, format, *args):
        pass  # http.server would print the raw request line; never log it

    def _reply(self, status, headers=(), body=b""):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _refuse(self, status, code):
        self._reply(status, [("Content-Type", "application/json")], json.dumps({"error": code}).encode())

    def _finish(self, source, path, status, size, started):
        log.info("%s %s %s %s status=%s bytes=%s ms=%d", self.client_address[0], source or "-", self.command,
                 path or "-", status, size, (time.monotonic() - started) * 1000)

    def _handle(self):
        started = time.monotonic()
        source = path = None
        status, size = 500, 0
        try:
            # Validate the target exactly as received: http.server collapses a leading "//" in self.path.
            words = self.requestline.split()
            raw_target = words[1] if len(words) >= 2 else ""
            if self.command == "GET" and raw_target == "/healthz":
                status = 200
                self._reply(200, [("Content-Type", "application/json")], b'{"ok":true}')
                return
            source, route, candidate, pairs = parse_target(raw_target)
            allowed_method(route, self.command, candidate)
            path = candidate  # only allowlisted paths ever reach the log
            headers, body = self._request_parts(route, pairs)
            status, out_headers, data = forward(route, self.command, path, pairs, headers, body)
            size = len(data)
            self._reply(status, out_headers + [("X-Egress-Upstream-Bytes", str(size))], data)
        except Refused as refused:
            status = refused.status
            extra = [] if refused.upstream_bytes is None else [("X-Egress-Upstream-Discarded", str(refused.upstream_bytes))]
            self._reply(refused.status, [("Content-Type", "application/json")] + extra,
                        json.dumps({"error": refused.code}).encode())
        except Exception:
            status = 500
            log.exception("internal error")  # exception text holds no request data
            self._refuse(500, "internal_error")
        finally:
            self._finish(source, path, status, size, started)

    def _request_parts(self, route, pairs):
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding"):
            raise Refused(400, "chunked_not_allowed")
        headers = []
        encoding = (self.headers.get("Accept-Encoding") or "").lower()
        headers.append(("Accept-Encoding", "gzip" if "gzip" in encoding else "identity"))
        headers.append(("Accept", "application/json"))
        if self.command == "POST":
            if pairs:
                raise Refused(400, "query_not_allowed")
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != route["post_content_type"]:
                raise Refused(415, "content_type_not_allowed")
            if length is None or not length.isdigit() or int(length) > MAX_REQUEST_BODY:
                raise Refused(413, "body_too_large")
            body = self.rfile.read(int(length))
            headers.append(("Content-Type", route["post_content_type"]))
            return headers, body
        if length not in (None, "0"):
            raise Refused(400, "body_not_allowed")
        auth = self.headers.get("Authorization")
        if auth is not None:
            if not re.fullmatch(r"Bearer [A-Za-z0-9._~+/=-]{10,8192}", auth):
                raise Refused(400, "bad_authorization")
            headers.append(("Authorization", auth))
        return headers, None

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _deny(self):
        started = time.monotonic()
        self._refuse(405, "method_not_allowed")
        self._finish(None, None, 405, 0, started)

    do_CONNECT = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = _deny


BUSY_BODY = b'{"error":"busy"}'
BUSY_REPLY = (b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: "
              + str(len(BUSY_BODY)).encode() + b"\r\nConnection: close\r\n\r\n" + BUSY_BODY)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address):
        super().__init__(address, Handler)
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENCY)

    def process_request(self, request, client_address):
        """Take a slot before starting a thread, so connections cannot pile up unbounded threads."""
        if not self.slots.acquire(blocking=False):
            try:
                # Read what the client already sent; closing with unread data makes the OS send a reset
                # and the client never sees the 503.
                request.settimeout(0.5)
                request.recv(65536)
                request.sendall(BUSY_REPLY)
            except OSError:
                pass
            self.shutdown_request(request)
            log.info("%s - - - status=503 bytes=0 ms=0", client_address[0])
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(message)s")
    server = Server(("0.0.0.0", LISTEN_PORT))
    log.info("listening on %s; sources=%s", LISTEN_PORT, ",".join(sorted(ROUTES)))
    server.serve_forever()


if __name__ == "__main__":
    main()
