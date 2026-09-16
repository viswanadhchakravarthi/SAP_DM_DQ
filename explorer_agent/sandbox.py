
"""
Sandboxed local execution of LLM-generated pandas code.

Isolation approach for Week 1 (POC-grade, not production-grade):
- Separate process (multiprocessing, 'spawn' context - portable across OS)
- Restricted builtins (no eval/exec/open/__import__ of dangerous modules)
- Blocked imports of os/sys/socket/subprocess/etc.
- CPU + memory resource limits (Unix only - no-op on Windows)
- Hard wall-clock timeout with forced termination

IMPORTANT: This blocks *naive* misuse, not a determined adversary.
For real client engagements, harden this further (Docker/gvisor/Firecracker
microVM, seccomp profiles, no filesystem mount, network namespace isolation)
before this touches actual client data. Treat this as "good enough to let
an LLM experiment safely," not "safe against malicious code."
"""

import multiprocessing as mp
import traceback
import io
import contextlib
from typing import Dict, Any

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
}


def _restricted_import(name, *args, **kwargs):
    if name.split(".")[0] in BLOCKED_MODULES:
        raise ImportError(f"Import of '{name}' is blocked in sandbox")
    return __import__(name, *args, **kwargs)


def _sandbox_worker(code: str, data_context: Dict[str, Any], result_queue,
                    mem_limit_mb: int, cpu_seconds: int):
    if _HAS_RESOURCE:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
            resource.setrlimit(
                resource.RLIMIT_AS,
                (mem_limit_mb * 1024 * 1024, mem_limit_mb * 1024 * 1024),
            )
        except Exception:
            pass  # some platforms/containers don't allow setting these

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

    safe_builtins = dict(SAFE_BUILTINS)
    safe_builtins["__import__"] = _restricted_import

    exec_globals: Dict[str, Any] = {
        "__builtins__": safe_builtins,
        "pd": pd,
        "np": np,
        "re": re,
        "datetime": datetime,
        "mask_value": mask_value,
        "fuzzy_token_similarity": fuzzy_token_similarity,
        "cluster_duplicates": cluster_duplicates,
        "detect_distribution_outliers": detect_distribution_outliers,
    }
    exec_globals.update(data_context)  # e.g. {"df": df}

    stdout_capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_capture):
            exec(code, exec_globals)
        result = exec_globals.get("result", None)
        result_queue.put({
            "success": True, "result": result,
            "stdout": stdout_capture.getvalue(), "error": None,
        })
    except Exception as e:
        result_queue.put({
            "success": False, "result": None,
            "stdout": stdout_capture.getvalue(),
            "error": f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}",
        })


class SandboxExecutor:
    def __init__(self, timeout_seconds: int = 10, mem_limit_mb: int = 512):
        self.timeout_seconds = timeout_seconds
        self.mem_limit_mb = mem_limit_mb

    def run(self, code: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        context: data only, e.g. {"df": df}. Do NOT pass modules here -
        the worker imports pd/np itself to avoid pickling ambiguity.
        """
        result_queue = _CTX.Queue()
        proc = _CTX.Process(
            target=_sandbox_worker,
            args=(code, context, result_queue, self.mem_limit_mb, self.timeout_seconds),
        )
        proc.start()
        proc.join(self.timeout_seconds + 2)

        if proc.is_alive():
            proc.terminate()
            proc.join()
            return {"success": False, "result": None, "stdout": "",
                    "error": f"Execution timed out after {self.timeout_seconds}s"}

        if not result_queue.empty():
            return result_queue.get()

        return {"success": False, "result": None, "stdout": "",
                "error": "No result returned (process may have crashed / hit memory limit)"}
