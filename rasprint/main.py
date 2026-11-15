import json
import logging
import os
import shutil
from base64 import b64encode
from hashlib import sha256
from html import escape
from pathlib import Path
from string import Template
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic, time
from typing import Literal, TypeVar
from urllib.parse import urlsplit

from anyio import CapacityLimiter, fail_after, to_thread
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, ValidationError
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from .ap import AccessPoint, AccessPointError, DemoAccessPoint, configured_hostname
from .auth import AdminAuth
from .config import STATIC_DIR, Settings
from .cups_setup import CupsSetup, CupsSetupError, DemoCupsSetup
from .documents import OFFICE_EXTENSIONS, SUPPORTED_EXTENSIONS
from .i18n import ApiError, language, localized, translate
from .office import office_engine, office_extensions, prepare_print_document
from .overlay import DemoOverlaySetup, OverlaySetup, OverlaySetupError
from .pages import page_ranges_supported, parse_page_ranges, prepare_pdf_pages
from .printer import CupsPrinter, DemoPrinter, PrinterError
from .state import PrintConfiguration, PrintJobPages, PrintSettings
from .themes import DEFAULT_COLORS, LOGO_BYTES, THEME_BYTES, ThemeStore

log = logging.getLogger(__name__)
MIB = 1024 * 1024
PAGE_EXTENSIONS = OFFICE_EXTENSIONS | {".pdf"}
BodyModel = TypeVar("BodyModel", bound=BaseModel)
PAGE_TEMPLATE = Template((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
EDITOR_DEFAULT_COLORS = escape(json.dumps(DEFAULT_COLORS, separators=(",", ":")))


class PrintOptions(BaseModel):
    printer: str = Field("", max_length=127)
    copies: int = Field(1, ge=1, le=99)
    media: Literal["A4", "Letter"] | None = None
    sides: Literal["one-sided", "two-sided-long-edge", "two-sided-short-edge"] | None = None
    print_color_mode: Literal["auto", "color", "monochrome"] = "auto"
    page_ranges: str = Field("", max_length=1024)

    def cups(self) -> dict[str, str]:
        values = {"copies": str(self.copies)}
        if self.media:
            values["media"] = self.media
        if self.sides:
            values["sides"] = self.sides
        if self.print_color_mode != "auto":
            values["print-color-mode"] = self.print_color_mode
        return values


class Login(BaseModel):
    password: str = Field(..., min_length=1, max_length=1024)


class SelectTheme(BaseModel):
    id: str = Field(..., min_length=1, max_length=48)


class PrintSettingsInput(BaseModel):
    max_upload_mb: int = Field(..., ge=1, le=100)
    printer: str | None = Field(None, max_length=127)


class APInput(BaseModel):
    ssid: str = Field(..., min_length=1, max_length=32)
    secured: bool
    password: str | None = Field(None, max_length=63)
    port: int = Field(..., ge=1, le=65535)
    hostname: str | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=1024)
    new_password: str = Field(..., max_length=1024)


class PrinterRegistration(BaseModel):
    device_id: str = Field(..., min_length=1, max_length=256)
    model_id: str = Field(..., min_length=1, max_length=256)
    queue: str = Field(..., min_length=1, max_length=127)


class OverlayInput(BaseModel):
    enabled: StrictBool


class UploadTooLarge(MultiPartException):
    pass


class UploadParser(MultiPartParser):
    async def parse(self):
        try:
            return await super().parse()
        except BaseException:
            # Starlette 0.50 only handles its own parser exceptions.
            for stream in self._files_to_close_on_error:
                stream.close()
            raise


def document_title(filename: str) -> str:
    title = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return "".join(char for char in title if char.isprintable())


def same_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        supplied = urlsplit(origin)
    except ValueError:
        return False
    return (supplied.scheme, supplied.netloc.lower()) == (request.url.scheme, request.url.netloc.lower())


async def admin_input(request: Request, model: type[BodyModel]) -> BodyModel:
    body = bytearray()
    try:
        with fail_after(10):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 8192:
                    raise ApiError(413, "관리자 요청이 너무 큽니다.", "The admin request is too large.")
                body.extend(chunk)
        return model.parse_raw(bytes(body))
    except (ClientDisconnect, TimeoutError, ValidationError) as exc:
        raise ApiError(422, "입력값을 확인해 주세요.", "Check the values and try again.") from exc


def upload_limit_error(size: int) -> ApiError:
    return ApiError(413, "파일 크기는 {size}MB까지 가능합니다.", "Files may be up to {size} MB.", size=size)


async def parse_upload(request: Request, max_body: int, max_field: int, too_big: ApiError):
    content_type, parameters = parse_options_header(request.headers.get("content-type", ""))
    cannot_read = (400, "첨부 파일을 읽을 수 없습니다.", "The attachment could not be read.")
    if content_type.lower() != b"multipart/form-data":
        raise ApiError(415, "문서 파일을 첨부해 주세요.", "Attach a document.")
    if not 1 <= len(parameters.get(b"boundary", b"")) <= 70:
        raise ApiError(*cannot_read)
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 16:
            raise ApiError(400, "잘못된 요청 크기입니다.", "Invalid request size.")
        if int(length) > max_body:
            raise too_big

    async def limited_body():
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_body:
                raise UploadTooLarge("Upload too large")
            yield chunk

    parser = UploadParser(request.headers, limited_body(), max_files=1, max_fields=8, max_part_size=max_field)
    try:
        with fail_after(180):
            return await parser.parse()
    except UploadTooLarge as exc:
        raise too_big from exc
    except TimeoutError as exc:
        raise ApiError(408, "업로드 시간이 초과되었습니다. 다시 시도하세요.", "Upload timed out. Try again.") from exc
    except ClientDisconnect as exc:
        raise ApiError(400, "업로드 연결이 끊겼습니다.", "The upload connection was interrupted.") from exc
    except (MultiPartException, MultipartParseError) as exc:
        raise ApiError(*cannot_read) from exc


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="RasPrint", docs_url=None, redoc_url=None, openapi_url=None)
    if settings.demo:
        backend = DemoPrinter()
        cups_setup = DemoCupsSetup(lambda: backend.queues()[0], backend.add_queue)
        overlay = DemoOverlaySetup()
    else:
        backend = CupsPrinter()
        cups_setup = CupsSetup()
        overlay = OverlaySetup()
    auth = AdminAuth(settings.admin_password, settings.secure_cookie, settings.state_dir)
    ap = DemoAccessPoint() if settings.demo else AccessPoint()
    printing = PrintSettings(settings.state_dir, PrintConfiguration(settings.max_upload_mb, settings.printer))
    job_pages = PrintJobPages(None if settings.demo else settings.state_dir)
    themes = ThemeStore(settings.themes_dir, settings.state_dir, settings.theme)
    uploads = CapacityLimiter(2)
    password_workers = CapacityLimiter(2)
    printer_setup_workers = CapacityLimiter(2)
    overlay_workers = CapacityLimiter(2)
    cache_lock = Lock()
    dashboard_cache: dict[str, tuple[float, dict]] = {}
    allowed_hosts = {host.strip().lower() for host in os.getenv("RASPRINT_ALLOWED_HOSTS", "").split(",") if host.strip()}
    dynamic_ap_host = os.getenv("RASPRINT_DYNAMIC_AP_HOST", "").lower() == "true"

    def invalidate_dashboard() -> None:
        with cache_lock:
            dashboard_cache.clear()

    @app.middleware("http")
    async def request_policy(request: Request, call_next):
        host = request.headers.get("host", "").partition(":")[0].lower().rstrip(".")
        if (allowed_hosts or dynamic_ap_host) and host not in allowed_hosts and not (
            dynamic_ap_host and host.endswith(".local") and host == f"{configured_hostname()}.local"
        ):
            response = Response(status_code=400)
        elif request.method in {"POST", "PUT", "DELETE", "PATCH"} and not same_origin(request):
            detail = translate(request, "같은 RasPrint 주소에서 요청하세요.", "Make the request from the same RasPrint address.")
            response = JSONResponse({"detail": detail}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        style_hash = getattr(request.state, "theme_style_hash", None)
        style_sources = f"'self' 'sha256-{style_hash}'" if style_hash else "'self'"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self'; style-src {style_sources}; img-src 'self' blob:; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/") or request.url.path in {"/", "/static/index.html"}:
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/") and request.url.path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        if request.url.path.startswith("/api/"):
            response.headers["Content-Language"] = language(request)
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        detail = translate(request, exc.detail, exc.english, **exc.fields)
        return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(PrinterError)
    @app.exception_handler(AccessPointError)
    @app.exception_handler(CupsSetupError)
    @app.exception_handler(OverlaySetupError)
    async def operation_error(request: Request, exc: PrinterError | AccessPointError | CupsSetupError | OverlaySetupError):
        return JSONResponse({"detail": translate(request, str(exc), exc.english)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, _exc: RequestValidationError):
        detail = translate(request, "입력값을 확인해 주세요.", "Check the values and try again.")
        return JSONResponse({"detail": detail}, status_code=422)

    @app.get("/")
    @app.get("/static/index.html", include_in_schema=False)
    def index(request: Request):
        active = themes.active(language(request))
        colors = active["colors"]
        # ThemeStore returns only known color keys and six-digit hex values.
        declarations = "".join(f"--{key.replace('_', '-')}:{color};" for key, color in colors.items())
        style = f":root{{{declarations}}}"
        request.state.theme_style_hash = b64encode(sha256(style.encode("ascii")).digest()).decode("ascii")
        html = PAGE_TEMPLATE.substitute(
            brand_name=escape(active["brand"]["name"]),
            brand_logo=escape(active["brand"]["logo"]),
            theme_style=style,
            editor_default_colors=EDITOR_DEFAULT_COLORS,
        )
        return HTMLResponse(html)

    @app.get("/api/info")
    def info():
        engine = office_engine()
        extensions = SUPPORTED_EXTENSIONS | office_extensions(engine)
        scoped = extensions & PAGE_EXTENSIONS if page_ranges_supported() else set()
        return {
            "max_upload_mb": printing.get().max_upload_mb,
            "allowed_extensions": sorted(extension[1:] for extension in extensions),
            "page_range_extensions": sorted(extension[1:] for extension in scoped),
            "office_available": engine is not None,
            "office_engine": engine,
        }

    @app.get("/api/theme")
    def theme(request: Request):
        return themes.active(language(request))

    @app.get("/theme-assets/{name:path}")
    def theme_asset(name: str):
        return theme_image(themes.image(name))

    @app.get("/managed-theme-assets/{theme_id}/{name}")
    def managed_theme_asset(theme_id: str, name: str):
        try:
            image = themes.managed_image(theme_id, name)
            if image:
                path, mime_type = image
                return Response(path.read_bytes(), media_type=mime_type, headers={"Cache-Control": "no-cache"})
        except FileNotFoundError:
            pass
        raise ApiError(404, "로고를 찾을 수 없습니다.", "Logo not found.")

    def theme_image(image: tuple[Path, str] | None):
        if not image:
            raise ApiError(404, "로고를 찾을 수 없습니다.", "Logo not found.")
        path, mime_type = image
        return FileResponse(path, media_type=mime_type, headers={"Cache-Control": "no-cache"})

    def printer_snapshot(locale: str):
        with cache_lock:
            cached = dashboard_cache.get(locale)
            if cached and monotonic() - cached[0] < 2:
                return cached[1]
            error = None
            try:
                snapshot = backend.snapshot(language=locale, document_progress=job_pages.snapshot)
            except PrinterError as exc:
                snapshot = {"printers": [], "default_printer": None, "jobs": []}
                error = localized(locale, str(exc), exc.english)
            queue = printing.get().printer
            if queue and not error:
                printers = [printer for printer in snapshot["printers"] if printer["id"] == queue]
                snapshot = {
                    "printers": printers,
                    "default_printer": queue if printers else None,
                    "jobs": [job for job in snapshot["jobs"] if job["printer"] == queue],
                }
                if not printers:
                    error = localized(locale, "설정된 CUPS 대기열을 찾을 수 없습니다.",
                                      "The configured CUPS queue was not found.") + f" ({queue})"
            dashboard = {**snapshot, "printer_error": error, "demo": settings.demo}
            dashboard_cache[locale] = (monotonic(), dashboard)
            return dashboard

    @app.get("/api/dashboard")
    def dashboard(request: Request):
        return printer_snapshot(language(request))

    def submit(source: UploadFile, title: str, extension: str, options: PrintOptions) -> int:
        config = printing.get()
        if source.size is not None and source.size > config.max_upload_mb * MIB:
            raise upload_limit_error(config.max_upload_mb)
        page_spec = options.page_ranges.strip()
        if page_spec and extension not in PAGE_EXTENSIONS:
            raise ApiError(422, "PDF, Word, PowerPoint, Excel 문서에서만 페이지를 선택할 수 있습니다.",
                           "Page selection is available for PDF, Word, PowerPoint, and Excel.")
        ranges = parse_page_ranges(page_spec) if page_spec else []
        if ranges and not page_ranges_supported():
            raise ApiError(503, "페이지 선택을 사용하려면 서버에 qpdf를 설치하세요.",
                           "Install qpdf on the server to select pages.")
        queues, default = backend.queues()
        if config.printer and options.printer and options.printer != config.printer:
            raise ApiError(400, "사용할 수 없는 프린터입니다.", "This printer is unavailable.")
        target = config.printer or options.printer or default
        if not target and len(queues) == 1:
            target = queues[0]
        if not queues:
            raise ApiError(503, "CUPS에 등록된 프린터가 없습니다.", "No printers are configured in CUPS.")
        if not target:
            raise ApiError(400, "프린터를 선택하세요.", "Select a printer.")
        if target not in queues:
            raise ApiError(404, "프린터를 찾을 수 없습니다.", "Printer not found.")
        with TemporaryDirectory(prefix="rasprint-") as directory:
            path = Path(directory) / f"document{extension}"
            with path.open("wb") as output:
                shutil.copyfileobj(source.file, output, length=128 * 1024)
            path = prepare_print_document(path)
            pages = None
            if path.suffix.lower() == ".pdf":
                path, pages = prepare_pdf_pages(path, ranges)
            created_at = int(time())
            job_id = backend.submit(target, path, title[:160], options.cups())
            try:
                job_pages.record(job_id, pages=pages, copies=options.copies, printer=target, created_at=created_at)
            except OSError:
                log.exception("Could not persist print job page counts")
            return job_id

    @app.post("/api/jobs", status_code=201)
    async def print_job(request: Request):
        limit = printing.get().max_upload_mb
        max_file = limit * MIB
        too_big = upload_limit_error(limit)
        async with uploads:
            form = await parse_upload(request, max_file + 64 * 1024, 2048, too_big)
            try:
                file = form.get("file")
                if not isinstance(file, UploadFile) or not file.filename or not file.size:
                    raise ApiError(400, "인쇄할 파일을 선택하세요.", "Choose a file to print.")
                if file.size > max_file:
                    raise too_big
                title = document_title(file.filename)
                extension = Path(title).suffix.lower()
                if extension not in SUPPORTED_EXTENSIONS | OFFICE_EXTENSIONS:
                    raise ApiError(415, "지원하지 않는 파일 형식입니다.", "This file type is unsupported.")
                try:
                    options = PrintOptions(**{key: value for key, value in form.items() if key != "file"})
                except ValidationError as exc:
                    raise ApiError(422, "인쇄 설정을 확인해 주세요.", "Check the print settings.") from exc
                job_id = await to_thread.run_sync(submit, file, title, extension, options)
            finally:
                await form.close()
        await to_thread.run_sync(invalidate_dashboard)
        return {"id": job_id}

    @app.get("/api/admin/session")
    def session(request: Request):
        csrf = auth.session(request)
        return {"authenticated": True, "csrf_token": csrf} if csrf else {"authenticated": False}

    @app.post("/api/admin/login")
    async def login(request: Request, response: Response):
        data = await admin_input(request, Login)
        csrf = await to_thread.run_sync(auth.login, request, response, data.password, limiter=password_workers)
        return {"authenticated": True, "csrf_token": csrf}

    @app.put("/api/admin/password")
    async def change_password(request: Request, response: Response):
        auth.require(request, csrf=True)
        data = await admin_input(request, PasswordChange)
        try:
            await to_thread.run_sync(auth.change_password, request, response, data.current_password,
                                     data.new_password, limiter=password_workers)
        except OSError as exc:
            log.exception("Could not persist new admin credential")
            raise ApiError(500, "관리자 비밀번호를 바꿀 수 없습니다.", "Could not change the admin password.") from exc
        return {"authenticated": False}

    @app.post("/api/admin/logout")
    def logout(request: Request, response: Response):
        auth.require(request, csrf=True)
        auth.logout(request, response)
        return {"authenticated": False}

    @app.post("/api/admin/jobs/{job_id}/cancel")
    def cancel_job(job_id: int, request: Request):
        auth.require(request, csrf=True)
        missing_job = (404, "인쇄 작업을 찾을 수 없습니다.", "Print job not found.")
        if not 1 <= job_id <= 2_147_483_647:
            raise ApiError(*missing_job)
        if printing.get().printer and not any(job["id"] == job_id for job in printer_snapshot(language(request))["jobs"]):
            raise ApiError(*missing_job)
        backend.cancel(job_id)
        invalidate_dashboard()
        return {"id": job_id}

    def admin_settings(locale: str):
        config = printing.get()
        printer_error = None
        try:
            queues, _ = backend.queues()
        except PrinterError as exc:
            queues = []
            printer_error = localized(locale, str(exc), exc.english)
        state = ap.status()
        english = state.pop("error_english", None)
        if state.get("error") and english:
            state["error"] = localized(locale, state["error"], english)
        return {
            "max_upload_mb": config.max_upload_mb,
            "printer": config.printer,
            "queues": [{"id": queue, "name": queue} for queue in queues],
            "printer_error": printer_error,
            "ap": state,
        }

    @app.get("/api/admin/settings")
    def get_admin_settings(request: Request):
        auth.require(request)
        return admin_settings(language(request))

    def localize_status(request: Request, result: dict):
        english = result.pop("error_english", None)
        if result.get("error") and english:
            result["error"] = translate(request, result["error"], english)
        return result

    @app.get("/api/admin/printer-devices")
    async def discover_printers(request: Request):
        auth.require(request)
        result = await to_thread.run_sync(cups_setup.discover, limiter=printer_setup_workers)
        return localize_status(request, result)

    @app.post("/api/admin/printers", status_code=201)
    async def register_printer(request: Request):
        auth.require(request, csrf=True)
        data = await admin_input(request, PrinterRegistration)
        result = await to_thread.run_sync(
            cups_setup.register, data.device_id, data.model_id, data.queue, limiter=printer_setup_workers,
        )
        await to_thread.run_sync(invalidate_dashboard)
        return result

    @app.get("/api/admin/overlay")
    async def overlay_status(request: Request):
        auth.require(request)
        result = await to_thread.run_sync(overlay.status, limiter=overlay_workers)
        return localize_status(request, result)

    @app.put("/api/admin/overlay")
    async def configure_overlay(request: Request):
        auth.require(request, csrf=True)
        data = await admin_input(request, OverlayInput)
        result = await to_thread.run_sync(overlay.set_enabled, data.enabled, limiter=overlay_workers)
        return localize_status(request, result)

    @app.post("/api/admin/overlay/reboot", status_code=202)
    async def restart_for_overlay(request: Request):
        auth.require(request, csrf=True)
        return await to_thread.run_sync(overlay.reboot, limiter=overlay_workers)

    @app.put("/api/admin/settings")
    async def save_print_settings(request: Request):
        auth.require(request, csrf=True)
        data = await admin_input(request, PrintSettingsInput)

        def save():
            selected = data.printer or None
            if selected and selected != printing.get().printer and selected not in backend.queues()[0]:
                raise ApiError(422, "선택한 CUPS 대기열을 사용할 수 없습니다.",
                               "The selected CUPS queue is unavailable.")
            try:
                printing.set(PrintConfiguration(data.max_upload_mb, selected))
            except OSError as exc:
                log.exception("Could not save print settings")
                raise ApiError(500, "인쇄 설정을 저장할 수 없습니다.", "Could not save print settings.") from exc
            invalidate_dashboard()
            return admin_settings(language(request))

        return await to_thread.run_sync(save)

    @app.put("/api/admin/ap", status_code=202)
    async def save_ap_settings(request: Request):
        auth.require(request, csrf=True)
        data = await admin_input(request, APInput)
        return await to_thread.run_sync(ap.apply, data.ssid, data.secured, data.password or "", data.port, data.hostname)

    def theme_response(locale: str):
        active_theme = themes.active(locale)
        return {"themes": themes.available(locale), "active": active_theme["id"], "active_theme": active_theme}

    @app.get("/api/admin/themes")
    def list_themes(request: Request):
        auth.require(request)
        return theme_response(language(request))

    @app.get("/api/admin/themes/{theme_id}")
    def inspect_theme(theme_id: str, request: Request):
        auth.require(request)
        try:
            return themes.inspect(theme_id, language(request))
        except KeyError as exc:
            raise ApiError(404, "테마를 찾을 수 없습니다.", "Theme not found.") from exc

    async def save_theme(request: Request, theme_id: str | None = None):
        auth.require(request, csrf=True)
        too_big = ApiError(413, "최대 2MB의 PNG, JPEG 또는 WebP 로고를 올려 주세요.",
                           "Upload a PNG, JPEG, or WebP logo no larger than 2 MB.")
        async with uploads:
            form = await parse_upload(request, LOGO_BYTES + THEME_BYTES + 65536, THEME_BYTES + 1, too_big)
            try:
                config = form.get("config")
                identifier = theme_id if theme_id is not None else form.get("id")
                source = form.get("logo")
                if not isinstance(identifier, str):
                    raise ApiError(422, "테마 ID가 올바르지 않습니다.", "The theme ID is invalid.")
                if not isinstance(config, str):
                    raise ApiError(422, "테마 JSON이 올바르지 않습니다.", "The theme JSON is invalid.")
                if len(config.encode("utf-8")) > THEME_BYTES:
                    raise ApiError(413, "테마 JSON은 최대 32KB입니다.", "The theme JSON may be up to 32 KB.")
                if source is None:
                    logo = None
                else:
                    if not isinstance(source, UploadFile) or not source.size or source.size > LOGO_BYTES:
                        raise too_big
                    logo = await source.read(LOGO_BYTES + 1)
                try:
                    await to_thread.run_sync(
                        lambda: themes.save(identifier, config, logo, edit=theme_id is not None)
                    )
                except KeyError as exc:
                    raise ApiError(404, "테마를 찾을 수 없습니다.", "Theme not found.") from exc
                except OSError as exc:
                    log.exception("Could not save theme")
                    raise ApiError(500, "테마를 저장할 수 없습니다.", "Could not save the theme.") from exc
            finally:
                await form.close()
        return await to_thread.run_sync(theme_response, language(request))

    @app.post("/api/admin/themes", status_code=201)
    async def add_theme(request: Request):
        return await save_theme(request)

    @app.put("/api/admin/themes/{theme_id}")
    async def edit_theme(theme_id: str, request: Request):
        return await save_theme(request, theme_id)

    @app.delete("/api/admin/themes/{theme_id}")
    def remove_theme(theme_id: str, request: Request):
        auth.require(request, csrf=True)
        try:
            themes.remove(theme_id)
        except KeyError as exc:
            raise ApiError(404, "테마를 찾을 수 없습니다.", "Theme not found.") from exc
        except OSError as exc:
            log.exception("Could not remove theme")
            raise ApiError(500, "테마를 제거할 수 없습니다.", "Could not remove the theme.") from exc
        return theme_response(language(request))

    @app.put("/api/admin/theme")
    async def select_theme(request: Request):
        auth.require(request, csrf=True)
        data = await admin_input(request, SelectTheme)
        try:
            await to_thread.run_sync(themes.activate, data.id)
        except KeyError as exc:
            raise ApiError(404, "테마를 찾을 수 없습니다.", "Theme not found.") from exc
        except OSError as exc:
            log.exception("Could not save the active theme")
            raise ApiError(500, "서버에서 테마 설정을 저장하지 못했습니다.", "The server could not save the theme.") from exc
        return await to_thread.run_sync(themes.active, language(request))

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(Settings.from_env())
