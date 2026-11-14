from fastapi import HTTPException, Request

from .locales import TRANSLATIONS

LOCALE_INDEX = {"ja": 0, "zh-CN": 1, "es": 2, "fr": 3, "de": 4, "ru": 5}


def resolve_locale(tag: str) -> str | None:
    normalized = tag.strip().lower().replace("_", "-")
    if normalized in {"zh", "zh-cn", "zh-sg", "zh-hans"} or normalized.startswith(("zh-hans-", "zh-cn-", "zh-sg-")):
        return "zh-CN"
    primary = normalized.split("-", 1)[0]
    if primary in {"ko", "en"} or primary in LOCALE_INDEX:
        return primary
    return None


def language(request: Request) -> str:
    if selected := resolve_locale(request.headers.get("X-RasPrint-Language", "")):
        return selected
    choices = []
    for entry in request.headers.get("Accept-Language", "").split(","):
        tag, _, parameter = entry.lower().partition(";")
        locale = resolve_locale(tag)
        if not locale:
            continue
        try:
            quality = float(parameter.strip()[2:]) if parameter.strip().startswith("q=") else 1
        except ValueError:
            continue
        if 0 < quality <= 1:
            choices.append((quality, locale))
    return max(choices, key=lambda choice: choice[0])[1] if choices else "en"


def localized(locale: str, korean: str, english: str, **fields) -> str:
    translations = TRANSLATIONS.get(english)
    if locale == "ko":
        value = korean
    elif translations is not None and locale in LOCALE_INDEX:
        value = translations[LOCALE_INDEX[locale]]
    else:
        value = english
    return value.format(**fields) if fields else value


def translate(request: Request, korean: str, english: str, **fields) -> str:
    return localized(language(request), korean, english, **fields)


class ApiError(HTTPException):
    def __init__(
        self, status_code: int, korean: str, english: str,
        headers: dict[str, str] | None = None, **fields,
    ):
        super().__init__(status_code, korean, headers)
        self.english = english
        self.fields = fields
