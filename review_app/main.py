"""
FastAPI backend for Week 2 human review loop.
Serves findings from episodic memory (SQLite) and captures human
approve/reject decisions back into the same store. No promotion logic
here yet - that's Week 3.
"""

import json
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


@app.middleware("http")
async def revalidate_static_files(request: Request, call_next):
    """Make browsers re-check the UI files (HTML/JS/CSS) on every load. Without a
    Cache-Control header they cache them heuristically, so after an update a browser
    can keep running the old app.js/setup.js against the new HTML. Unchanged files
    still come from cache (ETag -> 304), so this costs next to nothing."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


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
    reviewer: Optional[str] = ""


class ClusterAcceptRequest(BaseModel):
    # The Unique record the others merge into; None only when every record is Unique.
    survivor_item_id: Optional[str] = None
    # Other records marked Unique: separate entities (look-alikes), not duplicates.
    separate_item_ids: List[str] = []
    reviewer: Optional[str] = ""
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
    max_repair_rounds: Optional[int] = None
    llm_provider: str = Config.LLM_PROVIDER
    no_cache: bool = False
    duplicates_only: bool = False


@app.get("/api/runs")
def list_runs(client_id: Optional[str] = None):
    return store.get_runs(client_id=client_id)


@app.get("/api/runs/{run_id}/llm-usage")
def get_run_llm_usage(run_id: str):
    """Token counts of every LLM request in a run (totals, per role, per call). Counts only, no prompts."""
    if not store.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return store.get_run_llm_usage(run_id)


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


@app.delete("/api/clients/{client_id}/files")
def clear_client_files(client_id: str):
    """Page 1 "Clear all files": remove the client's uploaded dictionary and tables in one go.
    Findings, review decisions and the client's memory are kept. Refused during a run."""
    client = _require_client(client_id)
    job = job_manager.get_current_job()
    if job and job.get("status") == "RUNNING":
        raise HTTPException(status_code=409, detail="A run is in progress - wait for it to finish before clearing files")
    try:
        workspace = client_workspace.clear_files(client_id)
    except client_workspace.WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
        "default_max_repair_rounds": Config.MAX_REPAIR_ROUNDS,
        "default_llm_provider": Config.LLM_PROVIDER,
        "gemini_model_default": Config.GEMINI_MODEL,
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


@app.get("/api/scorecard")
def get_scorecard(client_id: str, run_id: Optional[str] = None):
    """Composite DQ scorecard of a run (default: the client's latest scored run), with the
    previous scored run's index per entry for the trend."""
    runs = store.scorecard_runs(client_id)
    if not runs:
        raise HTTPException(status_code=404, detail="No scorecard yet - run the explorer for this client")
    run_id = run_id if run_id in runs else runs[0]
    previous = runs[runs.index(run_id) + 1] if runs.index(run_id) + 1 < len(runs) else None
    before = {(e["scope"], e["name"]): e for e in store.get_scorecard(previous)} if previous else {}
    entries = store.get_scorecard(run_id)
    for e in entries:
        prev = before.get((e["scope"], e["name"]))
        e["previous_dq_index"] = prev["dq_index"] if prev else None
    for e in entries:   # the worklist is served separately (/api/scorecard/not-ready)
        (e["details"].get("readiness") or {}).pop("worklist", None)
        prev = before.get((e["scope"], e["name"]))
        e["previous_readiness"] = prev["readiness"] if prev else None
    return {"run_id": run_id, "previous_run_id": previous, "weights": Config.SCORECARD_WEIGHTS,
            "bands": Config.SCORECARD_BANDS, "readiness_bands": Config.READINESS_BANDS,
            "overall": next((e for e in entries if e["scope"] == "run"), None),
            "objects": [e for e in entries if e["scope"] == "object"],
            "tables": [e for e in entries if e["scope"] == "table"]}


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
    updated = store.set_cluster_verdict(finding_id, group_id, body.verdict, body.comment or "",
                                        (body.reviewer or "").strip())
    if updated == 0:
        raise HTTPException(status_code=404, detail="Cluster not found")
    memory = _remember_duplicate_groups(finding_id, {group_id})
    return {"ok": True, "finding_id": finding_id, "group_id": group_id, "verdict": body.verdict,
            "updated_count": updated, "memory": memory}


@app.post("/api/findings/{finding_id}/duplicate-groups/{group_id}/accept")
def accept_cluster_endpoint(finding_id: str, group_id: str, body: ClusterAcceptRequest):
    """Accept a cluster: survivor = Unique, separate entities = Unique, all others =
    Duplicate of the survivor. Saved to the run and to the client's memory, so a
    fully decided cluster is not shown again on the next run."""
    try:
        updated = store.accept_cluster(finding_id, group_id, body.survivor_item_id, body.separate_item_ids,
                                       (body.reviewer or "").strip(), body.comment or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if updated == 0:
        raise HTTPException(status_code=404, detail="Cluster or record not found")
    memory = _remember_duplicate_groups(finding_id, {group_id})
    return {"ok": True, "finding_id": finding_id, "group_id": group_id, "updated_count": updated, "memory": memory}


@app.post("/api/findings/{finding_id}/duplicate-groups/{group_id}/undo")
def undo_cluster_endpoint(finding_id: str, group_id: str):
    """Undo the cluster's last Accept / To be confirmed: rows go back to their earlier
    state and the client's memory is updated to match."""
    restored = store.undo_cluster(finding_id, group_id)
    if restored == 0:
        raise HTTPException(status_code=404, detail="Cluster not found")
    memory = _remember_duplicate_groups(finding_id, {group_id})
    groups = [g for g in store.get_duplicate_groups(finding_id) if g["duplicate_group_id"] == group_id]
    return {"ok": True, "finding_id": finding_id, "group_id": group_id, "restored_count": restored,
            "group": groups[0] if groups else None, "memory": memory}


@app.get("/api/scorecard/not-ready")
def get_not_ready_records(client_id: str, table: str, run_id: Optional[str] = None):
    """The cleansing worklist: one table's records that cannot be loaded as they are, with reasons."""
    runs = store.scorecard_runs(client_id)
    if not runs:
        raise HTTPException(status_code=404, detail="No scorecard yet")
    run_id = run_id if run_id in runs else runs[0]
    entry = next((e for e in store.get_scorecard(run_id) if e["scope"] == "table" and e["name"] == table), None)
    if not entry:
        raise HTTPException(status_code=404, detail=f"No table {table} in run {run_id}")
    r = entry["details"].get("readiness") or {}
    return {"run_id": run_id, "table": table, **{k: r.get(k) for k in (
        "records", "in_scope", "out_of_scope", "ready", "not_ready", "unlisted", "score", "top_reasons", "worklist")}}


# Handoff to / from the neighbouring agents (explorer_agent/contracts.py)
# ======================================
@app.get("/api/clients/{client_id}/handoff/structural-profile")
def get_structural_profile(client_id: str, run_id: Optional[str] = None):
    """The structural profile for the Mapping / Value Mapping Agent: the latest run's, or one run's."""
    from fastapi.responses import FileResponse
    from explorer_agent import structural_profile
    path = structural_profile.profile_path(client_id, run_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No structural profile yet - run the explorer for this client")
    return FileResponse(path, media_type="application/json", filename=f"{client_id}_structural_profile.json")


@app.get("/api/handoff/schemas/{name}")
def get_contract_schema(name: str):
    """JSON Schema of a handoff contract: sap-dm.structural-profile or sap-dm.field-value-mapping."""
    from explorer_agent import contracts
    models = {contracts.STRUCTURAL_PROFILE: contracts.StructuralProfile,
              contracts.FIELD_VALUE_MAPPING: contracts.FieldValueMapping,
              contracts.TARGET_DOMAINS: contracts.TargetDomains,
              contracts.PIPELINE_EVENT: contracts.PipelineEvent}
    if name not in models:
        raise HTTPException(status_code=404, detail=f"Unknown contract; known: {sorted(models)}")
    return models[name].model_json_schema()


@app.put("/api/clients/{client_id}/handoff/field-mapping")
async def put_field_mapping(client_id: str, request: Request):
    """The Mapping Agent delivers its field/value mapping here. It is validated against the
    contract and stored in the client's data folder, where the next run picks it up."""
    from explorer_agent import contracts
    if not client_knowledge.get_client(client_id):
        raise HTTPException(status_code=404, detail="Unknown client")
    try:
        doc = json.loads(await request.body())
        contracts.check_version(doc.get("version", "?"), doc.get("contract", ""))
        parsed = contracts.FieldValueMapping.model_validate(doc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Not a valid {contracts.FIELD_VALUE_MAPPING} document: {exc}")
    folder = Path(Config.CLIENT_DATA_DIR) / client_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / Config.HANDOFF_MAPPING_FILE).write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "client_id": client_id, "field_mappings": len(parsed.field_mappings),
            "value_mappings": len(parsed.value_mappings), "stored_as": Config.HANDOFF_MAPPING_FILE}


@app.put("/api/clients/{client_id}/handoff/target-domains")
async def put_target_domains(client_id: str, request: Request):
    """The Metadata Repository delivers the allowed SAP values per target field (check tables)."""
    from explorer_agent import contracts
    if not client_knowledge.get_client(client_id):
        raise HTTPException(status_code=404, detail="Unknown client")
    try:
        doc = json.loads(await request.body())
        contracts.check_version(doc.get("version", "?"), doc.get("contract", ""))
        parsed = contracts.TargetDomains.model_validate(doc)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Not a valid {contracts.TARGET_DOMAINS} document: {exc}")
    folder = Path(Config.CLIENT_DATA_DIR) / client_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / Config.HANDOFF_TARGET_DOMAINS_FILE).write_text(json.dumps(doc, indent=2, ensure_ascii=False),
                                                             encoding="utf-8")
    return {"ok": True, "client_id": client_id, "domains": len(parsed.domains),
            "stored_as": Config.HANDOFF_TARGET_DOMAINS_FILE}


@app.get("/api/handoff/events")
def get_pipeline_events(client_id: Optional[str] = None, type: Optional[str] = None, after: Optional[str] = None,
                        limit: int = 100):
    """Pipeline events (sap-dm.pipeline-event), oldest first; pass the last seen event_id as
    `after` to resume. The POC outbox - a queue replaces the transport in production."""
    from explorer_agent import events
    return events.read(client_id=client_id, event_type=type, after=after, limit=limit)


@app.get("/api/clients/{client_id}/duplicate-decisions.csv")
def export_duplicate_decisions(client_id: str):
    """The client's remembered duplicate decisions as a merge map (all runs): each record,
    its verdict, the survivor it merges into and the proposed action - the old-number ->
    surviving-number table the cleansing/load stage needs."""
    import csv
    import io
    from fastapi.responses import Response
    if not client_knowledge.get_client(client_id):
        raise HTTPException(status_code=404, detail="Unknown client")
    data = client_knowledge.load_all_duplicate_decisions(client_id)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["table", "record_key", "verdict", "golden_record", "merge_into", "action",
                     "recommended_verdict", "accepted_as_recommended", "reviewer", "decided_at", "run_id"])
    for table, decisions in sorted(data.items()):
        for entry in sorted(decisions.values(), key=lambda e: (e.get("merge_into") or e["key"], e["key"])):
            final = entry["verdict"] in ("UNIQUE", "DUPLICATE")
            recommended = entry.get("recommended_verdict") if final else None
            writer.writerow([table, entry["key"], entry["verdict"], "yes" if entry.get("survivor") else "",
                             entry.get("merge_into") or "", entry.get("action") or "", recommended or "",
                             "" if not recommended else ("yes" if recommended == entry["verdict"] else "no"),
                             entry.get("reviewer") or "", entry.get("decided_at", ""), entry.get("run_id", "")])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{client_id}_duplicate_decisions.csv"'})


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
    if body.max_repair_rounds is not None:
        args += ["--max-repair-rounds", str(body.max_repair_rounds)]
    if body.no_cache:
        args.append("--no-cache")
    if body.duplicates_only:
        args.append("--duplicates-only")
    args += ["--llm-provider", body.llm_provider]
    return args


@app.post("/api/jobs/run-profiling")
@app.post("/api/jobs/run-explorer")
def run_explorer_endpoint(body: RunExplorerRequest):
    """Start a profiling run in the background; returns a job_id at once. Poll
    /api/jobs/{job_id} (status, run_id, outputs) or read the profiling.completed event.
    /run-profiling is the name the pipeline orchestrator uses; /run-explorer is the UI's."""
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
