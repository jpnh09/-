"""Client for the 政府 API 專用出口代理. ERP containers have no internet; this is their only way out.

The gateway address is fixed here (not a setting) so nobody can point ERP traffic elsewhere.
Only sources and paths whitelisted in apps/fleet_bridge/egress/gov_egress.py will pass.
"""
import zlib

import requests

GATEWAY = "http://gov-egress:8080"
MAX_BODY = 32 * 1024 * 1024      # bytes on the wire
MAX_DECODED = 64 * 1024 * 1024   # bytes after decompression


class GatewayError(Exception):
    """Transport-level failure (gateway down, refused, timeout). Message never holds secrets."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.code, self.status = code, status


class Response:
    """from_upstream is False when the gateway itself answered (refusal or unreachable upstream)."""

    def __init__(self, status, headers, body, wire_bytes, from_upstream):
        self.status, self.headers, self.body = status, headers, body
        self.wire_bytes, self.from_upstream = wire_bytes, from_upstream
        discarded = headers.get("X-Egress-Upstream-Discarded")
        # The provider answered but the gateway refused to pass the answer on (redirect, too large, too slow).
        self.discarded_bytes = int(discarded) if discarded and discarded.isdigit() else None
        self.gateway_error = None
        if not from_upstream:
            try:
                self.gateway_error = str(self.json().get("error") or f"HTTP {status}")[:80]
            except (ValueError, AttributeError, UnicodeDecodeError):
                self.gateway_error = f"HTTP {status}"

    def json(self):
        import json
        return json.loads(self.body.decode("utf-8-sig"))


def _decode(raw, encoding, limit=None):
    """Decompress with a ceiling on the decoded size, so a tiny compressed body cannot expand without bound."""
    limit = limit or MAX_DECODED
    encoding = (encoding or "").lower().strip()
    if encoding in ("", "identity"):
        return raw
    if encoding not in ("gzip", "deflate"):
        raise GatewayError("unsupported_content_encoding")
    inflater = zlib.decompressobj(wbits=31 if encoding == "gzip" else 15)
    data = inflater.decompress(raw, limit + 1)
    if len(data) > limit or inflater.unconsumed_tail:
        raise GatewayError("decoded_body_too_large")
    if not inflater.eof:
        raise GatewayError("bad_compressed_body")
    return data


def call(source, method, path, *, params=None, data=None, headers=None, timeout=(10, 90)):
    """One request through the gateway. Returns Response with the on-the-wire byte count (for TDX point usage)."""
    if not path.startswith("/"):
        raise ValueError("path must start with /")
    session = requests.Session()
    session.trust_env = False  # ignore any proxy environment variables
    try:
        response = session.request(method, f"{GATEWAY}/{source}{path}", params=params, data=data,
                                   headers={"Accept-Encoding": "gzip", **(headers or {})},
                                   timeout=timeout, stream=True, allow_redirects=False)
        try:
            raw = response.raw.read(MAX_BODY + 1, decode_content=False)
        finally:
            response.close()
    except requests.Timeout:
        raise GatewayError("gateway_timeout")
    except requests.RequestException:
        raise GatewayError("gateway_unreachable")
    except Exception:  # urllib3 errors raised while reading the raw stream are not RequestExceptions
        raise GatewayError("gateway_read_failed")
    finally:
        session.close()
    if len(raw) > MAX_BODY:
        raise GatewayError("response_too_large", response.status_code)
    try:
        body = _decode(raw, response.headers.get("Content-Encoding"))
    except zlib.error:
        raise GatewayError("bad_compressed_body", response.status_code)
    wire = response.headers.get("X-Egress-Upstream-Bytes")
    from_upstream = wire is not None and wire.isdigit()
    return Response(response.status_code, response.headers, body, int(wire) if from_upstream else 0, from_upstream)
