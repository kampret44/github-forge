"""Custom raw-socket threaded HTTP/1.1 server (stdlib only, no http.server).

Why custom: http.server's per-request overhead (BaseHTTPRequestHandler parsing,
error handling, logging) is too slow for the read workload budget (1.38 ms/req).
This parser does the minimum: request line + headers + Content-Length body,
keep-alive, and immediate `100 Continue` for Expect (curl/git send it for bodies
> 1 KiB; without it every large push stalls ~1 s).
"""
import socket
import threading

_REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request",
    401: "Unauthorized", 404: "Not Found", 405: "Method Not Allowed",
    409: "Conflict", 422: "Unprocessable Entity", 500: "Internal Server Error",
}


class Server:
    def __init__(self, handler, host="127.0.0.1", port=0):
        self._handler = handler
        self._host = host
        self._port = port
        self._sock = None
        self._accept_thread = None
        self._stopped = threading.Event()
        self.port = 0

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, self._port))
        sock.listen(256)
        sock.settimeout(0.5)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self.port

    def stop(self) -> None:
        self._stopped.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            t = threading.Thread(target=self._conn_loop, args=(conn,), daemon=True)
            t.start()

    def _conn_loop(self, conn: socket.socket) -> None:
        buf = bytearray()
        try:
            conn.settimeout(30)
            while not self._stopped.is_set():
                req = self._read_request(conn, buf)
                if req is None:
                    return
                method, raw_path, headers, body = req
                try:
                    status, resp_headers, resp_body = self._handler(method, raw_path, headers, body)
                except Exception:
                    status, resp_headers, resp_body = 500, [("content-type", "application/json")], b'{"message":"Internal Server Error"}'
                keep_alive = self._keep_alive(headers)
                self._send_response(conn, status, resp_headers, resp_body, keep_alive)
                if not keep_alive:
                    return
        except (OSError, ValueError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _keep_alive(headers: dict) -> bool:
        conn = headers.get("connection", "").lower()
        if "close" in conn:
            return False
        return True

    def _read_request(self, conn: socket.socket, buf: bytearray):
        # Read until end of headers.
        while True:
            idx = buf.find(b"\r\n\r\n")
            if idx >= 0:
                break
            chunk = conn.recv(65536)
            if not chunk:
                return None
            buf += chunk
        head, rest = bytes(buf[:idx]), bytes(buf[idx + 4:])
        del buf[:]
        lines = head.split(b"\r\n")
        try:
            request_line = lines[0].decode("latin-1")
            method, raw_path, _version = request_line.split(" ", 2)
        except ValueError:
            return None
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
        if headers.get("expect", "").lower() == "100-continue":
            try:
                conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            except OSError:
                return None
        body = rest
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = self._read_chunked(conn, body)
            if body is None:
                return None
        else:
            try:
                length = int(headers.get("content-length", "0") or "0")
            except ValueError:
                length = 0
            while len(body) < length:
                chunk = conn.recv(min(262144, length - len(body)))
                if not chunk:
                    return None
                body += chunk
            body = body[:length]
        return method, raw_path, headers, body

    @staticmethod
    def _read_chunked(conn: socket.socket, initial: bytes):
        buf = bytearray(initial)
        out = bytearray()
        while True:
            while b"\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return None
                buf += chunk
            line_end = buf.find(b"\r\n")
            line = bytes(buf[:line_end])
            del buf[:line_end + 2]
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                return None
            if size == 0:
                # trailer + final CRLF; consume what is there, ignore the rest
                return bytes(out)
            while len(buf) < size + 2:
                chunk = conn.recv(65536)
                if not chunk:
                    return None
                buf += chunk
            out += buf[:size]
            del buf[:size + 2]

    @staticmethod
    def _send_response(conn: socket.socket, status: int, headers, body: bytes, keep_alive: bool) -> None:
        reason = _REASONS.get(status, "OK")
        out = bytearray()
        out += ("HTTP/1.1 %d %s\r\n" % (status, reason)).encode("latin-1")
        seen_ct = False
        for k, v in headers:
            if k.lower() == "content-type":
                seen_ct = True
            out += ("%s: %s\r\n" % (k, v)).encode("latin-1")
        if not seen_ct:
            out += b"content-type: application/json\r\n"
        out += ("content-length: %d\r\n" % len(body)).encode("latin-1")
        out += b"connection: keep-alive\r\n" if keep_alive else b"connection: close\r\n"
        out += b"\r\n"
        out += body
        conn.sendall(bytes(out))
