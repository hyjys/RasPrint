"""Convert Microsoft Office documents to PDF in a bounded local process."""

from importlib.util import find_spec
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
from struct import Struct, calcsize
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Literal
from urllib.parse import unquote
from xml.etree import ElementTree
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from .documents import OFFICE_EXTENSIONS, PrinterError, validate_document


_MIB = 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 8192
_MAX_CENTRAL_DIRECTORY = 8 * _MIB
_MAX_EXPANDED_BYTES = 128 * _MIB
_MAX_METADATA_BYTES = 8 * _MIB
_MAX_PDF_BYTES = 128 * _MIB
_TIMEOUT = 120
_EOCD = Struct("<4s4H2IH")
_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_ENTRY = Struct("<4s6H3I5H2I")
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CONTENT_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
}
_MAIN_ELEMENTS = {
    ".docx": ("wordprocessingml", "document"),
    ".pptx": ("presentationml", "presentation"),
    ".xlsx": ("spreadsheetml", "workbook"),
}
_LEGACY_FILTERS = {".doc": "MS Word 97", ".ppt": "MS PowerPoint 97", ".xls": "MS Excel 97"}
_PYTHON_DEPENDENCIES = ("docx", "pptx", "openpyxl", "reportlab", "PIL", "defusedxml")
_MODERN_EXTENSIONS = frozenset(_CONTENT_TYPES)
OfficeEngine = Literal["libreoffice", "python"]
_RELATION_TYPES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/",
)
_DEBIAN_COMPONENTS = ("libswlo.so", "libsclo.so", "libsdlo.so")
_CONVERSION_LOCK = Lock()
_PROFILE = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting">
    <prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisablePythonRuntime" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="DisableOLEAutomation" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>
    <prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Writer/Content/Update">
    <prop oor:name="Link" oor:op="fuse"><value>2</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Calc/Content/Update">
    <prop oor:name="Link" oor:op="fuse"><value>1</value></prop>
  </item>
</oor:items>
"""


def _invalid() -> PrinterError:
    return PrinterError(
        "Office 문서가 올바르지 않거나 파일 내용이 확장자와 일치하지 않습니다.", 415,
        "The Office document is invalid or does not match its extension.",
    )


def _unsafe() -> PrinterError:
    return PrinterError(
        "Office 문서가 암호화되어 있거나 자동 실행 콘텐츠를 포함합니다.", 415,
        "The Office document is encrypted or contains active content.",
    )


def _too_large() -> PrinterError:
    return PrinterError(
        "Office 문서가 너무 크거나 복잡해서 변환할 수 없습니다.", 413,
        "The Office document is too large or complex to convert.",
    )


def _unavailable() -> PrinterError:
    return PrinterError(
        "Office 문서를 인쇄하려면 서버에 LibreOffice 또는 Python Office 변환 패키지를 설치하세요.", 503,
        "Install LibreOffice or the Python Office rendering dependencies on the server.",
    )


def _failed() -> PrinterError:
    return PrinterError(
        "Office 문서를 변환할 수 없습니다. 파일을 확인하거나 PDF로 내보내세요.", 422,
        "The Office document could not be converted. Check the file or export it to PDF.",
    )


def _office_binary() -> str | None:
    names = ("soffice.com", "soffice.exe") if os.name == "nt" else ("soffice", "libreoffice")
    for name in names:
        if executable := shutil.which(name):
            program = Path(executable).resolve().parent
            if program == Path("/usr/lib/libreoffice/program") and not all(
                (program / library).is_file() for library in _DEBIAN_COMPONENTS
            ):
                continue
            return executable
    if os.name == "nt":
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            if directory := os.environ.get(variable):
                for name in names:
                    candidate = Path(directory) / "LibreOffice" / "program" / name
                    if candidate.is_file():
                        return str(candidate)
    elif sys.platform == "darwin":
        candidate = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if candidate.is_file():
            return str(candidate)
    return None


def _python_available() -> bool:
    return all(find_spec(package) for package in _PYTHON_DEPENDENCIES)


def office_engine() -> OfficeEngine | None:
    if _office_binary():
        return "libreoffice"
    if _python_available():
        return "python"
    return None


def office_extensions(engine: OfficeEngine | None) -> frozenset[str]:
    if engine == "libreoffice":
        return OFFICE_EXTENSIONS
    if engine == "python":
        return _MODERN_EXTENSIONS
    return frozenset()


def _bound_zip_directory(path: Path) -> None:
    # Check the central directory before ZipFile allocates metadata for its members.
    size = path.stat().st_size
    with path.open("rb") as stream:
        if stream.read(8) == _OLE_SIGNATURE:
            # Password-protected OOXML files may wrap their data in an OLE container.
            raise _unsafe()
        tail_size = min(size, 65_535 + _EOCD.size)
        stream.seek(size - tail_size)
        tail = stream.read(tail_size)
    offset = tail.rfind(_EOCD_SIGNATURE)
    if offset == -1 or len(tail) - offset < _EOCD.size:
        raise _invalid()
    _, disk, directory_disk, records, count, length, start, comment = _EOCD.unpack_from(tail, offset)
    if disk or directory_disk or records != count or offset + _EOCD.size + comment != len(tail):
        raise _invalid()
    if count > _MAX_ARCHIVE_MEMBERS or length > _MAX_CENTRAL_DIRECTORY:
        raise _too_large()
    if start + length != size - len(tail) + offset:
        raise _invalid()
    with path.open("rb") as stream:
        stream.seek(start)
        for _ in range(count):
            if stream.tell() + _CENTRAL_ENTRY.size > start + length:
                raise _invalid()
            entry = _CENTRAL_ENTRY.unpack(stream.read(_CENTRAL_ENTRY.size))
            if entry[0] != _CENTRAL_SIGNATURE:
                raise _invalid()
            stream.seek(sum(entry[10:13]), os.SEEK_CUR)
        if stream.tell() != start + length:
            raise _invalid()


def _entry_name(info: ZipInfo) -> str:
    name = info.filename.rstrip("/")
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name
            or "\x00" in info.orig_filename or stat.S_ISLNK(info.external_attr >> 16)):
        raise _invalid()
    return name


def _metadata(package: ZipFile, info: ZipInfo) -> ElementTree.Element:
    with package.open(info) as stream:
        content = stream.read(_MAX_METADATA_BYTES + 1)
    if len(content) > _MAX_METADATA_BYTES:
        raise _too_large()
    if b"<!DOCTYPE" in content.replace(b"\0", b""):
        raise _unsafe()
    return ElementTree.fromstring(content)


def _validate_main_part(package: ZipFile, info: ZipInfo, suffix: str) -> None:
    with package.open(info) as stream:
        prefix = stream.read(64 * 1024)
    if b"<!DOCTYPE" in prefix.replace(b"\0", b""):
        raise _unsafe()
    parser = ElementTree.XMLPullParser(events=("start",))
    parser.feed(prefix)
    part_type, element_name = _MAIN_ELEMENTS[suffix]
    namespaces = (f"http://schemas.openxmlformats.org/{part_type}/2006/main",
                  f"http://purl.oclc.org/ooxml/{part_type}/main")
    first = next(parser.read_events(), None)
    if first is None or first[1].tag not in {f"{{{namespace}}}{element_name}" for namespace in namespaces}:
        raise _invalid()


def _validate_ooxml(path: Path, suffix: str) -> None:
    _bound_zip_directory(path)
    with ZipFile(path) as package:
        infos = package.infolist()
        if len(infos) > _MAX_ARCHIVE_MEMBERS or sum(info.file_size for info in infos) > _MAX_EXPANDED_BYTES:
            raise _too_large()
        entries = {}
        for info in infos:
            name = _entry_name(info).casefold()
            if name in entries:
                raise _invalid()
            if (info.flag_bits & 1 or name.endswith(("vbaproject.bin", "vbadata.xml"))
                    or "activex" in PurePosixPath(name).parts):
                raise _unsafe()
            if info.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                raise _invalid()
            entries[name] = info

        metadata = (info for name, info in entries.items() if name.endswith(".rels") or name == "[content_types].xml")
        if sum(info.file_size for info in metadata) > _MAX_METADATA_BYTES:
            raise _too_large()
        content_types = {}
        for element in _metadata(package, entries["[content_types].xml"]):
            content_type = element.get("ContentType", "")
            if "macroenabled" in content_type.casefold() or "vbaproject" in content_type.casefold():
                raise _unsafe()
            if part := element.get("PartName"):
                content_types[unquote(part).lstrip("/").casefold()] = content_type

        office_part = None
        for name, info in entries.items():
            if not name.endswith(".rels"):
                continue
            for relation in _metadata(package, info):
                relation_type = relation.get("Type", "")
                if relation.get("TargetMode", "").casefold() == "external":
                    if relation_type not in {prefix + "hyperlink" for prefix in _RELATION_TYPES}:
                        raise _unsafe()
                elif name == "_rels/.rels" and relation_type in {
                    prefix + "officeDocument" for prefix in _RELATION_TYPES
                }:
                    office_part = unquote(relation.get("Target", "")).lstrip("/").casefold()
        if office_part not in entries or content_types.get(office_part) != _CONTENT_TYPES[suffix]:
            raise _invalid()
        _validate_main_part(package, entries[office_part], suffix)


def _validate_office(path: Path, suffix: str) -> None:
    try:
        if suffix in _CONTENT_TYPES:
            _validate_ooxml(path, suffix)
        else:
            with path.open("rb") as stream:
                if stream.read(8) != _OLE_SIGNATURE or path.stat().st_size < 512:
                    raise _invalid()
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError, ValueError, RuntimeError) as exc:
        raise _invalid() from exc


def _process_environment(work: Path) -> dict[str, str]:
    keep = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR", "FONTCONFIG_FILE", "FONTCONFIG_PATH")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update({
        "HOME": str(work), "USERPROFILE": str(work), "TMPDIR": str(work), "TEMP": str(work), "TMP": str(work),
        "XDG_CACHE_HOME": str(work / "cache"), "XDG_CONFIG_HOME": str(work / "config"),
    })
    return env


def _convert_libreoffice(path: Path, target: Path, binary: str) -> None:
    work = target.parent
    profile = work / "profile"
    profile.joinpath("user").mkdir(parents=True)
    profile.joinpath("user", "registrymodifications.xcu").write_text(_PROFILE, encoding="utf-8")
    env = _process_environment(work)
    env["SAL_USE_VCLPLUGIN"] = "svp"
    options = [
        binary, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--nologo",
        "--nodefault", "--norestore", "--nolockcheck",
    ]
    if legacy := _LEGACY_FILTERS.get(path.suffix.lower()):
        options.append(f"--infilter={legacy}")
    options.extend(("--convert-to", "pdf", "--outdir", str(target.parent), str(path)))
    memory = 896 if calcsize("P") == 4 else 2048
    _run_conversion(options, target, work, env, memory)


def _convert_python(path: Path, target: Path) -> None:
    options = [sys.executable, "-B", "-m", "rasprint.office", str(path), str(target)]
    _run_conversion(options, target, Path(__file__).resolve().parents[1], _process_environment(target.parent), 384)


def _run_conversion(options: list[str], target: Path, cwd: Path, env: dict[str, str], memory_mib: int) -> None:
    if sys.platform == "linux" and (prlimit := shutil.which("prlimit")):
        options[:0] = [
            prlimit, f"--as={memory_mib * _MIB}", f"--fsize={_MAX_PDF_BYTES}", f"--cpu={_TIMEOUT}", "--",
        ]
    try:
        with subprocess.Popen(
            options, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=os.name == "posix",
        ) as process:
            try:
                result = process.wait(timeout=_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                raise PrinterError("Office 문서 변환 시간이 초과되었습니다.", 504,
                                   "Office document conversion timed out.") from exc
    except FileNotFoundError as exc:
        raise _unavailable() from exc
    except OSError as exc:
        raise _failed() from exc
    if result < 0 and -result in (getattr(signal, "SIGXFSZ", -1), getattr(signal, "SIGXCPU", -1)):
        raise _too_large()
    if target.is_file() and target.stat().st_size >= _MAX_PDF_BYTES:
        raise _too_large()
    if result != 0 or not target.is_file():
        raise _failed()
    try:
        validate_document(target)
    except (OSError, PrinterError) as exc:
        raise _failed() from exc


def prepare_print_document(path: Path) -> Path:
    """Validate native formats or convert an Office upload to a staged PDF."""
    path = path.resolve()
    suffix = path.suffix.lower()
    if suffix not in OFFICE_EXTENSIONS:
        validate_document(path)
        return path
    binary = _office_binary()
    if not binary and suffix in _LEGACY_FILTERS:
        raise PrinterError(
            "이전 DOC, PPT, XLS 파일을 인쇄하려면 LibreOffice를 설치하거나 최신 Office 형식으로 저장하세요.", 415,
            "Install LibreOffice to print legacy DOC, PPT, or XLS files, or save the document in a modern Office format.",
        )
    if not binary and not _python_available():
        raise _unavailable()
    if not _CONVERSION_LOCK.acquire(blocking=False):
        raise PrinterError("다른 Office 문서를 변환하고 있습니다. 잠시 후 다시 시도하세요.", 503,
                           "Another Office document is being converted. Try again shortly.")
    try:
        _validate_office(path, suffix)
        target = path.with_suffix(".pdf")
        with TemporaryDirectory(prefix="office-", dir=path.parent) as work:
            staged_pdf = Path(work) / target.name
            if binary:
                _convert_libreoffice(path, staged_pdf, binary)
            else:
                _convert_python(path, staged_pdf)
            staged_pdf.replace(target)
        return target
    finally:
        _CONVERSION_LOCK.release()


def _render(source: Path, output: Path) -> None:
    # Import only the requested format in the worker to keep Pi memory use lower.
    if source.suffix.lower() == ".docx":
        from .office_word import render_word

        render_word(source, output)
    elif source.suffix.lower() == ".pptx":
        from .office_slides import render_slides

        render_slides(source, output)
    elif source.suffix.lower() == ".xlsx":
        from .office_excel import render_excel

        render_excel(source, output)
    else:
        raise ValueError("Unsupported Python Office format")


if __name__ == "__main__":
    _render(Path(sys.argv[1]), Path(sys.argv[2]))
