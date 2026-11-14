import json
import re
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from urllib.parse import quote

from .i18n import ApiError, localized
from .state import atomic_write

THEME_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
RESERVED_ID = re.compile(r"raspberry|con|prn|aux|nul|(?:com|lpt)[0-9]")
MANAGED_LOGO = re.compile(r"logo(?:-[a-f0-9]{16})?\.(?:png|jpg|webp)")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
THEME_BYTES = 32 * 1024
LOGO_BYTES = 2 * 1024 * 1024
DEFAULT_COLORS = {
    "primary": "#c51a4a",
    "primary_hover": "#a8113a",
    "primary_light": "#fce8ee",
    "primary_text": "#ffffff",
    "sidebar": "#fffdfd",
    "background": "#f8f6f7",
    "surface": "#ffffff",
    "panel": "#f7f2f4",
    "border": "#eadfe3",
    "text": "#281a20",
    "muted": "#64555d",
    "success": "#237648",
    "success_light": "#e8f5ec",
    "danger": "#aa243d",
    "danger_light": "#fce9ee",
    "warning": "#8a5316",
    "warning_light": "#fff2db",
}
DEFAULT_BRAND = {"name": "RasPrint", "logo": "/static/logo.png"}


def label(value: object, fallback: str) -> str:
    if isinstance(value, str) and 0 < len(value.strip()) <= 48 and value.isprintable():
        return value.strip()
    return fallback


class ThemeStore:
    def __init__(self, directory: Path, state_dir: Path, initial: str):
        self.directory = directory
        self.state_file = state_dir / "theme"
        self.managed = state_dir / "themes"
        self.hidden = state_dir / "hidden-themes"
        self.initial = initial
        self.lock = RLock()

    def local_file(self, path: str) -> Path | None:
        try:
            directory = self.directory.resolve()
            candidate = (directory / path).resolve()
            if candidate.is_relative_to(directory) and candidate.is_file():
                return candidate
        except (OSError, RuntimeError, ValueError):
            pass
        return None

    def image(self, name: str) -> tuple[Path, str] | None:
        if not name or len(name) > 240 or name.startswith(("/", "\\")) or "\\" in name:
            return None
        return self.image_file(self.local_file(name))

    @staticmethod
    def image_file(path: Path | None) -> tuple[Path, str] | None:
        if path and path.suffix.lower() in IMAGE_TYPES:
            return path, IMAGE_TYPES[path.suffix.lower()]
        return None

    def managed_image(self, theme_id: str, name: str) -> tuple[Path, str] | None:
        if not THEME_ID.fullmatch(theme_id) or not MANAGED_LOGO.fullmatch(name):
            return None
        with self.lock:
            path = self.managed / theme_id / name
            if not (self.hidden / theme_id).is_file() and path.is_file():
                return self.image_file(path)
        return None

    def stored_theme(self, theme_id: str) -> Path | None:
        if not THEME_ID.fullmatch(theme_id) or (self.hidden / theme_id).is_file():
            return None
        managed = self.managed / theme_id / "theme.json"
        return managed if managed.is_file() else self.local_file(f"{theme_id}.json")

    def load(self, theme_id: str, language: str = "ko") -> dict | None:
        path = self.stored_theme(theme_id)
        if path is None:
            return None
        try:
            if path.stat().st_size > THEME_BYTES:
                return None
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None

        colors = DEFAULT_COLORS.copy()
        custom_colors = data.get("colors")
        if isinstance(custom_colors, dict):
            for key, color in custom_colors.items():
                if key in colors and isinstance(color, str) and COLOR.fullmatch(color):
                    colors[key] = color

        brand = DEFAULT_BRAND.copy()
        custom_brand = data.get("brand")
        if isinstance(custom_brand, dict):
            brand["name"] = label(custom_brand.get("name"), brand["name"])
            logo = custom_brand.get("logo")
            if isinstance(logo, str):
                if path.is_relative_to(self.managed) and self.managed_image(theme_id, logo):
                    brand["logo"] = f"/managed-theme-assets/{theme_id}/{logo}"
                elif self.image(logo):
                    brand["logo"] = f"/theme-assets/{quote(logo, safe='/')}"
        name = label(data.get("name"), theme_id)
        if theme_id == "plum" and name == "플럼" and data.get("name_en") == "Plum":
            name = localized(language, "플럼", "Plum")
        name = label(data.get(f"name_{language}"), label(data.get("name_en"), name) if language == "en" else name)
        return {"id": theme_id, "name": name, "brand": brand, "colors": colors}

    def available(self, language: str = "ko") -> list[dict]:
        result = [{"id": "raspberry", "name": localized(language, "라즈베리", "Raspberry"), "removable": False}]
        with self.lock:
            ids = {path.stem for path in self.directory.glob("*.json")}
            ids.update(path.name for path in self.managed.glob("*") if path.is_dir())
            for theme_id in sorted(ids - {"raspberry"}):
                if theme := self.load(theme_id, language):
                    result.append({"id": theme_id, "name": theme["name"], "removable": True})
        return result

    def active(self, language: str = "ko") -> dict:
        with self.lock:
            try:
                saved = self.state_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                saved = ""
            for theme_id in (saved, self.initial):
                if theme_id == "raspberry":
                    break
                if theme := self.load(theme_id, language):
                    return theme
        return {
            "id": "raspberry",
            "name": localized(language, "라즈베리", "Raspberry"),
            "brand": DEFAULT_BRAND,
            "colors": DEFAULT_COLORS,
        }

    def activate(self, theme_id: str) -> None:
        with self.lock:
            if theme_id != "raspberry" and not self.load(theme_id):
                raise KeyError(theme_id)
            atomic_write(self.state_file, theme_id.encode("ascii"))

    def inspect(self, theme_id: str, language: str) -> dict:
        with self.lock:
            if theme_id == "raspberry":
                return {"id": theme_id, "name": localized(language, "라즈베리", "Raspberry"),
                        "name_en": "Raspberry", "brand": DEFAULT_BRAND, "colors": DEFAULT_COLORS}
            if theme := self.load(theme_id, language):
                source = self.stored_theme(theme_id)
                data = json.loads(source.read_text(encoding="utf-8-sig"))
                return {**theme, "name": label(data.get("name"), theme_id),
                        "name_en": label(data.get("name_en"), "")}
        raise KeyError(theme_id)

    def save(self, theme_id: str, content: str, logo: bytes | None, *, edit: bool = False) -> None:
        if not THEME_ID.fullmatch(theme_id) or RESERVED_ID.fullmatch(theme_id):
            raise ApiError(422, "사용할 수 없는 테마 ID입니다.", "This theme ID is reserved or invalid.")
        try:
            fields = json.loads(content.lstrip("\ufeff"))
            if not isinstance(fields, dict):
                raise ValueError
            colors = fields.get("colors", {})
            brand = fields.get("brand", {})
            if not isinstance(brand, dict) or not isinstance(colors, dict):
                raise ValueError
            if any(key not in DEFAULT_COLORS or not isinstance(value, str) or not COLOR.fullmatch(value)
                   for key, value in colors.items()):
                raise ValueError
            if brand.get("logo") is not None and logo is None and (
                not isinstance(brand["logo"], str) or not self.image(brand["logo"])
            ):
                raise ValueError
        except (ValueError, RecursionError) as exc:
            raise ApiError(422, "테마 JSON이 올바르지 않습니다.", "The theme JSON is invalid.") from exc
        with self.lock:
            target = self.managed / theme_id
            existing = self.stored_theme(theme_id)
            if edit and not existing:
                raise KeyError(theme_id)
            if not edit and (existing or target.exists()):
                raise ApiError(409, "같은 ID의 테마가 이미 있습니다.", "A theme with that ID already exists.")
            data = {}
            if edit:
                data = json.loads(existing.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ApiError(422, "테마 JSON이 올바르지 않습니다.", "The theme JSON is invalid.")
                old_brand = data.get("brand")
                if isinstance(old_brand, dict):
                    brand = old_brand | brand
            name = None
            if logo is not None:
                suffix = Path(logo_name(logo)).suffix
                name = f"logo-{sha256(logo).hexdigest()[:16]}{suffix}"
                brand["logo"] = name
            data.update(fields)
            if brand:
                data["brand"] = brand
            try:
                encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except (UnicodeEncodeError, RecursionError) as exc:
                raise ApiError(422, "테마 JSON이 올바르지 않습니다.", "The theme JSON is invalid.") from exc
            if len(encoded) > THEME_BYTES:
                raise ApiError(413, "테마 JSON은 최대 32KB입니다.", "The theme JSON may be up to 32 KB.")
            self.managed.mkdir(parents=True, exist_ok=True)
            if target.is_dir():
                if name:
                    atomic_write(target / name, logo)
                atomic_write(target / "theme.json", encoded)
                for old in target.glob("logo*"):
                    if MANAGED_LOGO.fullmatch(old.name) and old.name != brand.get("logo"):
                        try:
                            old.unlink()
                        except OSError:
                            pass
            else:
                with TemporaryDirectory(prefix=".theme-", dir=self.managed) as directory:
                    staging = Path(directory) / "content"
                    staging.mkdir()
                    if name:
                        (staging / name).write_bytes(logo)
                    (staging / "theme.json").write_bytes(encoded)
                    staging.rename(target)
            (self.hidden / theme_id).unlink(missing_ok=True)

    def remove(self, theme_id: str) -> None:
        if theme_id == "raspberry":
            raise ApiError(422, "이 테마는 제거할 수 없습니다.", "This theme cannot be removed.")
        with self.lock:
            if not self.stored_theme(theme_id):
                raise KeyError(theme_id)
            if self.active()["id"] == theme_id:
                self.activate("raspberry")
            target = self.managed / theme_id
            if self.local_file(f"{theme_id}.json"):
                atomic_write(self.hidden / theme_id, b"")
            if target.is_dir():
                with TemporaryDirectory(prefix=".removed-", dir=self.managed) as directory:
                    trash = Path(directory) / "content"
                    target.rename(trash)


def logo_name(logo: bytes) -> str:
    if 14 <= len(logo) <= LOGO_BYTES:
        if logo.startswith(b"\x89PNG\r\n\x1a\n"):
            return "logo.png"
        if logo.startswith(b"\xff\xd8\xff"):
            return "logo.jpg"
        if logo[:4] == b"RIFF" and logo[8:12] == b"WEBP":
            return "logo.webp"
    raise ApiError(415, "최대 2MB의 PNG, JPEG 또는 WebP 로고를 올려 주세요.",
                   "Upload a PNG, JPEG, or WebP logo no larger than 2 MB.")
