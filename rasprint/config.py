import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "rasprint" / "static"


def flag(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    admin_password: str = field(repr=False)
    printer: str | None
    theme: str
    themes_dir: Path
    state_dir: Path
    max_upload_mb: int
    secure_cookie: bool
    demo: bool

    @classmethod
    def from_env(cls) -> "Settings":
        password = os.getenv("RASPRINT_ADMIN_PASSWORD", "rasprint")
        if not password or len(password.encode("utf-8")) > 1024:
            raise ValueError("RASPRINT_ADMIN_PASSWORD must contain 1–1024 UTF-8 bytes")
        size = int(os.getenv("RASPRINT_MAX_UPLOAD_MB", "25"))
        if not 1 <= size <= 100:
            raise ValueError("RASPRINT_MAX_UPLOAD_MB must be between 1 and 100")
        return cls(
            admin_password=password,
            printer=os.getenv("RASPRINT_PRINTER") or None,
            theme=os.getenv("RASPRINT_THEME", "raspberry"),
            themes_dir=Path(os.getenv("RASPRINT_THEMES_DIR", ROOT / "themes")).resolve(),
            state_dir=Path(os.getenv("RASPRINT_STATE_DIR", ROOT / ".rasprint")).resolve(),
            max_upload_mb=size,
            secure_cookie=flag("RASPRINT_SECURE_COOKIE"),
            demo=flag("RASPRINT_DEMO"),
        )
