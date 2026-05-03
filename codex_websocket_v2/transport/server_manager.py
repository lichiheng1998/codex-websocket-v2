"""Process-level singleton that manages the shared codex app-server child.

Multiple ``CodexSession`` instances each open their own WebSocket connection,
but they all talk to the same ``codex app-server`` subprocess. The manager
ref-counts the sessions: the first ``acquire()`` spawns the process, the last
``release()`` terminates it.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import socket
import subprocess
import threading
import time
from typing import ClassVar, Optional

from ..core.policies import PORT_PROBE_TIMEOUT, SHUTDOWN_TIMEOUT, STARTUP_TIMEOUT
from ..core.state import Result, err, ok
from ..core.utils import pick_free_port

logger = logging.getLogger(__name__)


@dataclass
class ServerLease:
    manager: "CodexServerManager"
    port: int
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.manager.release()


class CodexServerManager:
    _instance: ClassVar[Optional["CodexServerManager"]] = None
    _instance_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self._log_file = None
        self._ref_count = 0
        self._start_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "CodexServerManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def acquire(self) -> Result:
        """Ensure the app-server is running. Returns ``{ok, port}``.

        First caller spawns the subprocess; subsequent callers just bump
        the ref count and return the existing port.
        """
        with self._start_lock:
            if self.proc is not None and self.proc.poll() is None:
                self._ref_count += 1
                return ok(port=self.port)

            spawn = self._spawn()
            if not spawn["ok"]:
                return spawn
            self._ref_count = 1
            return ok(port=self.port)

    def acquire_lease(self) -> Result:
        """Acquire a closeable app-server lease for one bridge."""
        acquired = self.acquire()
        if not acquired["ok"]:
            return acquired
        return ok(lease=ServerLease(self, acquired["port"]))

    def release(self) -> None:
        """Decrement ref count; terminate the subprocess on the last release."""
        with self._start_lock:
            if self._ref_count <= 0:
                return
            self._ref_count -= 1
            if self._ref_count > 0:
                return
            self._shutdown_locked()

    def force_shutdown(self) -> None:
        """Tear down regardless of ref count — atexit / test cleanup."""
        with self._start_lock:
            self._ref_count = 0
            self._shutdown_locked()

    def _spawn(self) -> Result:
        try:
            self.port = pick_free_port()
            cmd = ["codex", "app-server", "--listen", f"ws://127.0.0.1:{self.port}"]
            logger.info("Starting codex app-server on port %d", self.port)
            log_path = os.path.expanduser("~/.hermes/logs/codex-app-server.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            env = os.environ.copy()
            env["RUST_LOG"] = env.get("RUST_LOG", "codex_app_server=debug,codex_core=info")
            env["NO_COLOR"] = "1"
            self._log_file = open(log_path, "a")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL,
                stderr=self._log_file, env=env,
            )
        except Exception as exc:
            return err(f"failed to spawn codex app-server: {exc}")

        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return err(
                    f"codex app-server exited with code {self.proc.returncode}; "
                    f"see {log_path}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=PORT_PROBE_TIMEOUT):
                    return ok(port=self.port)
            except OSError:
                time.sleep(0.2)
        return err("codex app-server failed to open port within timeout")

    def _shutdown_locked(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.port = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
