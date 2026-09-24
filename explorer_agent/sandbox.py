
"""
Sandboxed local execution of LLM-generated pandas code.

Isolation approach for Week 1 (POC-grade, not production-grade):
- Separate process (multiprocessing, 'spawn' context - portable across OS)
- Restricted builtins (no eval/exec/open/__import__ of dangerous modules)
- Blocked imports of os/sys/socket/subprocess/etc.
- CPU + memory resource limits (Unix only - no-op on Windows)
- Hard wall-clock timeout per check with forced termination

One worker process serves a batch of checks (``SandboxExecutor.session``): the
tables are sent to it once, then each check runs with its own timeout. A fresh
process and a fresh copy of the data per check cost ~8 s and 210 MB of pickling
per check at 1M rows, and the transfer ate into the check's own timeout. Checks
still cannot change each other's data - each gets shallow copies of the frames
under pandas copy-on-write - and a check that times out or crashes takes the
worker down with it; the next check starts a new one. What one check leaves
behind in module state (e.g. ``pd.set_option``) is visible to the next check
of the same batch.

IMPORTANT: This blocks *naive* misuse, not a determined adversary.
For real client engagements, harden this further (Docker/gvisor/Firecracker
microVM, seccomp profiles, no filesystem mount, network namespace isolation)
before this touches actual client data. Treat this as "good enough to let
an LLM experiment safely," not "safe against malicious code."
"""

import contextlib
import io
import multiprocessing as mp
import pickle
import traceback
from typing import Any, Dict, Optional

try:
    import resource  # Unix only
    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False

_CTX = mp.get_context("spawn")  # portable across Linux/Mac/Windows

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float,
    "getattr": getattr, "hasattr": hasattr, "int": int,
    "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "print": print, "range": range,
    "round": round, "set": set, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "type": type, "zip": zip,
    # Exception types - needed for try/except in generated code
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError,
    "AttributeError": AttributeError, "ZeroDivisionError": ZeroDivisionError,
    "StopIteration": StopIteration, "RuntimeError": RuntimeError,
}

BLOCKED_MODULES = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib",
    "requests", "urllib", "importlib", "ctypes", "multiprocessing",
    "resource",  # would let a check lift its own CPU / memory limits
}

_CRASHED = "No result returned (process may have crashed / hit memory limit)"


def _restricted_import(name, *args, **kwargs):
    if name.split(".")[0] in BLOCKED_MODULES:
        raise ImportError(f"Import of '{name}' is blocked in sandbox")
    return __import__(name, *args, **kwargs)


def _limit_memory(mem_limit_mb: int) -> None:
    """Unix: allow mem_limit_mb on top of what the loaded data already uses (Linux
    reports that; elsewhere no address-space cap is set)."""
    try:
        with open("/proc/self/statm") as f:
            used = int(f.read().split()[0]) * resource.getpagesize()
        limit = used + mem_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass  # no /proc, or the platform/container doesn't allow it


def _limit_cpu(cpu_seconds: int) -> None:
    """Unix: CPU time is cumulative per process, so each check gets cpu_seconds more."""
    try:
        import os
        used = int(sum(os.times()[:2]))
        hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
        soft = used + cpu_seconds + 1
        resource.setrlimit(resource.RLIMIT_CPU, (soft if hard == resource.RLIM_INFINITY else min(soft, hard), hard))
    except Exception:
        pass


def _fresh(value: Any) -> Any:
    """A per-check view of the data: shallow copies, isolated by copy-on-write."""
    import pandas as pd
    if isinstance(value, (pd.DataFrame, pd.Series)):
        return value.copy(deep=False)
    if isinstance(value, dict):
        return {k: _fresh(v) for k, v in value.items()}
    return value


def _sandbox_worker(conn, mem_limit_mb: int, cpu_seconds: int):
    import pandas as pd
    import numpy as np
    import re
    import datetime
    from .profiler_primitives import (
        mask_value,
        fuzzy_token_similarity,
        cluster_duplicates,
        detect_distribution_outliers,
    )

    pd.set_option("mode.copy_on_write", True)
    try:
        context = pickle.loads(conn.recv_bytes())
    except Exception as e:
        conn.send({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return
    if _HAS_RESOURCE:
        _limit_memory(mem_limit_mb)
    conn.send({"ready": True})

    safe_builtins = dict(SAFE_BUILTINS)
    safe_builtins["__import__"] = _restricted_import
    base_globals: Dict[str, Any] = {
        "pd": pd,
        "np": np,
        "re": re,
        "datetime": datetime,
        "mask_value": mask_value,
        "fuzzy_token_similarity": fuzzy_token_similarity,
        "cluster_duplicates": cluster_duplicates,
        "detect_distribution_outliers": detect_distribution_outliers,
    }

    while True:
        try:
            code = conn.recv()
        except EOFError:
            return
        if code is None:
            return
        if _HAS_RESOURCE:
            _limit_cpu(cpu_seconds)
        exec_globals = {"__builtins__": dict(safe_builtins), **base_globals, **_fresh(context)}
        stdout_capture = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_capture):
                exec(code, exec_globals)
            payload = {"success": True, "result": exec_globals.get("result", None),
                       "stdout": stdout_capture.getvalue(), "error": None}
        except Exception as e:
            payload = {"success": False, "result": None, "stdout": stdout_capture.getvalue(),
                       "error": f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"}
        try:
            conn.send(payload)
        except Exception as e:  # the result itself can't be pickled (generator, lambda, ...)
            conn.send({"success": False, "result": None, "stdout": payload["stdout"],
                       "error": f"Result could not be returned from the sandbox: {type(e).__name__}: {e}"})


class SandboxSession:
    """One worker for a batch of checks on the same data. Use via SandboxExecutor.session()."""

    def __init__(self, executor: "SandboxExecutor", context: Dict[str, Any]):
        self.executor = executor
        # Pickled once; kept only to restart the worker after a timeout or crash.
        self._payload = pickle.dumps(context, protocol=pickle.HIGHEST_PROTOCOL)
        self._proc = None
        self._conn = None

    def _start(self) -> Optional[str]:
        """Start a worker and hand it the data. Returns an error message, or None."""
        parent, child = _CTX.Pipe()
        proc = _CTX.Process(target=_sandbox_worker, daemon=True,
                            args=(child, self.executor.mem_limit_mb, self.executor.timeout_seconds))
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent
        try:
            parent.send_bytes(self._payload)
            if not parent.poll(self.executor.load_timeout_seconds):
                self._stop()
                return f"Sandbox did not load the data within {self.executor.load_timeout_seconds}s"
            ready = parent.recv()
        except (EOFError, OSError):
            self._stop()
            return _CRASHED
        if not ready.get("ready"):
            self._stop()
            return f"Sandbox could not load the data: {ready.get('error')}"
        return None

    def _stop(self) -> None:
        if self._proc is not None:
            if self._proc.is_alive():
                self._proc.terminate()
            self._proc.join()
        if self._conn is not None:
            self._conn.close()
        self._proc = self._conn = None

    def run(self, code: str) -> Dict[str, Any]:
        if self._proc is None or not self._proc.is_alive():
            self._stop()
            error = self._start()
            if error:
                return {"success": False, "result": None, "stdout": "", "error": error}
        try:
            self._conn.send(code)
            if not self._conn.poll(self.executor.timeout_seconds):
                self._stop()
                return {"success": False, "result": None, "stdout": "",
                        "error": f"Execution timed out after {self.executor.timeout_seconds}s"}
            return self._conn.recv()
        except (EOFError, OSError):
            self._stop()
            return {"success": False, "result": None, "stdout": "", "error": _CRASHED}

    def close(self) -> None:
        if self._conn is not None and self._proc is not None and self._proc.is_alive():
            try:
                self._conn.send(None)
                self._proc.join(2)
            except OSError:
                pass
        self._stop()
        self._payload = b""


class SandboxExecutor:
    def __init__(self, timeout_seconds: int = 10, mem_limit_mb: int = 512, load_timeout_seconds: int = 120):
        self.timeout_seconds = timeout_seconds
        self.mem_limit_mb = mem_limit_mb
        self.load_timeout_seconds = load_timeout_seconds

    @contextlib.contextmanager
    def session(self, context: Dict[str, Any]):
        """
        Run several checks against the same data with one worker:

            with executor.session({"df": df, "tables": tables}) as box:
                box.run(code_a); box.run(code_b)

        context: data only. Do NOT pass modules here - the worker imports
        pd/np itself to avoid pickling ambiguity.
        """
        box = SandboxSession(self, context)
        try:
            yield box
        finally:
            box.close()

    def run(self, code: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """One check in its own worker (a session of one)."""
        with self.session(context) as box:
            return box.run(code)
