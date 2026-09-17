"""
FastAPI backend for Week 2 human review loop.
Serves findings from episodic memory (SQLite) and captures human
approve/reject decisions back into the same store. No promotion logic
here yet - that's Week 3.
"""

from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from explorer_agent import data_loader, episodic_store as store
from explorer_agent.config import Config
from . import job_manager

app = FastAPI(title="SAP DM Data Quality - Human Review")

app.add_middleware(
    CORSMiddleware,
    allow_origins=Config.REVIEW_APP_CORS_ORIGINS,  # see review_app.cors_origins in config.yaml
    allow_methods=["*"],
    allow_headers=["*"],
)

store.init_db()

# The dictionary is committed metadata (business meanings), not per-run
# profiled data - review_app has no --data-dir, so it's loaded once from the
# repo's committed data/ folder regardless of which run's data was profiled.
_DICTIONARY_PATH = Config.PROJECT_ROOT / "data" / Config.DATA_DICTIONARY_FILE
_column_dictionary = (
    data_loader.load_data_dictionary_structured(str(_DICTIONARY_PATH))
    if _DICTIONARY_PATH.exists() else {}
)


class DecisionRequest(BaseModel):
    status: str
    comment: Optional[str] = ""


class ItemVerdictRequest(BaseModel):
    verdict: str  # see explorer_agent.episodic_store.ALL_VALID_VERDICTS
    comment: Optional[str] = ""
    corrected_data: Optional[str] = ""


class ClusterVerdictRequest(BaseModel):
    verdict: str
    comment: Optional[str] = ""


class AutoFillRequest(BaseModel):
    fix_value: str


class RunExplorerRequest(BaseModel):
    data_dir: str
    dictionary_file: Optional[str] = None
    tables: Optional[List[str]] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_iterations: Optional[int] = None
    llm_provider: str = Config.LLM_PROVIDER
    fallback_providers: Optional[List[str]] = None
    no_cache: bool = False


@app.get("/api/runs")
def list_runs():
    return store.get_runs()


@app.get("/api/findings")
def list_findings(
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    rule_scope: Optional[str] = None,
    industry: Optional[str] = None,
    is_anomaly: Optional[bool] = None,
):
    return store.get_findings_light(
        run_id=run_id,
        status=status,
        category=category,
        rule_scope=rule_scope,
        industry=industry,
        is_anomaly=is_anomaly,
    )


@app.get("/api/dictionary")
def get_dictionary():
    """Business meanings for SAP tables/columns, for the review app's hover tooltips."""
    return {"tables": Config.SAP_TABLE_DESCRIPTIONS, "columns": _column_dictionary}


@app.get("/api/config/options")
def get_config_options():
    """Populates the 'Run Explorer Agent' modal's dropdowns/defaults."""
    return {
        "llm_providers": list(Config.SUPPORTED_LLM_PROVIDERS),
        "tables": sorted(Config.SAP_TABLE_FILES.keys()),
        "default_data_dir": str(Config.PROJECT_ROOT / "data"),
        "default_dictionary_file": Config.DATA_DICTIONARY_FILE,
        "default_max_iterations": Config.MAX_ITERATIONS_PER_COLUMN,
        "default_llm_provider": Config.LLM_PROVIDER,
        "default_fallback_providers": Config.LLM_FALLBACK_PROVIDERS,
        "gemini_model_default": Config.GEMINI_MODEL,
        "groq_models_default": [m["name"] for m in Config.GROQ_MODELS],
    }


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


# Promotion endpoints
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


# Finding items & duplicate governance endpoints
# ======================================
class ItemDecisionRequest(BaseModel):
    status: str
    corrected_data: Optional[str] = ""
    comment: Optional[str] = ""


@app.get("/api/findings/{finding_id}/items")
def get_finding_items(finding_id: str):
    return store.get_finding_items(finding_id)


@app.get("/api/findings/{finding_id}/duplicate-groups")
def get_finding_duplicate_groups(finding_id: str):
    return store.get_duplicate_groups(finding_id)


@app.post("/api/findings/{finding_id}/duplicate-groups/{group_id}/verdict")
def set_cluster_verdict_endpoint(finding_id: str, group_id: str, body: ClusterVerdictRequest):
    if body.verdict not in store.ALL_VALID_VERDICTS:
        raise HTTPException(status_code=400, detail="Invalid verdict")
    updated = store.set_cluster_verdict(finding_id, group_id, body.verdict, body.comment or "")
    if updated == 0:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return {"ok": True, "finding_id": finding_id, "group_id": group_id, "verdict": body.verdict, "updated_count": updated}


@app.post("/api/finding-items/{item_id}/decision")
def decide_item(item_id: str, body: ItemDecisionRequest):
    if body.status not in ("APPROVED", "REJECTED"):
        raise HTTPException(status_code=400, detail="status must be APPROVED or REJECTED")
    ok = store.update_item_decision(item_id, body.status, body.corrected_data or "", body.comment or "")
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True, "item_id": item_id, "status": body.status}


@app.post("/api/finding-items/{item_id}/verdict")
def set_item_verdict_endpoint(item_id: str, body: ItemVerdictRequest):
    if body.verdict not in store.ALL_VALID_VERDICTS:
        raise HTTPException(status_code=400, detail="Invalid verdict")
    ok = store.update_item_verdict(item_id, body.verdict, body.comment or "", body.corrected_data or "")
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True, "item_id": item_id, "verdict": body.verdict}


@app.post("/api/finding-items/{item_id}/autofill")
def apply_autofill_endpoint(item_id: str, body: AutoFillRequest):
    ok = store.apply_auto_fix(item_id, body.fix_value)
    if not ok:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"ok": True, "item_id": item_id, "auto_fixed_value": body.fix_value}


@app.get("/api/findings/{finding_id}/item-stats")
def item_stats(finding_id: str):
    return store.get_item_stats(finding_id)


# Run Explorer Agent (background subprocess + polling)
# ======================================
def _build_explorer_cli_args(body: RunExplorerRequest) -> List[str]:
    """Mirrors explorer_agent.main's argparse semantics: only pass flags the
    user actually set, so config.yaml/CLI defaults still apply otherwise."""
    args = ["--data-dir", body.data_dir]
    if body.dictionary_file:
        args += ["--dictionary-file", body.dictionary_file]
    if body.model:
        args += ["--model", body.model]
    if body.temperature is not None:
        args += ["--temperature", str(body.temperature)]
    if body.max_iterations is not None:
        args += ["--max-iterations", str(body.max_iterations)]
    if body.tables:
        args += ["--tables", *body.tables]
    if body.no_cache:
        args.append("--no-cache")
    args += ["--llm-provider", body.llm_provider]
    if body.fallback_providers is not None:
        # Passing the flag with no values explicitly disables fallbacks,
        # matching the documented CLI behavior.
        args += ["--fallback-providers", *body.fallback_providers]
    return args


@app.post("/api/jobs/run-explorer")
def run_explorer_endpoint(body: RunExplorerRequest):
    if not Path(body.data_dir).is_dir():
        raise HTTPException(status_code=400, detail=f"Data directory not found: {body.data_dir}")
    cli_args = _build_explorer_cli_args(body)
    try:
        job_id = job_manager.start_job(cli_args)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "job_id": job_id, "cli_args": cli_args}


@app.get("/api/jobs/current")
def get_current_job_endpoint():
    job = job_manager.get_current_job()
    if not job:
        raise HTTPException(status_code=404, detail="No job has been run yet")
    return job


@app.get("/api/jobs/{job_id}")
def get_job_endpoint(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# =====================

# IMPORTANT: mount static files LAST - /api/* routes registered above take
# precedence; this mount only catches whatever wasn't matched by them.
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
