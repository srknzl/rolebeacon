from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .collectors import description_blocks, plain_text, repair_text
from .company import CompanyResearchCoordinator, CompanyResearchService
from .config import Settings
from .database import JOB_SORTS, PIPELINE_COLUMNS, Database, JobFilters, company_key
from .domain import CollectedJob, JobStatus, SourceConfig
from .llm import LlmClient, LlmResponseRejected, LlmUnavailable
from .profile import country_catalog, relocation_region_options
from .scoring import INELIGIBLE_SCORE_CAP, dimension_metadata, location_requirement, seniority_level_options
from .services import ArtifactService, ProfileValidationError, cover_letter_recommendation
from .setup import LocalModelService, SetupService
from .source_catalog import SourceCatalog, SourceCatalogError
from .source_discovery import SourceDiscoveryError, SourceDiscoveryService, detect_source
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
    company_research_coordinator = CompanyResearchCoordinator(company_research)
    scheduler = Scheduler(sync_service, app_settings.sync_interval_seconds)
    setup_service = SetupService(app_settings)
    source_discovery = SourceDiscoveryService()
    source_catalog = SourceCatalog(app_settings)
    local_models = LocalModelService(app_settings)
    csrf_token = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=app_settings.resource_dir / "templates")
    templates.env.filters["repair_text"] = repair_text
    templates.env.filters["description_blocks"] = description_blocks
    templates.env.filters["location_requirement"] = location_requirement

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
    app.state.company_research_coordinator = company_research_coordinator
    app.state.setup_service = setup_service
    app.state.source_discovery = source_discovery
    app.state.source_catalog = source_catalog

    def guard_rejection(request: Request, detail: str) -> Response:
        if _wants_html(request):
            return templates.TemplateResponse(
                request,
                "error.html",
                page_context(request, error_title="Request blocked", error_detail=detail),
                status_code=403,
            )
        return JSONResponse({"detail": detail}, status_code=403)

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next: Any) -> Response:
        configured_hosts = {app_settings.host}
        if app_settings.host in {"127.0.0.1", "localhost", "::1"}:
            configured_hosts.update({"127.0.0.1", "localhost", "::1"})
        allowed_origins = {f"http://{host}:{app_settings.port}" for host in configured_hosts if host != "::1"}
        allowed_origins.add(f"http://[::1]:{app_settings.port}")
        # Starlette's in-process TestClient uses this synthetic peer and origin. A network
        # request cannot acquire that ASGI client identity, so the test-only origin never enters
        # the production allowlist merely because its Host header says "testserver".
        if request.client and request.client.host == "testclient":
            allowed_origins.add("http://testserver")
        request_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request_origin not in allowed_origins:
            return guard_rejection(request, "RoleBeacon accepts requests only from its configured local host")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in allowed_origins:
                return guard_rejection(request, "Cross-origin state changes are not allowed")
            supplied_token = request.headers.get("x-csrf-token", "")
            if not supplied_token and "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
                # Reading body() caches the bytes for the endpoint's later request.form() call.
                # Native browser forms cannot set headers, so their server-rendered hidden field
                # is the no-JavaScript equivalent of the fetch wrapper's X-CSRF-Token header.
                form_values = parse_qs((await request.body()).decode("utf-8", errors="replace"))
                supplied_token = form_values.get("csrf_token", [""])[-1]
            if (origin or request.headers.get("sec-fetch-site")) and not secrets.compare_digest(
                supplied_token, csrf_token
            ):
                return guard_rejection(request, "A valid CSRF token is required")
        return await call_next(request)

    def page_context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "sync": sync_service.status.to_dict(),
            "routes": app_settings.load_strategies(),
            "setup_complete": app_settings.setup_complete,
            # Every page states which engine produced what it shows, because the two modes
            # differ in what they can produce and whether a repeat run gives the same answer.
            "llm_enabled": app_settings.llm_enabled,
            "llm_model": app_settings.llm_model,
            "ineligible_score_cap": INELIGIBLE_SCORE_CAP,
            "csrf_token": csrf_token,
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
                jobs=database.list_jobs(
                    JobFilters(min_score=65, exclude_ineligible=True),
                    limit=int(app_settings.load_search_profile().get("daily_review_limit", 15)),
                ),
                sources=database.list_sources(),
            ),
        )

    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs_page(request: Request) -> HTMLResponse:
        values = dict(request.query_params)
        sources = app_settings.load_sources()
        preferences = app_settings.load_search_profile()
        filters = _job_filters_from_query(request.query_params, sources=sources, preferences=preferences)
        sort = values.get("sort", "decision_ready")
        if sort not in JOB_SORTS:
            sort = "decision_ready"
        page_size = _as_int(values.get("page_size"), 50)
        if page_size not in {10, 20, 50}:
            page_size = 50
        page = max(1, _as_int(values.get("page"), 1))
        try:
            jobs = database.list_jobs(filters, sort=sort, limit=page_size, offset=(page - 1) * page_size)
            total = database.count_jobs(filters)
            all_matches_total = database.count_jobs(replace(filters, hide_mismatched_titles=False))
        except Exception as error:
            jobs, total, all_matches_total = [], 0, 0
            query_error = str(error)
        else:
            query_error = ""
        hidden_title_count = max(0, all_matches_total - total)
        source_names = {item.id: item.name for item in sources}
        for job in jobs:
            job["source_name"] = source_names.get(job.get("primary_source_id") or "", "")
        return templates.TemplateResponse(
            request,
            "jobs.html",
            page_context(
                request,
                jobs=jobs,
                filters=filters,
                selected=values,
                sort=sort,
                total=total,
                all_matches_total=all_matches_total,
                hidden_title_count=hidden_title_count,
                page=page,
                page_size=page_size,
                page_count=max(1, -(-total // page_size)),
                active_chips=_active_filter_chips(
                    request.query_params, sources=sources, hidden_title_count=hidden_title_count
                ),
                sort_options=JOB_SORT_LABELS,
                technology_options=preferences.get("preferred_skills", []),
                sources=_source_filter_options(sources),
                query_error=query_error,
            ),
        )

    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request, i: int = Query(0, ge=0)) -> HTMLResponse:
        filters = JobFilters(status="bookmarked")
        total = database.count_jobs(filters)
        jobs = database.list_jobs(filters, sort="job_fit", limit=1, offset=i) if i < total else []
        return templates.TemplateResponse(
            request,
            "review.html",
            page_context(request, job=jobs[0] if jobs else None, index=i, total=total),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: int) -> HTMLResponse:
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        recommended, recommendation_reason = cover_letter_recommendation(job)
        application = next((item for item in database.list_applications() if item["job_id"] == job_id), None)
        cover_letter_text = None
        if application and application.get("cover_letter_path"):
            cover_letter_file = Path(application["cover_letter_path"])
            if cover_letter_file.exists():
                cover_letter_text = cover_letter_file.read_text(encoding="utf-8")
        return templates.TemplateResponse(
            request,
            "job-detail.html",
            page_context(
                request,
                job=job,
                application=application,
                cover_letter_recommended=recommended,
                cover_letter_reason=recommendation_reason,
                cover_letter_text=cover_letter_text,
                dimension_meta=dimension_metadata(app_settings.load_search_profile()),
            ),
        )

    @app.get("/applications", response_class=HTMLResponse)
    async def applications_page(request: Request) -> HTMLResponse:
        # The board is driven by jobs.status directly, so a bookmark-only job shows up without
        # ever generating a resume. The applications table is only consulted for the optional
        # "resume ready" badge on each card.
        applications_by_job = {row["job_id"]: row for row in database.list_applications()}
        columns: dict[str, list[dict[str, Any]]] = {}
        for column in PIPELINE_COLUMNS:
            jobs = database.list_jobs(JobFilters(status=column), sort="newest", limit=300)
            for job in jobs:
                application = applications_by_job.get(job["id"])
                job["resume_ready"] = bool(application and application["resume_path"])
            columns[column] = jobs
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
        preferences = app_settings.load_search_profile()
        priority_companies = list(
            dict.fromkeys(preferences.get("priority_companies", []) + preferences.get("company_watchlist", []))
        )
        coverage = []
        for company in priority_companies:
            matches = [source for source in configured.values() if source.company.casefold() == str(company).casefold()]
            coverage.append({"company": company, "sources": matches})
        return templates.TemplateResponse(
            request,
            "sources.html",
            page_context(request, sources=rows, coverage=coverage, source_catalog=source_catalog.view()),
        )

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
    async def settings_page(request: Request, saved: int = 0) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "setup.html",
            page_context(
                request,
                setup=setup_service.status(),
                schemas=setup_service.schemas(),
                sources=app_settings.load_sources(),
                source_catalog=source_catalog.view(),
                countries=country_catalog(),
                region_options=relocation_region_options(),
                seniority_levels=seniority_level_options(),
                editing=True,
                saved=bool(saved),
                company_search={
                    "provider": app_settings.company_search_provider,
                    "configured": bool(app_settings.company_search_api_key),
                    "registry_count": len(app_settings.load_company_registry()),
                },
                initial_setup=setup_service.saved_payload(),
            ),
        )

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
                source_catalog=source_catalog.view(),
                countries=country_catalog(),
                region_options=relocation_region_options(),
                seniority_levels=seniority_level_options(),
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

    @app.post("/api/setup/company-search")
    async def save_company_search(request: Request) -> dict[str, Any]:
        nonlocal app_settings
        payload = await _payload(request)
        app_settings = app_settings.save_company_search_key(str(payload.get("api_key", "")))
        company_research.settings = app_settings
        setup_service.settings = app_settings
        app.state.settings = app_settings
        return {
            "configured": bool(app_settings.company_search_api_key),
            "provider": app_settings.company_search_provider,
        }

    @app.post("/api/setup/complete")
    async def complete_setup(request: Request, return_to: str = "") -> dict[str, Any]:
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
        setup_service.settings = app_settings
        local_models.settings = app_settings
        source_catalog.settings = app_settings
        app.state.settings = app_settings
        if app_settings.activated and app_settings.auto_sync:
            scheduler.start(run_immediately=True)
        redirect = "/settings" if return_to == "/settings" else "/"
        return {"completed": True, "activated": app_settings.activated, "redirect": redirect}

    @app.post("/api/sync", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_sync(background_tasks: BackgroundTasks) -> dict[str, Any]:
        if not app_settings.setup_complete or not app_settings.activated:
            raise HTTPException(status_code=409, detail="Complete and activate setup before syncing")
        if sync_service.status.running:
            return {"accepted": False, "reason": "already_running", "status": sync_service.status.to_dict()}
        background_tasks.add_task(sync_service.run, False, True)
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
        sort: str = "decision_ready",
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        if sort not in JOB_SORTS:
            raise HTTPException(status_code=422, detail=f"sort must be one of: {', '.join(JOB_SORTS)}")
        filters = JobFilters(
            route=route, status=job_status, source_ids=(source,) if source else (), query=q, min_score=min_score
        )
        return {
            "jobs": database.list_jobs(filters, sort=sort, limit=limit, offset=offset),
            "total": database.count_jobs(filters),
        }

    @app.get("/api/companies/suggest")
    async def suggest_companies(q: str = "") -> dict[str, Any]:
        return {"companies": database.suggest_companies(q)}

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
        except LlmResponseRejected as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except LlmUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError) as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
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

    @app.post("/api/jobs/{job_id}/research-company/start", status_code=status.HTTP_202_ACCEPTED)
    async def start_company_research_for_job(job_id: int) -> dict[str, Any]:
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return company_research_coordinator.start(str(job["company"]))

    @app.post("/api/companies/{company_id}/research")
    async def refresh_company_research(company_id: int, request: Request) -> Response:
        company = database.get_company(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        try:
            refreshed_company_id = await company_research.research(str(company["name"]))
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (LlmUnavailable, RuntimeError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if _wants_html(request):
            return RedirectResponse(f"/companies/{refreshed_company_id}", status_code=303)
        return JSONResponse({"company_id": refreshed_company_id, "refreshed": True})

    @app.post("/api/companies/{company_id}/research/start", status_code=status.HTTP_202_ACCEPTED)
    async def start_company_research(company_id: int) -> dict[str, Any]:
        company = database.get_company(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        return company_research_coordinator.start(str(company["name"]))

    @app.get("/api/company-research/status")
    async def company_research_status() -> dict[str, Any]:
        return company_research_coordinator.status.to_dict()

    @app.get("/api/applications")
    async def applications_api() -> dict[str, Any]:
        return {"applications": database.list_applications()}

    @app.get("/api/sources/metrics")
    async def source_metrics_api() -> dict[str, Any]:
        return {"sources": database.list_sources(), "api_usage": database.list_api_usage()}

    @app.get("/api/source-packs")
    async def source_packs() -> dict[str, Any]:
        return source_catalog.view()

    @app.post("/api/source-packs/{pack_id}/install")
    async def install_source_pack(pack_id: str, request: Request) -> dict[str, Any]:
        payload = await _payload(request)
        if not isinstance(payload.get("enabled", False), bool):
            raise HTTPException(status_code=422, detail="enabled must be true or false")
        try:
            result = source_catalog.install(pack_id, enabled=bool(payload.get("enabled", False)))
        except SourceCatalogError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return result.to_dict()

    @app.post("/api/source-catalog/{entry_id}/install")
    async def install_catalog_source(entry_id: str, request: Request) -> Response:
        payload = await _payload(request)
        if not isinstance(payload.get("enabled", False), bool):
            raise HTTPException(status_code=422, detail="enabled must be true or false")
        try:
            source, created = source_catalog.install_entry(
                entry_id,
                enabled=bool(payload.get("enabled", False)),
            )
        except SourceCatalogError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JSONResponse(
            {"source": source.to_dict(), "created": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @app.post("/api/sources/discover")
    async def discover_source(request: Request) -> dict[str, Any]:
        if not app_settings.activated:
            raise HTTPException(status_code=409, detail="Activate setup before contacting a source for preview")
        payload = await _payload(request)
        try:
            preview = await source_discovery.preview(
                str(payload.get("careers_url", "")), str(payload.get("company", ""))
            )
        except SourceDiscoveryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                status_code=502,
                detail=f"The detected ATS endpoint returned HTTP {error.response.status_code}",
            ) from error
        except httpx.RequestError as error:
            raise HTTPException(status_code=502, detail=f"Could not reach the detected ATS endpoint: {error}") from error
        return preview.to_dict()

    @app.post("/api/sources")
    async def add_source(request: Request) -> Response:
        payload = await _payload(request)
        if "enabled" in payload and not isinstance(payload["enabled"], bool):
            raise HTTPException(status_code=422, detail="enabled must be true or false")
        try:
            source = detect_source(str(payload.get("careers_url", "")), str(payload.get("company", "")))
        except SourceDiscoveryError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        source.enabled = bool(payload.get("enabled", True))
        saved, created = app_settings.save_source(source)
        return JSONResponse(
            {"source": saved.to_dict(), "created": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @app.post("/api/sources/{source_id}/enabled")
    async def set_source_enabled(source_id: str, request: Request) -> dict[str, Any]:
        payload = await _payload(request)
        if not isinstance(payload.get("enabled"), bool):
            raise HTTPException(status_code=422, detail="enabled must be true or false")
        try:
            source = app_settings.set_source_enabled(source_id, bool(payload["enabled"]))
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"source": source.to_dict()}

    @app.get("/api/duplicates")
    async def duplicates_api() -> dict[str, Any]:
        return {"candidates": database.list_duplicate_candidates()}

    @app.post("/api/duplicates/{candidate_id}/dismiss")
    async def dismiss_duplicate(candidate_id: int, request: Request) -> Response:
        database.dismiss_duplicate(candidate_id)
        if _wants_html(request):
            return RedirectResponse("/duplicates", status_code=303)
        return JSONResponse({"candidate_id": candidate_id, "status": "dismissed"})

    @app.post("/api/duplicates/merge-exact")
    async def merge_exact_duplicates(request: Request) -> Response:
        result = database.merge_all_exact_duplicates()
        if _wants_html(request):
            return RedirectResponse("/duplicates", status_code=303)
        return JSONResponse(result)

    @app.post("/api/duplicates/{candidate_id}/merge")
    async def merge_duplicate(candidate_id: int, request: Request) -> Response:
        payload = await _payload(request)
        try:
            keep_job_id = int(payload["keep_job_id"]) if payload.get("keep_job_id") is not None else None
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="keep_job_id must be an integer") from error
        try:
            winner = database.merge_duplicate(candidate_id, keep_job_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
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
        parsed_url = urlsplit(str(values["url"]))
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise HTTPException(status_code=422, detail="Job URLs must use http or https")
        source_job_id = hashlib.sha256(str(values["url"]).encode()).hexdigest()
        job_id, _ = database.upsert_job(
            CollectedJob(
                source="manual", source_job_id=source_job_id, title=str(values["title"]),
                company=str(values["company"]), location=str(values.get("location", "")),
                description=plain_text(str(values.get("description", ""))), url=str(values["url"]),
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


JOB_SORT_LABELS: tuple[tuple[str, str], ...] = (
    ("decision_ready", "Decision-ready"),
    ("opportunity", "Opportunity score"),
    ("job_fit", "Job fit only"),
    ("title_match", "Title match"),
    ("stack_match", "Technology match"),
    ("company_fit", "Company fit"),
    ("newest", "Newest first"),
)

# Label shown on a removable chip for each filter that is currently narrowing the list.
FILTER_CHIP_LABELS: dict[str, str] = {
    "q": "Search",
    "title": "Title",
    "tech": "Technology",
    "route": "Strategy",
    "job_status": "Status",
    "source": "Source",
    "company": "Company",
    "company_list": "Company list",
    "location": "Location",
    "eligibility": "Eligibility",
    "sponsorship": "Sponsorship",
    "relocation": "Relocation",
    "work_model": "Work model",
    "seniority": "Seniority",
    "provider": "Scored by",
    "posted_within": "Posted within",
    "min_score": "Min score",
    "min_title_match": "Min title match",
    "min_stack_match": "Min technology match",
    "salary_floor": "Salary at least",
    "has_salary": "Has stated salary",
    "hide_unmet_experience": "Hiding unmet experience requirements",
}

# These two kinds generate one source row per relocation-target country - dozens of otherwise
# identical "Google Careers"/"Amazon Jobs" entries. Every other kind (Adzuna's per-country rows,
# Arbeitnow's general/sponsored split, company-scoped boards) is genuinely distinct and stays
# listed individually, so only these two are grouped into a single dropdown option per kind.
_GROUPED_SOURCE_LABELS = {"google_careers": "Google Careers", "amazon_jobs": "Amazon Jobs"}


def _source_filter_options(sources: list[SourceConfig]) -> list[dict[str, str]]:
    seen_kinds: set[str] = set()
    options = []
    for source in sources:
        if source.kind in _GROUPED_SOURCE_LABELS:
            if source.kind in seen_kinds:
                continue
            seen_kinds.add(source.kind)
            # A fixed label per kind, not source.name: an individual row's saved name can carry a
            # stale per-country suffix from before this row was generated (save_sources() keeps an
            # existing row's name on every later save), which must never leak into a label that is
            # supposed to represent every row of that kind.
            options.append({"value": source.kind, "label": _GROUPED_SOURCE_LABELS[source.kind]})
        else:
            options.append({"value": source.id, "label": source.name})
    return options


def _as_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: str | None, default: float = 0) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _job_filters_from_query(
    params: Any, sources: list[SourceConfig] | None = None, preferences: dict[str, Any] | None = None
) -> JobFilters:
    values = dict(params)
    technologies = tuple(item for item in params.getlist("tech") if item.strip())
    source = values.get("source", "")
    if source in _GROUPED_SOURCE_LABELS and sources is not None:
        source_ids = tuple(item.id for item in sources if item.kind == source)
    else:
        source_ids = (source,) if source else ()
    company_list = values.get("company_list", "")
    company_in: tuple[str, ...] = ()
    if company_list in {"priority", "watchlist"} and preferences is not None:
        names = preferences.get("priority_companies" if company_list == "priority" else "company_watchlist", [])
        company_in = tuple(company_key(str(name)) for name in names)
    return JobFilters(
        query=values.get("q", "").strip(),
        title=values.get("title", "").strip(),
        technologies=technologies,
        route=values.get("route", ""),
        status=values.get("job_status", ""),
        source_ids=source_ids,
        company=values.get("company", "").strip(),
        company_in=company_in,
        location=values.get("location", "").strip(),
        eligibility=values.get("eligibility", ""),
        sponsorship=values.get("sponsorship", ""),
        relocation=values.get("relocation", ""),
        work_model=values.get("work_model", ""),
        seniority=values.get("seniority", ""),
        provider=values.get("provider", ""),
        posted_within_days=_as_int(values.get("posted_within")),
        min_score=_as_int(values.get("min_score")),
        min_title_match=_as_int(values.get("min_title_match")),
        min_stack_match=_as_int(values.get("min_stack_match")),
        salary_floor=_as_float(values.get("salary_floor")),
        has_salary=values.get("has_salary", "") in {"1", "true", "on"},
        hide_unmet_experience=values.get("hide_unmet_experience", "") in {"1", "true", "on"},
        # Inverted default: absent from the query (a fresh page load, or an explicit uncheck -
        # HTML forms omit an unchecked box either way) means hide, which is the requested default.
        hide_mismatched_titles=values.get("show_mismatched_titles", "") not in {"1", "true", "on"},
    )


def _active_filter_chips(
    params: Any,
    sources: list[SourceConfig] | None = None,
    hidden_title_count: int = 0,
) -> list[dict[str, Any]]:
    """One removable chip per active filter, so an empty result set is always explainable."""
    values = dict(params)
    source_labels = {item["value"]: item["label"] for item in _source_filter_options(sources or [])}
    chips: list[dict[str, Any]] = []
    if hidden_title_count > 0 and values.get("show_mismatched_titles", "") not in {"1", "true", "on"}:
        chips.append(
            {
                "key": "show_mismatched_titles",
                "label": "Hiding different-role titles",
                "value": f"{hidden_title_count} job{'' if hidden_title_count == 1 else 's'}",
                "inverse": True,
            }
        )
    for key, label in FILTER_CHIP_LABELS.items():
        if key == "tech":
            selected = [item for item in params.getlist("tech") if item.strip()]
            if selected:
                chips.append({"key": key, "label": label, "value": ", ".join(selected)})
            continue
        value = str(values.get(key, "")).strip()
        if not value or value in {"0", "0.0"}:
            continue
        if key == "source":
            value = source_labels.get(value, value)
        elif key == "company_list":
            value = "Priority companies" if value == "priority" else "Watchlist" if value == "watchlist" else value
        chips.append(
            {
                "key": key,
                "label": label,
                "value": "yes" if key in {"has_salary", "hide_unmet_experience", "show_mismatched_titles"} else value,
            }
        )
    return chips


async def _payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            value = await request.json()
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=422, detail="Request body must be valid JSON") from error
        if not isinstance(value, dict):
            raise HTTPException(status_code=422, detail="Request body must be a JSON object")
        return value
    value = dict(await request.form())
    value.pop("csrf_token", None)
    return value


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
