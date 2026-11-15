"""Fonts shared by the Python Office renderers."""

import os
from functools import cache
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError


@cache
def _fonts() -> tuple[str, str, str, str]:
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    nanum = Path("/usr/share/fonts/truetype/nanum")
    dejavu = Path("/usr/share/fonts/truetype/dejavu")
    candidates = (
        (windows, "malgun.ttf", "malgunbd.ttf", "", ""),
        (nanum, "NanumGothic.ttf", "NanumGothicBold.ttf", "", ""),
        (dejavu, "DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
         "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"),
    )
    for directory, regular, bold, italic, bold_italic in candidates:
        if not (directory / regular).is_file():
            continue
        names = []
        for name, filename, fallback in (
            ("RasPrintSans", regular, ""),
            ("RasPrintSans-Bold", bold, "RasPrintSans"),
            ("RasPrintSans-Italic", italic, "RasPrintSans"),
            ("RasPrintSans-BoldItalic", bold_italic, "RasPrintSans-Bold"),
        ):
            try:
                if not filename or not (directory / filename).is_file():
                    raise FileNotFoundError
                pdfmetrics.registerFont(TTFont(name, str(directory / filename)))
            except (OSError, TTFError):
                name = fallback
            names.append(name)
        if names[0]:
            normal, bold_name, italic_name, bold_italic_name = names
            pdfmetrics.registerFontFamily(
                normal, normal=normal, bold=bold_name, italic=italic_name, boldItalic=bold_italic_name
            )
            return normal, bold_name, italic_name, bold_italic_name
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"


def register_fonts() -> str:
    """Register the font family and return its normal face."""
    return _fonts()[0]


def font_name(bold: bool = False, italic: bool = False) -> str:
    return _fonts()[int(bool(bold)) + 2 * int(bool(italic))]
