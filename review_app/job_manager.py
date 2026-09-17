"""
Runs `python -m explorer_agent.main ...` as a background subprocess, the same
way the CLI is already invoked - explorer_agent.main is not refactored to
accept an in-process args list, so review_app just launches it as a child
process and polls it, instead of blocking a FastAPI request for however long
a profiling run takes.

Only one run is allowed at a time: the pipeline shares a single SQLite DB
file and (for llm.provider == "local") a process-wide local-LLM singleton,
so concurrent runs aren't safe.
"""

import collections
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from explorer_agent.config import Config

_LOG_TAIL_MAX_LINES = 1000


@dataclass
class JobState:
    job_id: str
    cli_args: List[str]
    status: str = "RUNNING"  # RUNNING | COMPLETED | FAILED | STOPPED
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    return_code: Optional[int] = None
    log_lines: collections.deque = field(default_factory=lambda: collections.deque(maxlen=_LOG_TAIL_MAX_LINES))
    lock: threading.Lock = field(default_factory=threading.Lock)
    process: Optional[subprocess.Popen] = None
    stop_requested: bool = False

    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "job_id": self.job_id,
                "cli_args": self.cli_args,
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "return_code": self.return_code,
                "log_tail": list(self.log_lines),
            }


_jobs: Dict[str, JobState] = {}
_current_job_id: Optional[str] = None
_manager_lock = threading.Lock()


def _stream_output(job: JobState) -> None:
    proc = job.process
    assert proc is not None and proc.stdout is not None
    for line in proc.stdout:
        with job.lock:
            job.log_lines.append(line.rstrip("\n"))
    return_code = proc.wait()
    with job.lock:
        job.return_code = return_code
        if job.stop_requested:
            job.status = "STOPPED"
        else:
            job.status = "COMPLETED" if return_code == 0 else "FAILED"
        job.finished_at = datetime.now(timezone.utc).isoformat()


def start_job(cli_args: List[str]) -> str:
    global _current_job_id
    with _manager_lock:
        current = _jobs.get(_current_job_id) if _current_job_id else None
        if current is not None:
            with current.lock:
                if current.status == "RUNNING":
                    raise RuntimeError("An Explorer Agent run is already in progress")

        job_id = str(uuid.uuid4())
        job = JobState(job_id=job_id, cli_args=cli_args)
        job.process = subprocess.Popen(
            [sys.executable, "-m", "explorer_agent.main", *cli_args],
            cwd=str(Config.PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        _jobs[job_id] = job
        _current_job_id = job_id

    thread = threading.Thread(target=_stream_output, args=(job,), daemon=True)
    thread.start()
    return job_id


def stop_job(job_id: str) -> bool:
    """Kill a running job's process tree. Returns False if the job isn't running.

    The explorer spawns sandbox child processes, so on Windows the whole tree
    is killed via taskkill /T; elsewhere terminate() is sent to the main process.
    The final STOPPED status is recorded by _stream_output once the process exits.
    """
    job = _jobs.get(job_id)
    if job is None:
        raise KeyError(job_id)
    with job.lock:
        if job.status != "RUNNING" or job.process is None:
            return False
        job.stop_requested = True
        proc = job.process

    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
    return True


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    job = _jobs.get(job_id)
    return job.to_dict() if job else None


def get_current_job() -> Optional[Dict[str, Any]]:
    if not _current_job_id:
        return None
    return get_job(_current_job_id)
