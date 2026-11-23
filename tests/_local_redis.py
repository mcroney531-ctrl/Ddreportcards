"""A throwaway local redis-server for tests that must run the real Lua.

Upstash speaks the Redis command set over REST; IntelStore only needs a
cmd(*args) callable. This starts redis-server on a free port with no
persistence and exposes a minimal RESP client as that callable, so the
production Lua scripts are exercised verbatim. Tests skip if redis-server is
not installed.
"""
import shutil
import socket
import subprocess
import time

REDIS_BIN = shutil.which("redis-server")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalRedis:
    def __init__(self) -> None:
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [REDIS_BIN, "--port", str(self.port), "--bind", "127.0.0.1",
             "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                break
            except OSError:
                if time.time() > deadline:
                    self.proc.kill()
                    raise
                time.sleep(0.05)
        self.buf = b""
        self.log: list[tuple] = []

    def close(self) -> None:
        self.sock.close()
        self.proc.kill()
        self.proc.wait()

    def __call__(self, *args):
        self.log.append(args)
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = str(a).encode()
            parts.append(b"$%d\r\n%s\r\n" % (len(b), b))
        self.sock.sendall(b"".join(parts))
        return self._read()

    def _line(self) -> bytes:
        while b"\r\n" not in self.buf:
            self.buf += self.sock.recv(65536)
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _read(self):
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
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self.buf += self.sock.recv(65536)
            data, self.buf = self.buf[:n], self.buf[n + 2:]
            return data.decode()
        if kind == b"*":
            n = int(rest)
            return None if n < 0 else [self._read() for _ in range(n)]
        raise RuntimeError(f"bad reply {line!r}")
