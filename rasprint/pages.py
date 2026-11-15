import re
import shutil
import subprocess
from pathlib import Path

from .i18n import ApiError

PAGE_ITEM = re.compile(r"([0-9]{1,9})(?:\s*[-–]\s*([0-9]{1,9}))?")
PAGE_TIMEOUT = 40
MAX_OUTPUT_BYTES = 100 * 1024 * 1024


def page_ranges_supported() -> bool:
    return shutil.which("qpdf") is not None


def parse_page_ranges(value: str) -> list[tuple[int, int]]:
    ranges = []
    for item in value.split(","):
        match = PAGE_ITEM.fullmatch(item.strip())
        if match:
            start = int(match[1])
            end = int(match[2]) if match[2] else start
            if 1 <= start <= end:
                ranges.append((start, end))
                continue
        raise ApiError(422, "페이지 범위를 확인하세요. 예: 1,3-5,8", "Enter page numbers such as 1,3-5,8.")

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            previous, last = merged[-1]
            merged[-1] = (previous, max(end, last))
        else:
            merged.append((start, end))
    return merged


def run_qpdf(executable: str, *args: str) -> str:
    command = [executable, *args]
    if (prlimit := shutil.which("prlimit")) is not None:
        command = [prlimit, f"--fsize={MAX_OUTPUT_BYTES}", "--as=402653184", "--", *command]
    try:
        result = subprocess.run(
            command, check=False, timeout=PAGE_TIMEOUT, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="ascii", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(422, "PDF 페이지를 처리하지 못했습니다.",
                       "The PDF pages could not be processed.") from exc
    # qpdf status 3 means it recovered from warnings and produced its output.
    if result.returncode not in (0, 3):
        raise ApiError(422, "PDF 페이지를 처리하지 못했습니다.", "The PDF pages could not be processed.")
    return result.stdout.strip()


def prepare_pdf_pages(path: Path, ranges: list[tuple[int, int]]) -> tuple[Path, int | None]:
    executable = shutil.which("qpdf")
    if not executable:
        if ranges:
            raise ApiError(503, "페이지 선택을 사용하려면 서버에 qpdf를 설치하세요.",
                           "Install qpdf on the server to select pages.")
        return path, None

    try:
        raw_count = run_qpdf(executable, "--show-npages", str(path))
        count = int(raw_count) if raw_count.isdecimal() and len(raw_count) <= 9 else 0
    except ApiError:
        if ranges:
            raise
        return path, None

    if not ranges:
        return path, count or None
    if not count:
        raise ApiError(422, "PDF 페이지를 처리하지 못했습니다.", "The PDF pages could not be processed.")
    if ranges[-1][1] > count:
        raise ApiError(422, "이 문서는 {pages}페이지입니다. 페이지 범위를 확인하세요.",
                       "This document has {pages} pages. Check the page selection.", pages=count)
    selected = sum(end - start + 1 for start, end in ranges)
    if selected == count:
        return path, count

    selection = ",".join(str(start) if start == end else f"{start}-{end}" for start, end in ranges)
    output = path.parent / "selected-pages.pdf"
    run_qpdf(executable, str(path), "--pages", ".", selection, "--", str(output))
    if not output.is_file() or output.stat().st_size > MAX_OUTPUT_BYTES:
        raise ApiError(422, "선택한 PDF 페이지가 너무 크거나 처리할 수 없습니다.",
                       "The selected PDF pages are too large or could not be processed.")
    return output, selected
