"""
FastAPI backend for Week 2 human review loop.
Serves findings from episodic memory (SQLite) and captures human
approve/reject decisions back into the same store. No promotion logic
here yet - that's Week 3.
"""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from explorer_agent import episodic_store as store
from explorer_agent.config import Config

app = FastAPI(title="SAP DM Data Quality - Human Review")

app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.REVIEW_APP_CORS_ORIGINS,  # see review_app.cors_origins in config.yaml
    allow_methods=["*"],
    allow_headers=["*"],
)

store.init_db()


class DecisionRequest(BaseModel):
    status: str
    comment: Optional[str] = ""


@app.get("/api/runs")
def list_runs():
    return store.get_runs()


@app.get("/api/findings")
def list_findings(run_id: Optional[str] = None, status: Optional[str] = None):
    return store.get_findings(run_id=run_id, status=status)


@app.get("/api/findings/{finding_id}")
def get_finding(finding_id: str):
    finding = store.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@app.post("/api/findings/{finding_id}/decision")
def decide_finding(finding_id: str, body: DecisionRequest):
    if body.status not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail="status must be APPROVED or REJECTED")
    if not store.get_finding(finding_id):
        raise HTTPException(status_code=404, detail="Finding not found")
    store.update_decision(finding_id, body.status, body.comment or "")
    return {"ok": True, "finding_id": finding_id, "status": body.status}


@app.get("/api/stats")
def stats(run_id: Optional[str] = None):
    return store.get_stats(run_id=run_id)


# new additon in week 3
# ======================================
from explorer_agent.memory.promotion import promote_approved_findings


@app.post("/api/promote")
def promote(run_id: Optional[str] = None):
    result = promote_approved_findings(run_id=run_id)
    return result


@app.get("/api/skills")
def list_skills():
    from explorer_agent.memory import skill_registry as registry
    return registry.get_all_skills()


# ======================================

# new addition for details in UI's view details
# ======================================
class ItemDecisionRequest(BaseModel):
    status: str
    corrected_data: Optional[str] = ""
    comment: Optional[str] = ""


@app.get("/api/findings/{finding_id}/items")
def get_finding_items(finding_id: str):
    return store.get_finding_items(finding_id)


@app.post("/api/finding-items/{item_id}/decision")
def decide_item(item_id: str, body: ItemDecisionRequest):
    if body.status not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail="status must be APPROVED or REJECTED")
    ok = store.update_item_decision(item_id, body.status, body.corrected_data or "", body.comment or "")
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True, "item_id": item_id, "status": body.status}


@app.get("/api/findings/{finding_id}/item-stats")
def item_stats(finding_id: str):
    return store.get_item_stats(finding_id)


# =====================

# IMPORTANT: mount static files LAST - /api/* routes registered above take
# precedence; this mount only catches whatever wasn't matched by them.
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
