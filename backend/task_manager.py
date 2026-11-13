import asyncio
import json
import uuid
import gc
from typing import Dict, Any, Optional, List, Set
from datetime import datetime

from backend.maps_scraper import scrape_google_maps
from backend.contact_miner import enrich_firm_contact
from backend.people_miner import mine_affiliated_people
from backend.database import (
    upsert_law_firm,
    update_firm_enrichment,
    get_existing_firm_enrichment,
    insert_affiliated_person,
    get_db_stats,
    create_run,
    finish_run,
    set_latest_run_id
)

class JobState:
    def __init__(self, job_id: str, query: str, location: str, max_results: int, session_id: Optional[str] = None):
        self.job_id = job_id
        self.session_id = session_id
        self.query = query
        self.location = location
        self.max_results = max_results
        self.status = "running" # running, completed, stopped, error
        self.end_reason = "running"
        self.started_at = datetime.utcnow().isoformat()
        self.completed_at: Optional[str] = None
        self.discovered_count = 0
        self.new_count = 0
        self.enriched_count = 0
        self.emails_found_count = 0
        self.logs: List[str] = []
        self.cancel_requested = False
        self.subscribers: Set[asyncio.Queue] = set()

    def add_log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.logs.append(entry)
        if len(self.logs) > 300:
            self.logs.pop(0)

class TaskManager:
    def __init__(self):
        self.jobs: Dict[str, JobState] = {}
        self.active_job_id: Optional[str] = None
        self._lock = asyncio.Lock()

    def get_job(self, job_id: str) -> Optional[JobState]:
        return self.jobs.get(job_id)

    def get_active_job(self) -> Optional[JobState]:
        if self.active_job_id:
            return self.jobs.get(self.active_job_id)
        return None

    async def broadcast(self, job: JobState, event_type: str, data: Dict[str, Any]):
        payload = {
            "event": event_type,
            "data": data,
            "job_id": job.job_id,
            "run_id": job.job_id,
            "status": job.status,
            "end_reason": job.end_reason,
            "discovered": job.discovered_count,
            "new_count": job.new_count,
            "enriched": job.enriched_count,
            "emails_found": job.emails_found_count,
            "max_results": job.max_results,
            "timestamp": datetime.utcnow().isoformat()
        }
        dead_subscribers = set()
        for queue in list(job.subscribers):
            try:
                queue.put_nowait(payload)
            except Exception:
                dead_subscribers.add(queue)
        for dead in dead_subscribers:
            job.subscribers.discard(dead)

    async def start_job(self, query: str, location: str, max_results: int = 25, session_id: Optional[str] = None) -> str:
        async with self._lock:
            # Stop any existing running job
            if self.active_job_id and self.active_job_id in self.jobs:
                current = self.jobs[self.active_job_id]
                if current.status == "running":
                    current.cancel_requested = True
                    current.status = "stopped"
                    current.end_reason = "stopped"

            job_id = f"run_{str(uuid.uuid4())[:8]}"
            job = JobState(job_id, query, location, max_results, session_id=session_id)
            self.jobs[job_id] = job
            self.active_job_id = job_id

            create_run(job_id, location, max_results, session_id=session_id)
            set_latest_run_id(job_id, session_id=session_id)

            asyncio.create_task(self._run_job(job))
            return job_id

    async def stop_job(self, job_id: str):
        job = self.jobs.get(job_id)
        if job and job.status == "running":
            job.cancel_requested = True
            job.status = "stopped"
            job.end_reason = "stopped"
            job.completed_at = datetime.utcnow().isoformat()
            stop_msg = f"Search paused by user: {job.discovered_count} matches found."
            job.add_log(stop_msg)
            finish_run(job.job_id, "stopped", job.discovered_count, job.new_count, job.emails_found_count)
            await self.broadcast(job, "stopped", {
                "message": stop_msg,
                "end_reason": "stopped",
                "stats": get_db_stats(run_id=job.job_id),
                "all_time_stats": get_db_stats(run_id=None)
            })

    async def _run_job(self, job: JobState):
        job.add_log(f"Starting directory collection for '{job.query}' in '{job.location}' (Target: {job.max_results})")
        await self.broadcast(job, "started", {
            "query": job.query,
            "location": job.location,
            "max_results": job.max_results,
            "run_id": job.job_id
        })

        def is_cancelled() -> bool:
            return job.cancel_requested

        def on_scraper_log(msg: str):
            job.add_log(msg)
            asyncio.create_task(self.broadcast(job, "log", {"message": msg}))

        enrichment_queue: asyncio.Queue = asyncio.Queue()

        async def enrichment_worker():
            """Worker to crawl website for emails and verify domain MX."""
            while True:
                item = await enrichment_queue.get()
                if item is None:
                    enrichment_queue.task_done()
                    break

                record_id = item["record_id"]
                firm_name = item["name"]
                website = item.get("website")
                already_in_db = item.get("already_in_db", False)

                try:
                    if is_cancelled():
                        continue

                    # Check if this firm was already enriched in the database
                    if already_in_db:
                        cached = get_existing_firm_enrichment(record_id)
                        if cached:
                            firm_cache, people_cache = cached
                            job.enriched_count += 1
                            if firm_cache.get("email"):
                                job.emails_found_count += 1
                                job.add_log(f"Contact email found: {firm_cache['email']} ({firm_cache.get('email_type')}) for {firm_name}")
                            elif firm_cache.get("enrichment_status") == "contact_form_only":
                                job.add_log(f"Contact intake form found for {firm_name}")
                            else:
                                job.add_log(f"No direct email found for {firm_name} ({firm_cache.get('enrichment_status')})")

                            await self.broadcast(job, "firm_enriched", {
                                "id": record_id,
                                "name": firm_name,
                                "website": firm_cache.get("website") or website,
                                "email": firm_cache.get("email"),
                                "email_type": firm_cache.get("email_type"),
                                "mx_valid": firm_cache.get("mx_valid"),
                                "secondary_emails": firm_cache.get("secondary_emails") or [],
                                "status": firm_cache.get("enrichment_status") or "completed",
                                "contact_form_url": firm_cache.get("contact_form_url")
                            })
                            if people_cache:
                                job.add_log(f"Mapped {len(people_cache)} affiliated practitioners for {firm_name}")
                                await self.broadcast(job, "firm_people_updated", {
                                    "firm_id": record_id,
                                    "people": people_cache
                                })
                            continue

                    await self.broadcast(job, "firm_enriching", {
                        "id": record_id,
                        "name": firm_name,
                        "website": website,
                        "current": job.enriched_count + 1,
                        "total": max(job.discovered_count, job.max_results)
                    })

                    if not website:
                        try:
                            update_firm_enrichment(record_id, None, None, None, [], "no_website")
                        except Exception:
                            pass
                        job.enriched_count += 1
                        await self.broadcast(job, "firm_enriched", {
                            "id": record_id,
                            "name": firm_name,
                            "email": None,
                            "email_type": None,
                            "mx_valid": None,
                            "secondary_emails": [],
                            "status": "no_website"
                        })
                    else:
                        job.add_log(f"Inspecting contact channels for {firm_name} ({website})")
                        try:
                            primary_email, email_type, mx_valid, secondary_list, status, actual_web, contact_form = await enrich_firm_contact(website)
                            resolved_site = actual_web if (actual_web and not "google.com" in actual_web) else website
                            try:
                                update_firm_enrichment(
                                    record_id=record_id,
                                    email=primary_email,
                                    email_type=email_type,
                                    mx_valid=mx_valid,
                                    secondary_emails=secondary_list,
                                    status=status,
                                    resolved_website=resolved_site,
                                    contact_form_url=contact_form
                                )
                            except Exception as dbe:
                                job.add_log(f"Warning: Database save failed for {firm_name}: {str(dbe)}")
                            job.enriched_count += 1

                            if primary_email:
                                job.emails_found_count += 1
                                mx_str = "accepts mail" if mx_valid else "no MX record"
                                job.add_log(f"Contact email found: {primary_email} ({email_type}, domain {mx_str}) for {firm_name}")
                            elif status == "contact_form_only":
                                job.add_log(f"Contact intake form found for {firm_name} ({contact_form or 'web form'})")
                            else:
                                job.add_log(f"No direct email found for {firm_name} ({status})")

                            await self.broadcast(job, "firm_enriched", {
                                "id": record_id,
                                "name": firm_name,
                                "website": resolved_site,
                                "email": primary_email,
                                "email_type": email_type,
                                "mx_valid": mx_valid,
                                "secondary_emails": secondary_list,
                                "status": status,
                                "contact_form_url": contact_form
                            })
                        except Exception as err:
                            resolved_site = website
                            try:
                                update_firm_enrichment(record_id, None, None, None, [], "failed")
                            except Exception:
                                pass
                            job.enriched_count += 1
                            job.add_log(f"Website enrichment failed for {firm_name}: {str(err)}")
                            await self.broadcast(job, "firm_enriched", {
                                "id": record_id,
                                "name": firm_name,
                                "email": None,
                                "email_type": None,
                                "mx_valid": None,
                                "secondary_emails": [],
                                "status": "failed",
                                "contact_form_url": None
                            })

                    # Stage 2B: Mine Affiliated People & LinkedIn profiles
                    try:
                        people_list = await mine_affiliated_people(
                            firm_name=firm_name,
                            website_url=website,
                            city=item.get("city", ""),
                            state=item.get("state", "")
                        )
                        saved_people = []
                        for p in people_list:
                            try:
                                p_id = insert_affiliated_person(
                                    firm_id=record_id,
                                    run_id=job.job_id,
                                    name=p["name"],
                                    title=p.get("title"),
                                    seniority=p.get("seniority"),
                                    email=p.get("email"),
                                    linkedin_url=p.get("linkedin_url"),
                                    source=p.get("source", "website_directory"),
                                    is_direct_linkedin=p.get("is_direct_linkedin", False),
                                    source_url=p.get("source_url"),
                                    profile_url=p.get("profile_url"),
                                    source_layer=p.get("source_layer", "other"),
                                    confidence=p.get("confidence", "high"),
                                    score=p.get("score", 0),
                                    evidence=json.dumps(p.get("evidence", [])) if isinstance(p.get("evidence"), list) else p.get("evidence")
                                )
                                p_record = dict(p)
                                p_record["id"] = p_id
                                p_record["firm_id"] = record_id
                                saved_people.append(p_record)
                            except Exception:
                                continue

                        if saved_people:
                            job.add_log(f"Mapped {len(saved_people)} affiliated practitioners for {firm_name} ({sum(1 for p in saved_people if p.get('linkedin_url'))} LinkedIn links)")
                            await self.broadcast(job, "firm_people_updated", {
                                "firm_id": record_id,
                                "people": saved_people
                            })
                    except Exception as pe:
                        job.add_log(f"Notice: Practitioner mapping skipped for {firm_name}: {str(pe)}")

                except Exception as worker_err:
                    job.add_log(f"Notice: Enrichment task error for {firm_name}: {str(worker_err)}")
                finally:
                    enrichment_queue.task_done()

        items_to_enrich = []
        workers = []

        try:
            # Stage 1: Google Maps Discovery (Chromium browser running with low-memory args)
            listing_gen, get_scraper_end_reason = await scrape_google_maps(
                query=job.query,
                location=job.location,
                max_results=job.max_results,
                is_cancelled=is_cancelled,
                log_callback=on_scraper_log
            )

            async for firm_dict in listing_gen:
                if is_cancelled():
                    break

                job.discovered_count += 1
                record_id, already_in_db = upsert_law_firm(firm_dict, current_run_id=job.job_id)
                if not already_in_db:
                    job.new_count += 1
                firm_dict["id"] = record_id
                firm_dict["run_id"] = job.job_id
                firm_dict["already_in_db"] = already_in_db
                firm_dict["was_new"] = not already_in_db

                if already_in_db:
                    job.add_log(f"Record matched existing database entry: {firm_dict['name']}")

                # Stream discovered item to UI immediately
                await self.broadcast(job, "firm_discovered", firm_dict)

                items_to_enrich.append({
                    "record_id": record_id,
                    "name": firm_dict["name"],
                    "website": firm_dict.get("website"),
                    "city": firm_dict.get("city", ""),
                    "state": firm_dict.get("state", ""),
                    "already_in_db": already_in_db
                })

            # Stage 1 Complete: Chromium process has closed. Free browser buffers from RAM immediately.
            gc.collect()

            # Stage 2: Deep Website & Practitioner Enrichment (runs after Chromium is closed)
            if not is_cancelled() and items_to_enrich:
                job.add_log(f"Stage 1 complete. Beginning website inspection & practitioner discovery for {len(items_to_enrich)} listings...")
                for item in items_to_enrich:
                    await enrichment_queue.put(item)
                for _ in range(2):
                    await enrichment_queue.put(None)

                workers = [asyncio.create_task(enrichment_worker()) for _ in range(2)]
                await asyncio.gather(*workers)
                gc.collect()

            scraper_end = get_scraper_end_reason()
            if job.cancel_requested:
                job.status = "stopped"
                job.end_reason = "stopped"
                final_msg = f"Search paused by user: {job.discovered_count} matches found."
            else:
                job.status = "completed"
                job.end_reason = scraper_end
                if scraper_end == "target_reached":
                    final_msg = f"Target reached: {job.discovered_count} matches found."
                elif scraper_end == "search_exhausted":
                    final_msg = f"Search exhausted: {job.discovered_count} matches found (target was {job.max_results})."
                elif scraper_end == "timeout":
                    final_msg = f"Search timed out after {job.discovered_count} matches."
                elif scraper_end == "rate_limited":
                    final_msg = f"Search index rate-limited after {job.discovered_count} matches."
                else:
                    final_msg = f"Search completed with reason: {scraper_end} ({job.discovered_count} matches)."

            job.completed_at = datetime.utcnow().isoformat()
            job.add_log(final_msg)

            finish_run(job.job_id, job.end_reason, job.discovered_count, job.new_count, job.emails_found_count)

            await self.broadcast(job, "completed", {
                "message": final_msg,
                "end_reason": job.end_reason,
                "total_discovered": job.discovered_count,
                "new_count": job.new_count,
                "total_emails": job.emails_found_count,
                "stats": get_db_stats(run_id=job.job_id),
                "all_time_stats": get_db_stats(run_id=None)
            })

        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            job.status = "error"
            job.end_reason = "error"
            job.completed_at = datetime.utcnow().isoformat()
            err_msg = f"Search error encountered: {str(e) or type(e).__name__}"
            job.add_log(err_msg)
            print(f"[ERROR in _run_job]:\n{tb_str}")
            finish_run(job.job_id, "error", job.discovered_count, job.new_count, job.emails_found_count)
            for _ in range(2):
                await enrichment_queue.put(None)
            await self.broadcast(job, "error", {
                "message": err_msg,
                "end_reason": "error",
                "stats": get_db_stats(run_id=job.job_id),
                "all_time_stats": get_db_stats(run_id=None)
            })

manager = TaskManager()
