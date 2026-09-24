"""Pipeline events - how this agent tells the next one it is done (contracts.PipelineEvent).

POC transport: an append-only outbox, ``<handoff.dir>/events.jsonl``, served over
REST (``GET /api/handoff/events``) for polling. Production puts a message queue
(Celery + Redis / RabbitMQ / Kafka) behind ``publish``: the event documents stay
the same, only the transport changes - agents never call each other directly.
"""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .contracts import PIPELINE_EVENT, CONTRACT_VERSION, PipelineEvent
from .logging_config import get_logger

logger = get_logger("events")
_lock = threading.Lock()


def _outbox() -> Path:
    return Path(Config.HANDOFF_DIR) / "events.jsonl"


def publish(event_type: str, client_id: str, client_name: Optional[str], run_id: Optional[str],
            status: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    event = PipelineEvent(
        contract=PIPELINE_EVENT, version=CONTRACT_VERSION, event_id=str(uuid.uuid4()), type=event_type,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        producer={"agent": "data-profiling-agent", "run_id": run_id, "client_id": client_id,
                  "client_name": client_name},
        status=status, payload=payload).model_dump()
    path = _outbox()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    logger.info("Event %s (%s) published for client %s, run %s", event_type, status, client_id, run_id)
    return event


def read(client_id: Optional[str] = None, event_type: Optional[str] = None,
         after: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    """Events oldest first; ``after`` = an event_id already seen (resume a poll)."""
    path = _outbox()
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if after:
        ids = [e["event_id"] for e in events]
        events = events[ids.index(after) + 1:] if after in ids else events
    events = [e for e in events if (not client_id or e["producer"]["client_id"] == client_id)
              and (not event_type or e["type"] == event_type)]
    return events[:limit]
