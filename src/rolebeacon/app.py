from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .company import CompanyResearchService
from .config import Settings
from .database import Database
from .domain import CollectedJob, JobStatus
from .llm import LlmClient, LlmUnavailable
from .profile import SearchPreferencesV1, country_catalog
from .services import ArtifactService, ProfileValidationError, cover_letter_recommendation
from .setup import LocalModelService, SetupService
from .sync import Scheduler, SyncService


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.load()
    app_settings.ensure_directories()
    database = Database(app_settings.database_path)
    database.initialize()
    llm = LlmClient(app_settings)
    sync_service = SyncService(app_settings, database, llm)
    artifacts = ArtifactService(app_settings, database, llm)
    company_research = CompanyResearchService(app_settings, database, llm)
    scheduler = Scheduler(sync_service, app_settings.sync_interval_seconds)
    setup_service = SetupService(app_settings)
    local_models = LocalModelService(app_settings)
    templates = Jinja2Templates(directory=app_settings.resource_dir / "templates")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if app_settings.auto_sync and app_settings.setup_complete and app_settings.activated:
            scheduler.start(run_immediately=True)
        try:
            yield
        finally:
            await scheduler.stop()

    app = FastAPI(title="RoleBeacon", version="0.2.0", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=app_settings.resource_dir / "static"), name="static")
    app.state.settings = app_settings
    app.state.database = database
    app.state.sync_service = sync_service
    app.state.artifacts = artifacts
    app.state.company_research = company_research
    app.state.setup_service = setup_service

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next: Any) -> Response:
        host = request.url.hostname or ""
        allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver", app_settings.host}
        if host not in allowed_hosts:
            return JSONResponse({"detail": "RoleBeacon accepts requests only from its configured local host"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlsplit

                if (urlsplit(origin).hostname or "") not in allowed_hosts:
                    return JSONResponse({"detail": "Cross-origin state changes are not allowed"}, status_code=403)
        return await call_next(request)

    def page_context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "sync": sync_service.status.to_dict(),
            "routes": app_settings.load_strategies(),
            "setup_complete": app_settings.setup_complete,
            **values,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        if not app_settings.setup_complete:
            return RedirectResponse("/setup", status_code=307)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            page_context(
                request,
                stats=database.dashboard_stats(),
                jobs=database.list_jobs(min_score=65, exclude_ineligible=True, limit=15),
                sources=database.list_sources(),
            ),
        )

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(
        request: Request,
        route: str = "",
        job_status: str = "",
        source: str = "",
        q: str = "",
        min_score: int = 0,
    ) -> HTMLResponse:
        try:
            jobs = database.list_jobs(route=route, status=job_status, source=source, query=q, min_score=min_score, limit=250)
        except Exception as error:
            jobs = []
            query_error = str(error)
        else:
            query_error = ""
        return templates.TemplateResponse(
            request,
            "jobs.html",
            page_context(
                request,
                jobs=jobs,
                filters={"route": route, "status": job_status, "source": source, "q": q, "min_score": min_score},
                query_error=query_error,
            ),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: int) -> HTMLResponse:
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        recommended, recommendation_reason = cover_letter_recommendation(job)
        application = next((item for item in database.list_applications() if item["job_id"] == job_id), None)
        return templates.TemplateResponse(
            request,
            "job-detail.html",
            page_context(
                request,
                job=job,
                application=application,
                cover_letter_recommended=recommended,
                cover_letter_reason=recommendation_reason,
            ),
        )

    @app.get("/applications", response_class=HTMLResponse)
    async def applications_page(request: Request) -> HTMLResponse:
        applications = database.list_applications()
        columns: dict[str, list[dict[str, Any]]] = {
            key: [] for key in ("saved", "preparing", "ready", "applied", "interview", "rejected", "offer")
        }
        for application in applications:
            columns.setdefault(application["status"], []).append(application)
        return templates.TemplateResponse(
            request,
            "applications.html",
            page_context(request, columns=columns),
        )

    @app.get("/sources", response_class=HTMLResponse)
    async def sources_page(request: Request) -> HTMLResponse:
        configured = {source.id: source for source in app_settings.load_sources()}
        states = {state["source_id"]: state for state in database.list_sources()}
        rows = []
        for source_id, config in configured.items():
            rows.append({"config": config, "state": states.get(source_id, {})})
        return templates.TemplateResponse(request, "sources.html", page_context(request, sources=rows))

    @app.get("/duplicates", response_class=HTMLResponse)
    async def duplicates_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "duplicates.html",
            page_context(request, candidates=database.list_duplicate_candidates()),
        )

    @app.get("/imports", response_class=HTMLResponse)
    async def imports_page(request: Request, imported: int = 0) -> HTMLResponse:
        return templates.TemplateResponse(request, "imports.html", page_context(request, imported=imported))

    @app.get("/companies", response_class=HTMLResponse)
    async def companies_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "companies.html",
            page_context(request, companies=database.list_companies()),
        )

    @app.get("/companies/{company_id}", response_class=HTMLResponse)
    async def company_detail(request: Request, company_id: int) -> HTMLResponse:
        company = database.get_company(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        return templates.TemplateResponse(
            request,
            "company-detail.html",
            page_context(request, company=company),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: bool = False) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "settings.html",
            page_context(request, profile=app_settings.load_search_profile(), settings=app_settings, saved=saved),
        )

    @app.post("/settings")
    async def update_settings(
        target_roles: Annotated[str, Form()],
        preferred_skills: Annotated[str, Form()] = "",
        preferred_domains: Annotated[str, Form()] = "",
        priority_companies: Annotated[str, Form()] = "",
        company_watchlist: Annotated[str, Form()] = "",
        company_blocklist: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        profile = app_settings.load_search_profile()
        profile["target_roles"] = _lines(target_roles)
        profile["preferred_skills"] = _lines(preferred_skills)
        profile["preferred_domains"] = _lines(preferred_domains)
        profile["priority_companies"] = _lines(priority_companies)
        profile["company_watchlist"] = _lines(company_watchlist)
        profile["company_blocklist"] = _lines(company_blocklist)
        validated = SearchPreferencesV1.model_validate(profile)
        app_settings.save_search_profile(validated.model_dump(mode="json"))
        return RedirectResponse("/settings?saved=true", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "setup.html",
            page_context(
                request,
                setup=setup_service.status(),
                schemas=setup_service.schemas(),
                sources=app_settings.load_sources(),
                countries=country_catalog(),
            ),
        )

    @app.get("/api/setup/status")
    async def setup_status() -> dict[str, Any]:
        return setup_service.status()

    @app.get("/api/schemas/candidate-profile")
    async def setup_schema() -> dict[str, Any]:
        return setup_service.schemas()

    @app.post("/api/setup/profile/validate")
    async def validate_setup_profile(request: Request) -> dict[str, Any]:
        return setup_service.validate_profile(await _payload(request))

    @app.post("/api/setup/validate")
    async def validate_setup_payload(request: Request) -> dict[str, Any]:
        return setup_service.validate_setup_payload(await _payload(request))

    @app.post("/api/setup/plan")
    async def plan_setup_with_llm(request: Request) -> dict[str, Any]:
        try:
            return await setup_service.plan_with_llm(await _payload(request))
        except (LlmUnavailable, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/setup/model/discover")
    async def discover_local_model(request: Request) -> dict[str, Any]:
        payload = await _payload(request)
        return await local_models.discover(str(payload.get("base_url", "http://127.0.0.1:11434/v1")))

    @app.post("/api/setup/model/test")
    async def test_local_model(request: Request) -> dict[str, Any]:
        payload = await _payload(request)
        return await local_models.test_endpoint(
            base_url=str(payload.get("base_url", "http://127.0.0.1:11434/v1")),
            model=str(payload.get("model", "qwen3:8b")),
            api_key=str(payload.get("api_key", "")),
        )

    @app.post("/api/setup/model/start")
    async def start_local_model() -> dict[str, Any]:
        try:
            return local_models.start_ollama()
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/setup/model/pull")
    async def pull_local_model(request: Request) -> dict[str, Any]:
        payload = await _payload(request)
        try:
            return await local_models.pull_ollama_model(str(payload.get("model", "qwen3:8b")))
        except (RuntimeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/setup/complete")
    async def complete_setup(request: Request) -> dict[str, Any]:
        nonlocal app_settings, llm
        try:
            app_settings = setup_service.complete(await _payload(request))
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        sync_service.settings = app_settings
        llm = LlmClient(app_settings)
        sync_service.llm = llm
        artifacts.settings = app_settings
        artifacts.llm = llm
        company_research.settings = app_settings
        company_research.llm = llm
        local_models.settings = app_settings
        app.state.settings = app_settings
        if app_settings.activated and app_settings.auto_sync:
            scheduler.start(run_immediately=True)
        return {"completed": True, "activated": app_settings.activated, "redirect": "/"}

    @app.post("/api/sync", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_sync(background_tasks: BackgroundTasks) -> dict[str, Any]:
        if not app_settings.setup_complete or not app_settings.activated:
            raise HTTPException(status_code=409, detail="Complete and activate setup before syncing")
        if sync_service.status.running:
            return {"accepted": False, "reason": "already_running", "status": sync_service.status.to_dict()}
        background_tasks.add_task(sync_service.run)
        return {"accepted": True}

    @app.get("/api/sync/status")
    async def sync_status() -> dict[str, Any]:
        return sync_service.status.to_dict()

    @app.get("/api/model/status")
    async def model_status() -> dict[str, Any]:
        return await llm.health()

    @app.get("/api/jobs")
    async def list_jobs_api(
        route: str = "",
        job_status: str = Query("", alias="status"),
        source: str = "",
        q: str = "",
        min_score: int = 0,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return {
            "jobs": database.list_jobs(
                route=route, status=job_status, source=source, query=q,
                min_score=min_score, limit=limit, offset=offset,
            )
        }

    @app.post("/api/jobs/{job_id}/feedback")
    async def feedback(job_id: int, request: Request) -> Response:
        payload = await _payload(request)
        try:
            feedback_status = JobStatus(payload.get("status", ""))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="Invalid feedback status") from error
        if not database.get_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        database.save_feedback(job_id, feedback_status, str(payload.get("reason", "")))
        if _wants_html(request):
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        return JSONResponse({"job_id": job_id, "status": feedback_status.value})

    @app.post("/api/jobs/{job_id}/resume")
    async def generate_resume(job_id: int, request: Request) -> Response:
        try:
            path = await artifacts.generate_resume(job_id)
        except ProfileValidationError as error:
            raise HTTPException(status_code=409, detail={"message": "Candidate profile is inconsistent", "issues": error.issues}) from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        if _wants_html(request):
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        return JSONResponse({"job_id": job_id, "resume_path": str(path)})

    @app.post("/api/jobs/{job_id}/cover-letter")
    async def generate_cover_letter(job_id: int, request: Request) -> Response:
        try:
            path = await artifacts.generate_cover_letter(job_id)
        except ProfileValidationError as error:
            raise HTTPException(status_code=409, detail={"message": "Candidate profile is inconsistent", "issues": error.issues}) from error
        except LlmUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if _wants_html(request):
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        return JSONResponse({"job_id": job_id, "cover_letter_path": str(path)})

    @app.post("/api/jobs/{job_id}/prepare-application")
    async def prepare_application(job_id: int, request: Request) -> Response:
        try:
            path = artifacts.prepare_application(job_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if _wants_html(request):
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        return JSONResponse({"job_id": job_id, "packet_path": str(path), "browser_opened": app_settings.open_browser})

    @app.post("/api/jobs/{job_id}/research-company")
    async def research_company(job_id: int, request: Request) -> Response:
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        try:
            company_id = await company_research.research(str(job["company"]))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (LlmUnavailable, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if _wants_html(request):
            return RedirectResponse(f"/companies/{company_id}", status_code=303)
        return JSONResponse({"job_id": job_id, "company_id": company_id})

    @app.get("/api/applications")
    async def applications_api() -> dict[str, Any]:
        return {"applications": database.list_applications()}

    @app.get("/api/sources/metrics")
    async def source_metrics_api() -> dict[str, Any]:
        return {"sources": database.list_sources(), "api_usage": database.list_api_usage()}

    @app.get("/api/duplicates")
    async def duplicates_api() -> dict[str, Any]:
        return {"candidates": database.list_duplicate_candidates()}

    @app.post("/api/duplicates/{candidate_id}/dismiss")
    async def dismiss_duplicate(candidate_id: int, request: Request) -> Response:
        database.dismiss_duplicate(candidate_id)
        if _wants_html(request):
            return RedirectResponse("/duplicates", status_code=303)
        return JSONResponse({"candidate_id": candidate_id, "status": "dismissed"})

    @app.post("/api/duplicates/{candidate_id}/merge")
    async def merge_duplicate(candidate_id: int, request: Request) -> Response:
        payload = await _payload(request)
        keep_job_id = int(payload["keep_job_id"]) if payload.get("keep_job_id") else None
        try:
            winner = database.merge_duplicate(candidate_id, keep_job_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if _wants_html(request):
            return RedirectResponse("/duplicates", status_code=303)
        return JSONResponse({"candidate_id": candidate_id, "status": "merged", "job_id": winner})

    @app.post("/api/imports/preview")
    async def preview_import(request: Request) -> dict[str, Any]:
        return {"preview": infer_manual_job(await _payload(request))}

    @app.post("/api/imports")
    async def import_job(request: Request) -> Response:
        values = infer_manual_job(await _payload(request))
        missing = [key for key in ("title", "company", "url") if not values.get(key)]
        if missing:
            raise HTTPException(status_code=422, detail=f"Missing required fields: {', '.join(missing)}")
        source_job_id = hashlib.sha256(str(values["url"]).encode()).hexdigest()
        job_id, _ = database.upsert_job(
            CollectedJob(
                source="manual", source_job_id=source_job_id, title=str(values["title"]),
                company=str(values["company"]), location=str(values.get("location", "")),
                description=str(values.get("description", "")), url=str(values["url"]),
                apply_url=str(values["url"]), remote_scope=str(values.get("remote_scope", "")),
                published_at=datetime.now(UTC), metadata={"manual_import": True},
            ),
            source_priority=90,
        )
        if _wants_html(request):
            return RedirectResponse(f"/imports?imported={job_id}", status_code=303)
        return JSONResponse({"job_id": job_id}, status_code=201)

    @app.get("/api/companies")
    async def companies_api() -> dict[str, Any]:
        return {"companies": database.list_companies()}

    @app.get("/api/companies/{company_id}")
    async def company_api(company_id: int) -> dict[str, Any]:
        company = database.get_company(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        return company

    @app.get("/artifacts/{job_id}/{filename}")
    async def artifact(job_id: int, filename: str) -> FileResponse:
        allowed = {"resume.pdf", "resume.html", "resume.json", "cover-letter.txt", "cover-letter.html", "application-packet.json", "job-description.txt"}
        if filename not in allowed:
            raise HTTPException(status_code=404, detail="Artifact not found")
        path = (app_settings.data_dir / "applications" / str(job_id) / filename).resolve()
        expected_root = (app_settings.data_dir / "applications" / str(job_id)).resolve()
        if path.parent != expected_root or not path.exists():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(path)

    return app


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


async def _payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    return dict(await request.form())


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def infer_manual_job(payload: dict[str, Any]) -> dict[str, str]:
    text = str(payload.get("text", "")).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    url = str(payload.get("url", "")).strip()
    if not url:
        url = next((word.rstrip(".,)") for word in text.split() if word.startswith(("http://", "https://"))), "")
    return {
        "title": str(payload.get("title", "")).strip() or (lines[0] if lines else ""),
        "company": str(payload.get("company", "")).strip() or (lines[1] if len(lines) > 1 else ""),
        "location": str(payload.get("location", "")).strip(),
        "remote_scope": str(payload.get("remote_scope", "")).strip(),
        "url": url,
        "description": str(payload.get("description", "")).strip() or text,
    }
