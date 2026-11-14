from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path
import re
from threading import Lock
from time import monotonic, time
from urllib.parse import unquote, urlsplit

from .documents import PrinterError, validate_document
from .i18n import localized


_PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}
_JOB_STATES = {
    3: "pending",
    4: "held",
    5: "processing",
    6: "stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}
_FINAL_JOB_STATES = {"canceled", "aborted", "completed"}
_HISTORY_LIMIT = 50
_PRINTER_ATTRIBUTES = [
    "printer-is-accepting-jobs", "printer-state", "printer-state-message",
    "printer-state-reasons", "marker-colors", "marker-levels", "marker-names",
]
_JOB_ATTRIBUTES = [
    "job-id", "job-name", "job-printer-uri", "job-state", "job-state-reasons",
    "job-state-message", "job-printer-state-message", "time-at-creation",
    "job-impressions", "job-impressions-completed", "job-media-sheets",
    "job-media-sheets-completed", "job-media-progress",
]
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_IPP_ERRORS = {
    0x0401: ("CUPS가 이 요청을 허용하지 않습니다.", 403, "CUPS refused this request."),
    0x0402: ("CUPS가 인증을 요구합니다.", 403, "CUPS requires authentication."),
    0x0403: ("CUPS 작업에 접근할 권한이 없습니다.", 403, "CUPS denied access to this print job."),
    0x0404: ("현재 상태에서는 이 인쇄 작업을 변경할 수 없습니다.", 409, "This print job cannot be changed in its current state."),
    0x0406: ("CUPS에서 프린터 또는 인쇄 작업을 찾을 수 없습니다.", 404, "CUPS could not find the printer or print job."),
    0x0407: ("CUPS에서 프린터 또는 인쇄 작업을 찾을 수 없습니다.", 404, "CUPS could not find the printer or print job."),
    0x0408: ("CUPS에서 허용하는 파일 크기를 초과했습니다.", 413, "The file exceeds the CUPS size limit."),
    0x040A: ("이 CUPS 큐에서 해당 파일 형식을 지원하지 않습니다.", 415, "This CUPS queue does not support the file format."),
    0x040B: ("인쇄 설정을 프린터에서 지원하지 않습니다.", 422, "The printer does not support the selected print settings."),
    0x040D: ("프린터에서 함께 사용할 수 없는 인쇄 설정입니다.", 422, "The printer cannot use these print settings together."),
    0x0411: ("CUPS가 문서 파일을 해석하지 못했습니다.", 422, "CUPS could not read the document."),
}


def _sequence(value):
    if isinstance(value, (list, tuple)):
        return value
    return [] if value is None else [value]


def _nonnegative(value):
    if type(value) is int and value >= 0:
        return value
    return None


def _percent(value):
    if type(value) is int and 0 <= value <= 100:
        return value
    return None


def _timestamp(value):
    if type(value) in (int, float) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return None


def _reasons(value):
    return [reason for reason in _sequence(value) if reason and reason != "none"]


def _ink_levels(attributes, language):
    names = _sequence(attributes.get("marker-names"))
    levels = _sequence(attributes.get("marker-levels"))
    colors = _sequence(attributes.get("marker-colors"))
    return [
        {
            "name": name or localized(language, "소모품 {number}", "Supply {number}", number=index),
            "level": _percent(level),
            "color": color[:7] if isinstance(color, str) and _HEX_COLOR.match(color) else None,
        }
        for index, (name, level, color) in enumerate(zip_longest(names, levels, colors), 1)
    ]


def _job_progress(attributes, state):
    impressions = _nonnegative(attributes.get("job-impressions"))
    completed = _nonnegative(attributes.get("job-impressions-completed"))
    if state == "completed":
        return 100
    if state == "pending":
        return 0
    # The CUPS percentage describes the current page, so only use it for one-page jobs.
    if impressions == 1 and not completed and state == "processing":
        current_page = _percent(attributes.get("job-media-progress"))
        if current_page is not None:
            return current_page
    if impressions and completed is not None:
        return min(100, completed * 100 // impressions)

    sheets = _nonnegative(attributes.get("job-media-sheets"))
    sheets_completed = _nonnegative(attributes.get("job-media-sheets-completed"))
    if sheets and sheets_completed is not None:
        return min(100, sheets_completed * 100 // sheets)

    return None


def _job(job_id, attributes, language):
    state = _JOB_STATES.get(attributes.get("job-state"), "unknown")
    printer_uri = attributes.get("job-printer-uri") or ""
    return {
        "id": job_id,
        "title": attributes.get("job-name") or localized(
            language, "인쇄 작업 #{id}", "Print job #{id}", id=job_id,
        ),
        "printer": unquote(urlsplit(printer_uri).path.rsplit("/", 1)[-1]),
        "state": state,
        "progress": _job_progress(attributes, state),
        "created_at": _timestamp(attributes.get("time-at-creation")),
        "impressions": _nonnegative(attributes.get("job-impressions")),
        "impressions_completed": _nonnegative(attributes.get("job-impressions-completed")),
        "reasons": _reasons(attributes.get("job-state-reasons")),
        "message": attributes.get("job-state-message") or attributes.get("job-printer-state-message") or "",
    }


def _queue_default(names, cups_default):
    if cups_default in names:
        return cups_default
    return names[0] if names else None


class CupsPrinter:
    @contextmanager
    def _connection(self):
        try:
            import cups
        except ImportError as error:
            raise PrinterError(
                "Python CUPS 모듈을 불러올 수 없습니다.",
                english="The Python CUPS module could not be loaded.",
            ) from error

        try:
            # The web service runs without interactive CUPS authentication.
            cups.setPasswordCB(lambda prompt: None)
            yield cups, cups.Connection()
        except cups.IPPError as error:
            detail = _IPP_ERRORS.get(
                error.args[0], ("CUPS 요청을 처리할 수 없습니다.", 503, "CUPS could not process the request."),
            )
            raise PrinterError(*detail) from error
        except (cups.HTTPError, OSError, RuntimeError) as error:
            raise PrinterError(
                "CUPS 인쇄 서비스에 연결할 수 없습니다.",
                english="RasPrint could not connect to the CUPS print service.",
            ) from error

    def queues(self):
        with self._connection() as (_, connection):
            names = sorted(connection.getPrinters(), key=str.casefold)
            return names, _queue_default(names, connection.getDefault())

    def snapshot(self, language: str = "ko"):
        with self._connection() as (cups, connection):
            queues = connection.getPrinters()
            printers = []
            for name, base in sorted(queues.items(), key=lambda queue: queue[0].casefold()):
                try:
                    status = connection.getPrinterAttributes(name, requested_attributes=_PRINTER_ATTRIBUTES)
                except cups.IPPError as error:
                    if error.args[0] in (0x0406, 0x0407):
                        # The queue can disappear during an administrator's edit.
                        continue
                    raise
                attributes = base | status
                printers.append({
                    "id": name,
                    "name": attributes.get("printer-info") or name,
                    "model": attributes.get("printer-make-and-model") or "",
                    "state": _PRINTER_STATES.get(attributes.get("printer-state"), "unknown"),
                    "accepting": attributes.get("printer-is-accepting-jobs") is not False,
                    "reasons": _reasons(attributes.get("printer-state-reasons")),
                    "message": attributes.get("printer-state-message") or "",
                    "ink": _ink_levels(attributes, language),
                })

            active = connection.getJobs(
                which_jobs="not-completed", my_jobs=True, requested_attributes=_JOB_ATTRIBUTES,
            )
            history = connection.getJobs(
                which_jobs="completed", my_jobs=True, limit=_HISTORY_LIMIT,
                requested_attributes=_JOB_ATTRIBUTES,
            )
            jobs = [_job(job_id, attributes, language) for job_id, attributes in (active | history).items()]
            jobs.sort(key=lambda job: job["id"], reverse=True)
            names = [printer["id"] for printer in printers]
            return {
                "printers": printers,
                "default_printer": _queue_default(names, connection.getDefault()),
                "jobs": jobs,
            }

    def submit(self, printer: str, path: Path, title: str, options: dict[str, str]) -> int:
        mime = validate_document(path)
        job_name = title.encode("utf-8")[:255].decode("utf-8", errors="ignore")
        with self._connection() as (_, connection):
            if printer not in connection.getPrinters():
                raise PrinterError(
                    "선택한 프린터를 CUPS에서 찾을 수 없습니다.", 404,
                    "CUPS could not find the selected printer.",
                )
            print_options = {**options, "document-format": mime}
            return connection.printFile(printer, str(path), job_name, print_options)

    def cancel(self, job_id: int) -> None:
        with self._connection() as (_, connection):
            # CUPS may hide the owner field from standalone Get-Job-Attributes.
            jobs = connection.getJobs(
                which_jobs="all", my_jobs=True, first_job_id=job_id, limit=1,
                requested_attributes=["job-id", "job-state"],
            )
            job = jobs.get(job_id)
            if job is None:
                raise PrinterError(
                    "RasPrint에서 제출한 인쇄 작업을 찾을 수 없습니다.", 404,
                    "RasPrint could not find the print job it submitted.",
                )
            if _JOB_STATES.get(job.get("job-state")) in _FINAL_JOB_STATES:
                raise PrinterError("종료된 작업은 취소할 수 없습니다.", 409, "This print job has already ended.")
            connection.cancelJob(job_id)


class DemoPrinter:
    """In-memory demonstration queue used only when demo mode is enabled."""

    _queue = "HP_OfficeJet_Pro_8710"
    _sample_titles = {
        11: ("인쇄 테스트.pdf", "Print test.pdf"),
        12: ("회의 자료.pdf", "Meeting materials.pdf"),
        13: ("라즈베리파이 설치 가이드.pdf", "Raspberry Pi setup guide.pdf"),
    }
    _demo_ink = [
        ("검정", "Black", 78, "#272629"),
        ("청록", "Cyan", 64, "#08a7cb"),
        ("자홍", "Magenta", 45, "#d13c84"),
        ("노랑", "Yellow", 88, "#e4ad23"),
    ]

    def __init__(self):
        self._lock = Lock()
        self._next_id = 14
        self._jobs = {
            11: self._new_job(11, self._sample_titles[11][0], "aborted", time() - 300),
            12: self._new_job(12, self._sample_titles[12][0], "completed", time() - 120),
            13: self._new_job(13, self._sample_titles[13][0], "processing", time()),
        }
        self._jobs[11]["reasons"] = ["media-jam"]
        self._jobs[13].update(started=monotonic() - 80, duration=180)

    def _new_job(self, job_id, title, state, created):
        return {
            "id": job_id, "title": title, "printer": self._queue, "state": state,
            "progress": 100 if state == "completed" else 0, "created_at": _timestamp(created),
            "impressions": None, "impressions_completed": None, "reasons": [], "message": "",
            "started": None, "duration": 12,
        }

    def _refresh(self, now):
        busy = False
        for job in self._jobs.values():
            if job["state"] == "pending" and not busy:
                job.update(state="processing", started=now)
            if job["state"] == "processing":
                progress = min(100, int(100 * (now - job["started"]) / job["duration"]))
                job.update(progress=progress, state="completed" if progress == 100 else "processing")
                busy = job["state"] == "processing"
        return busy

    def queues(self):
        return [self._queue], self._queue

    def snapshot(self, language: str = "ko"):
        with self._lock:
            busy = self._refresh(monotonic())
            jobs = [
                {key: value for key, value in job.items() if key not in ("started", "duration")}
                for job in reversed(self._jobs.values())
            ]
        for job in jobs:
            if job["id"] in self._sample_titles:
                job["title"] = localized(language, *self._sample_titles[job["id"]])
        return {
            "printers": [{
                "id": self._queue, "name": "HP OfficeJet Pro 8710", "model": "HP OfficeJet Pro 8710",
                "state": "processing" if busy else "idle", "accepting": True, "reasons": [],
                "message": "", "ink": [
                    {"name": localized(language, korean, english), "level": level, "color": color}
                    for korean, english, level, color in self._demo_ink
                ],
            }],
            "default_printer": self._queue,
            "jobs": jobs,
        }

    def submit(self, printer: str, path: Path, title: str, options: dict[str, str]) -> int:
        validate_document(path)
        if printer != self._queue:
            raise PrinterError("선택한 프린터를 찾을 수 없습니다.", 404, "The selected printer could not be found.")
        with self._lock:
            now = monotonic()
            sample = self._jobs.get(13)
            if sample and sample["state"] == "processing":
                sample["started"] = now - sample["duration"] + 1
            self._refresh(now)
            finished = [job_id for job_id, job in self._jobs.items() if job["state"] in _FINAL_JOB_STATES]
            for job_id in finished[:-_HISTORY_LIMIT]:
                del self._jobs[job_id]
            job_id = self._next_id
            self._next_id += 1
            self._jobs[job_id] = self._new_job(job_id, title, "pending", time())
            return job_id

    def cancel(self, job_id: int) -> None:
        with self._lock:
            self._refresh(monotonic())
            job = self._jobs.get(job_id)
            if job is None:
                raise PrinterError("인쇄 작업을 찾을 수 없습니다.", 404, "The print job could not be found.")
            if job["state"] in _FINAL_JOB_STATES:
                raise PrinterError("종료된 작업은 취소할 수 없습니다.", 409, "This print job has already ended.")
            job.update(state="canceled", reasons=["job-canceled-by-user"])
