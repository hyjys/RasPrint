import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class PrintConfiguration:
    max_upload_mb: int
    printer: str | None


class PrintSettings:
    def __init__(self, state_dir: Path, defaults: PrintConfiguration):
        self.path = state_dir / "print-settings.json"
        self.lock = Lock()
        try:
            config = json.loads(self.path.read_text(encoding="utf-8"))
            limit, printer = config["max_upload_mb"], config["printer"]
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("Invalid upload limit")
            if printer is not None and (not isinstance(printer, str) or not 1 <= len(printer) <= 127 or not printer.isprintable()):
                raise ValueError("Invalid printer name")
        except (OSError, ValueError, KeyError, TypeError):
            self.current = defaults
        else:
            self.current = PrintConfiguration(limit, printer)

    def get(self) -> PrintConfiguration:
        with self.lock:
            return self.current

    def set(self, config: PrintConfiguration) -> None:
        with self.lock:
            payload = {"max_upload_mb": config.max_upload_mb, "printer": config.printer}
            atomic_write(self.path, json.dumps(payload).encode("utf-8"))
            self.current = config
