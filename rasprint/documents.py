from pathlib import Path
import re


class PrinterError(Exception):
    def __init__(self, message: str, status_code: int = 503, english: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.english = english or message


_FORMATS = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".txt": ("text/plain", b""),
}
SUPPORTED_EXTENSIONS = frozenset(_FORMATS)
OFFICE_EXTENSIONS = frozenset({".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"})
_BINARY_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def validate_document(path: Path) -> str:
    """Return the MIME type for a staged upload after checking its contents."""
    suffix = path.suffix.lower()
    if suffix not in _FORMATS:
        raise PrinterError(
            "PDF, JPG, PNG, UTF-8 TXT 파일을 올려 주세요.", 415,
            "Upload a PDF, JPEG, PNG, or UTF-8 text file.",
        )

    mime, signature = _FORMATS[suffix]
    with path.open("rb") as document:
        header = document.read(16)
    if not header:
        raise PrinterError("빈 파일은 인쇄할 수 없습니다.", 422, "The uploaded file is empty.")
    if not header.startswith(signature):
        raise PrinterError(
            "파일 내용이 확장자와 일치하지 않습니다.", 415,
            "The file contents do not match its extension.",
        )
    if suffix == ".txt":
        try:
            with path.open(encoding="utf-8-sig", newline="") as document:
                while chunk := document.read(65536):
                    if _BINARY_CONTROL_CHARACTERS.search(chunk):
                        raise PrinterError(
                            "TXT 파일에 인쇄할 수 없는 제어 문자가 있습니다.", 415,
                            "The text file contains non-printable control characters.",
                        )
        except UnicodeDecodeError as error:
            raise PrinterError(
                "TXT 파일은 UTF-8 인코딩으로 저장해 주세요.", 415,
                "Save the text file with UTF-8 encoding.",
            ) from error
    return mime
