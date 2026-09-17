"""
FastAPI backend for Week 2 human review loop.
Serves findings from episodic memory (SQLite) and captures human
approve/reject decisions back into the same store. No promotion logic
here yet - that's Week 3.
"""

from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from explorer_agent import client_knowledge, client_workspace, data_loader, episodic_store as store
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


class RunClientRequest(BaseModel):
    client_name: str


class CreateClientRequest(BaseModel):
    name: str


class AutoFillRequest(BaseModel):
    fix_value: str


class RunExplorerRequest(BaseModel):
    # Data folder, dictionary and tables all come from this client's workspace.
    client_id: str
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_iterations: Optional[int] = None
    llm_provider: str = Config.LLM_PROVIDER
    fallback_providers: Optional[List[str]] = None
    no_cache: bool = False
    duplicates_only: bool = False


@app.get("/api/runs")
def list_runs(client_id: Optional[str] = None):
    return store.get_runs(client_id=client_id)


@app.get("/api/clients")
def list_clients():
    return client_knowledge.list_clients()


@app.post("/api/clients")
def create_client(body: CreateClientRequest):
    """Create (or return the existing) client for a name - 'Add client' on page 1."""
    try:
        return client_knowledge.ensure_client(body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _require_client(client_id: str) -> dict:
    client = client_knowledge.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404, detail=f"Unknown client: {client_id}")
    return client


@app.get("/api/clients/{client_id}/workspace")
def get_client_workspace(client_id: str):
    client = _require_client(client_id)
    return {"client": client, **client_workspace.get_workspace(client_id)}


async def _receive_upload(client_id: str, filename: str, request: Request, save) -> dict:
    """Stream a raw request body (the file itself - no multipart dependency) into
    the client's workspace, then validate and register it."""
    client = _require_client(client_id)
    limit = Config.MAX_UPLOAD_MB * 1024 * 1024
    tmp = client_workspace.new_upload_path(client_id)
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"'{filename}' is larger than {Config.MAX_UPLOAD_MB} MB")
                f.write(chunk)
        workspace = await run_in_threadpool(save, client_id, filename, tmp)
    except client_workspace.WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        tmp.unlink(missing_ok=True)
    return {"client": client, **workspace}


@app.put("/api/clients/{client_id}/dictionary")
async def upload_dictionary(client_id: str, filename: str, request: Request):
    return await _receive_upload(client_id, filename, request, client_workspace.save_dictionary)


@app.put("/api/clients/{client_id}/tables")
async def upload_table(client_id: str, filename: str, request: Request):
    return await _receive_upload(client_id, filename, request, client_workspace.save_table)


@app.delete("/api/clients/{client_id}/tables/{table}")
def delete_table(client_id: str, table: str):
    client = _require_client(client_id)
    try:
        workspace = client_workspace.remove_table(client_id, table)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Table {table} is not uploaded for this client")
    return {"client": client, **workspace}


@app.post("/api/runs/{run_id}/client")
def assign_run_client(run_id: str, body: RunClientRequest):
    """Link a run created before clients existed to a client, and remember the
    duplicate decisions already made in it as that client's knowledge."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("client_id"):
        raise HTTPException(status_code=409, detail=f"Run already belongs to client {run['client_name']}")
    try:
        client = client_knowledge.ensure_client(body.client_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.set_run_client(run_id, client["client_id"], client["name"])

    records = 0
    for finding in store.get_findings_light(run_id=run_id, category="DUPLICATE"):
        records += _remember_duplicate_groups(finding["id"]).get("records_written", 0)
    return {"ok": True, **client, "remembered_records": records}


def _remember_duplicate_groups(finding_id: str, group_ids: Optional[set] = None) -> dict:
    """Copy a duplicate finding's current verdicts into its client's knowledge."""
    finding = store.get_finding(finding_id)
    if not finding or finding.get("category") != "DUPLICATE":
        return {"remembered": False}
    run = store.get_run(finding["run_id"])
    if not run or not run.get("client_id"):
        return {"remembered": False, "reason": "This run has no client, so decisions can't be remembered."}
    written = 0
    for group in store.get_duplicate_groups(finding_id):
        if group_ids is None or group["duplicate_group_id"] in group_ids:
            written += client_knowledge.sync_duplicate_group(
                run["client_id"], finding["table_name"], group["members"],
                run["run_id"], finding_id, group["duplicate_group_id"])
    return {"remembered": True, "client_id": run["client_id"], "records_written": written}


@app.get("/api/findings")
def list_findings(
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    rule_scope: Optional[str] = None,
    industry: Optional[str] = None,
    is_anomaly: Optional[bool] = None,
    client_id: Optional[str] = None,
):
    return store.get_findings_light(
        run_id=run_id,
        status=status,
        category=category,
        rule_scope=rule_scope,
        industry=industry,
        is_anomaly=is_anomaly,
        client_id=client_id,
    )


@app.get("/api/dictionary")
def get_dictionary(client_id: str):
    """Business meanings for a client's tables/columns (its uploaded data dictionary), for hover tooltips."""
    _require_client(client_id)
    path = client_workspace.dictionary_path(client_id)
    columns = data_loader.load_data_dictionary_structured(str(path)) if path else {}
    return {"tables": Config.SAP_TABLE_DESCRIPTIONS, "columns": columns}


@app.get("/api/config/options")
def get_config_options():
    """Populates the 'Run Explorer Agent' modal's dropdowns/defaults."""
    return {
        "llm_providers": list(Config.SUPPORTED_LLM_PROVIDERS),
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
    memory = _remember_duplicate_groups(finding_id, {group_id})
    return {"ok": True, "finding_id": finding_id, "group_id": group_id, "verdict": body.verdict,
            "updated_count": updated, "memory": memory}


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
    memory = {"remembered": False}
    item = store.get_finding_item(item_id)
    if item and item.get("duplicate_group_id"):
        # Partners' pair knowledge depends on this verdict too, so sync the whole group.
        memory = _remember_duplicate_groups(item["finding_id"], {item["duplicate_group_id"]})
    return {"ok": True, "item_id": item_id, "verdict": body.verdict, "memory": memory}


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
def _build_explorer_cli_args(body: RunExplorerRequest, client: dict, workspace: dict) -> List[str]:
    """Mirrors explorer_agent.main's argparse semantics: only pass flags the
    user actually set, so config.yaml/CLI defaults still apply otherwise.
    Data folder and dictionary always come from the client's workspace, never
    from a path in the request."""
    args = ["--client", client["name"],
            "--data-dir", workspace["data_dir"],
            "--dictionary-file", workspace["dictionary"]["file"]]
    if body.model:
        args += ["--model", body.model]
    if body.temperature is not None:
        args += ["--temperature", str(body.temperature)]
    if body.max_iterations is not None:
        args += ["--max-iterations", str(body.max_iterations)]
    if body.no_cache:
        args.append("--no-cache")
    if body.duplicates_only:
        args.append("--duplicates-only")
    args += ["--llm-provider", body.llm_provider]
    if body.fallback_providers is not None:
        # Passing the flag with no values explicitly disables fallbacks,
        # matching the documented CLI behavior.
        args += ["--fallback-providers", *body.fallback_providers]
    return args


@app.post("/api/jobs/run-explorer")
def run_explorer_endpoint(body: RunExplorerRequest):
    client = _require_client(body.client_id)
    workspace = client_workspace.get_workspace(body.client_id)
    if not workspace["ready"]:
        raise HTTPException(status_code=400,
                            detail="Upload a data dictionary and at least one table for this client first.")
    cli_args = _build_explorer_cli_args(body, client, workspace)
    try:
        job_id = job_manager.start_job(cli_args)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "job_id": job_id, "client_id": client["client_id"], "cli_args": cli_args}


@app.post("/api/jobs/{job_id}/stop")
def stop_job_endpoint(job_id: str):
    try:
        stopped = job_manager.stop_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")
    if not stopped:
        raise HTTPException(status_code=409, detail="Job is not running")
    return {"ok": True, "job_id": job_id}


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
