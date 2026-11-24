"""Just enough of the Redis protocol for DMOBNK's apps (the branch kiosk and
the CRM), so their python:alpine images need no pip install: branch
clusters may have no internet beyond pulling images. Mounted from the
dmobnk-ingress-host ConfigMap at /opt/dmobnk, like ingress_host.py."""
import socket
import threading


class Redis:
    """Just enough of the Redis protocol for this app, so the image needs
    no pip install (branch clusters may have no internet beyond images)."""

    def __init__(self, host, port=6379):
        self.addr = (host, port)
        self.lock = threading.Lock()
        self.sock = None
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("redis closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _exact(self, n):
        while len(self.buf) < n + 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("redis closed the connection")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n + 2:]
        return data

    def _reply(self):
        line = self._line()
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode()
        if kind == b"-":
            raise RuntimeError(rest.decode())
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            return None if n < 0 else self._exact(n).decode()
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._reply() for _ in range(n)]
        raise ConnectionError("bad redis reply")

    def __call__(self, *args):
        out = b"*%d\r\n" % len(args)
        for a in args:
            a = str(a).encode()
            out += b"$%d\r\n%s\r\n" % (len(a), a)
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self.sock = socket.create_connection(self.addr, timeout=3)
                        self.buf = b""
                    self.sock.sendall(out)
                    return self._reply()
                except (OSError, ConnectionError):
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                    if attempt:
                        raise
