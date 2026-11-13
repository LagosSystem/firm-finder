import os
import sys
import json
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from typing import Optional
from fastapi import FastAPI, Query, HTTPException, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from backend.models import ScrapeRequest
from backend.database import (
    init_db,
    get_law_firms,
    get_firm_people,
    get_db_stats,
    export_leads_csv,
    export_contacts_csv,
    get_db_connection,
    delete_firm,
    delete_person,
    get_latest_run_info,
    clear_latest_run,
    delete_everything,
    undo_action,
    purge_undo_snapshot,
    reset_session
)
from backend.task_manager import manager

app = FastAPI(
    title="Firm Finder",
    description="Law firm and contact directory builder (prototype)",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/health")
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "app": "Firm Finder", "version": "2.0.0"}

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Loading Firm Finder...</h1>")

@app.post("/api/search")
async def start_search(req: ScrapeRequest):
    if not req.location.strip():
        raise HTTPException(status_code=400, detail="Location is required.")
    # Always enforce constant "Law firms" keyword (Phase 2 Item 2)
    query_str = "Law firms"

    job_id = await manager.start_job(
        query=query_str,
        location=req.location.strip(),
        max_results=req.max_results,
        session_id=req.session_id
    )
    return {"status": "started", "job_id": job_id, "run_id": job_id}

@app.post("/api/stop/{job_id}")
async def stop_search(job_id: str):
    await manager.stop_job(job_id)
    return {"status": "stopped", "job_id": job_id}

@app.get("/api/stream/{job_id}")
async def stream_job_events(job_id: str):
    job = manager.get_job(job_id)
    if not job:
        # Check if run exists in database or was from a previous server session
        stats = get_db_stats(run_id=job_id)
        async def closed_generator():
            payload = {
                "event": "completed",
                "data": {
                    "message": f"Run session '{job_id}' concluded.",
                    "end_reason": "completed",
                    "stats": stats,
                    "all_time_stats": get_db_stats(run_id=None)
                },
                "job_id": job_id,
                "run_id": job_id,
                "status": "completed",
                "end_reason": "completed",
                "discovered": stats.get("total_leads", 0),
                "enriched": stats.get("with_website", 0),
                "emails_found": stats.get("with_email", 0),
                "max_results": stats.get("total_leads", 0)
            }
            yield f"data: {json.dumps(payload)}\n\n"
        return StreamingResponse(
            closed_generator(),
            media_type="text/event-stream"
        )

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.add(queue)

        init_payload = {
            "event": "sync",
            "data": {
                "logs": job.logs[-50:],
                "stats": get_db_stats(run_id=job.job_id),
                "all_time_stats": get_db_stats(run_id=None),
                "end_reason": job.end_reason
            },
            "job_id": job.job_id,
            "run_id": job.job_id,
            "status": job.status,
            "end_reason": job.end_reason,
            "discovered": job.discovered_count,
            "enriched": job.enriched_count,
            "emails_found": job.emails_found_count,
            "max_results": job.max_results
        }
        yield f"data: {json.dumps(init_payload)}\n\n"

        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"data: {json.dumps(payload)}\n\n"
                    if payload.get("event") in ["completed", "stopped", "error"]:
                        break
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            job.subscribers.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@app.get("/api/leads")
async def list_leads(
    run_id: Optional[str] = Query(None, description="Filter by run_id or 'all'"),
    session_id: Optional[str] = Query(None, description="Client session identifier"),
    search: Optional[str] = Query(None),
    only_with_email: bool = Query(False),
    hide_low_relevance: bool = Query(False),
    limit: int = Query(500, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    leads = get_law_firms(
        run_id=run_id,
        session_id=session_id,
        search=search,
        only_with_email=only_with_email,
        hide_low_relevance=hide_low_relevance,
        limit=limit,
        offset=offset
    )
    run_stats = get_db_stats(run_id=run_id, session_id=session_id)
    all_time_stats = get_db_stats(run_id=None, session_id=session_id)
    return {
        "leads": leads,
        "count": len(leads),
        "stats": run_stats,
        "all_time_stats": all_time_stats,
        "active_run_id": run_id
    }

@app.get("/api/stats")
async def get_stats(run_id: Optional[str] = Query(None), session_id: Optional[str] = Query(None)):
    return {
        "run_stats": get_db_stats(run_id=run_id, session_id=session_id),
        "all_time_stats": get_db_stats(run_id=None, session_id=session_id)
    }

@app.get("/api/export/csv")
async def export_csv(
    run_id: Optional[str] = Query(None),
    only_with_email: bool = Query(False),
    hide_low_relevance: bool = Query(False)
):
    csv_content = export_leads_csv(
        run_id=run_id,
        only_with_email=only_with_email,
        hide_low_relevance=hide_low_relevance
    )
    filename = f"firm_finder_directory_{run_id or 'all_time'}.csv"
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

@app.get("/api/firms/{firm_id}/people")
async def get_practitioners(firm_id: int):
    people = get_firm_people(firm_id)
    return {"firm_id": firm_id, "people": people, "count": len(people)}

@app.get("/api/export/contacts/csv")
async def export_contacts(run_id: Optional[str] = Query(None)):
    csv_content = export_contacts_csv(run_id=run_id)
    filename = f"firm_finder_practitioners_{run_id or 'all_time'}.csv"
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

@app.delete("/api/firms/{firm_id}")
async def remove_firm(firm_id: int):
    success = delete_firm(firm_id)
    return {"status": "deleted", "firm_id": firm_id, "success": success}

@app.delete("/api/people/{person_id}")
async def remove_person(person_id: int):
    success = delete_person(person_id)
    return {"status": "deleted", "person_id": person_id, "success": success}

@app.get("/api/runs/latest")
async def get_latest_run(session_id: Optional[str] = Query(None)):
    info = get_latest_run_info(session_id=session_id)
    return {"run": info}

@app.post("/api/session/new")
async def new_session(session_id: Optional[str] = Query(None)):
    reset_session(session_id=session_id)
    return {"status": "reset"}

@app.post("/api/runs/clear-latest")
async def clear_latest():
    info = get_latest_run_info()
    if not info:
        return {"status": "empty", "affected": 0, "token": ""}
    count, token = clear_latest_run(info["id"])
    return {"status": "cleared", "affected": count, "token": token, "run_id": info["id"]}

@app.post("/api/leads/clear-all")
async def clear_all_data():
    count, token = delete_everything()
    return {"status": "cleared", "affected": count, "token": token}

@app.post("/api/undo")
async def execute_undo(token: str = Query(...)):
    success = undo_action(token)
    return {"status": "undone" if success else "failed", "success": success}

@app.post("/api/undo/purge")
async def execute_purge(token: str = Query(...)):
    purge_undo_snapshot(token)
    return {"status": "purged"}

@app.post("/api/leads/clear")
async def clear_leads():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM law_firms")
    conn.commit()
    conn.close()
    return {"status": "cleared", "stats": get_db_stats()}

@app.get("/api/demo/mode")
async def get_demo_mode():
    from backend import database
    is_demo = "demo_leads.db" in str(database.DB_PATH)
    return {"demo": is_demo, "db_path": database.DB_PATH}

@app.post("/api/demo/enable")
async def enable_demo_mode():
    from backend import database
    demo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo_leads.db")
    if not os.path.exists(demo_path):
        from scripts.seed_demo_db import seed_database
        seed_database(demo_path)
    database.DB_PATH = demo_path
    database.init_db()
    return {"status": "ok", "demo": True, "db_path": database.DB_PATH}

@app.post("/api/demo/disable")
async def disable_demo_mode():
    from backend import database
    default_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leads.db")
    database.DB_PATH = default_path
    database.init_db()
    return {"status": "ok", "demo": False, "db_path": database.DB_PATH}

