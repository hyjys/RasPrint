"""Render static PowerPoint slides to PDF for the Python print fallback."""

from html import escape
from io import BytesIO
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.enum.dml import MSO_COLOR_TYPE, MSO_FILL
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.shapes.connector import Connector
from pptx.shapes.picture import Picture
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from .office_pdf import font_name, register_fonts


_ALIGNMENTS = {PP_ALIGN.LEFT: TA_LEFT, PP_ALIGN.CENTER: TA_CENTER, PP_ALIGN.RIGHT: TA_RIGHT, PP_ALIGN.JUSTIFY: TA_JUSTIFY}
_TITLES = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE, PP_PLACEHOLDER.VERTICAL_TITLE}
_BODY = {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.VERTICAL_BODY}
_THEME_ALIASES = {"bg1": "lt1", "tx1": "dk1", "bg2": "lt2", "tx2": "dk2"}
_POLYGONS = {
    "triangle": [(0, 0), (1, 0), (0.5, 1)],
    "rtTriangle": [(0, 0), (1, 0), (0, 1)],
    "diamond": [(0, 0.5), (0.5, 0), (1, 0.5), (0.5, 1)],
    "parallelogram": [(0, 0), (0.8, 0), (1, 1), (0.2, 1)],
    "chevron": [(0, 0), (0.75, 0), (1, 0.5), (0.75, 1), (0, 1), (0.25, 0.5)],
    "rightArrow": [(0, 0.25), (0.6, 0.25), (0.6, 0), (1, 0.5), (0.6, 1), (0.6, 0.75), (0, 0.75)],
}
_CELL_BORDER = HexColor("#ced4de")
_CELL_BAND = HexColor("#edf1f6")
_RASTER_FORMATS = ("JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP")


def _pt(value):
    return (value or 0) / 12700


def _theme(master):
    colors = {"dk1": black, "lt1": white, "accent1": HexColor("#4472c4")}
    try:
        root = parse_xml(master.part.part_related_by(RT.THEME).blob)
    except KeyError:
        return colors | {alias: colors.get(slot) for alias, slot in _THEME_ALIASES.items()}
    for slot in root.findall(f".//{qn('a:clrScheme')}/*"):
        child = next(iter(slot), None)
        if child is not None:
            name = slot.tag.rsplit("}", 1)[-1]
            hex_code = child.get("lastClr") if child.tag == qn("a:sysClr") else child.get("val")
            if hex_code and len(hex_code) == 6:
                colors[name] = HexColor(f"#{hex_code}")
    color_map = master.element.find(qn("p:clrMap"))
    if color_map is not None:
        colors.update({key: colors.get(slot) for key, slot in color_map.attrib.items() if slot in colors})
    return {alias: colors.get(slot) for alias, slot in _THEME_ALIASES.items()} | colors


def _color(color, theme, default=None):
    if color.type == MSO_COLOR_TYPE.RGB:
        return HexColor(f"#{color.rgb}")
    if color.type == MSO_COLOR_TYPE.SCHEME:
        return theme.get(color.theme_color.xml_value) or default
    return default


def _fill(fill, theme, default=None):
    if fill.type == MSO_FILL.SOLID:
        return _color(fill.fore_color, theme, default)
    if fill.type == MSO_FILL.BACKGROUND:
        return None
    return default


def _xml_color(element, theme, default=None):
    if element is None:
        return default
    if (rgb := element.find(qn("a:srgbClr"))) is not None:
        return HexColor(f"#{rgb.get('val')}")
    if (scheme := element.find(qn("a:schemeClr"))) is not None:
        return theme.get(scheme.get("val")) or default
    return default


def _style_color(shape, name, theme, default=None):
    reference = shape.element.find(f"{qn('p:style')}/{qn('a:' + name)}")
    if reference is not None and reference.get("idx") in {"0", "1000"}:
        return None
    return _xml_color(reference, theme, default)


def _background(slide, theme):
    for surface in (slide, slide.slide_layout, slide.slide_layout.slide_master):
        background = surface.element.find(f"{qn('p:cSld')}/{qn('p:bg')}")
        if background is None:
            continue
        solid = background.find(f"{qn('p:bgPr')}/{qn('a:solidFill')}")
        return _xml_color(solid, theme) or _xml_color(background.find(qn("p:bgRef")), theme)
    return None


def _font_size(value, default):
    return min(400, max(1, _pt(value))) if value is not None else default


def _paragraph_text(paragraph, theme, size, color):
    runs = iter(paragraph.runs)
    fragments = []
    largest = 0
    for element in paragraph._p:
        if element.tag == qn("a:br"):
            fragments.append("<br/>")
        elif element.tag == qn("a:fld"):
            fragments.append(escape(element.findtext(qn("a:t"), "")))
            largest = max(largest, size)
        elif element.tag == qn("a:r"):
            run = next(runs)
            font = run.font
            actual_size = _font_size(font.size, size)
            largest = max(largest, actual_size)
            run_color = _color(font.color, theme, color)
            bold = paragraph.font.bold if font.bold is None else font.bold
            italic = paragraph.font.italic if font.italic is None else font.italic
            text = escape(run.text).replace("\t", "&#160;" * 4)
            if font.underline:
                text = f"<u>{text}</u>"
            fragments.append(f'<font name="{font_name(bool(bold), bool(italic))}" size="{actual_size}" color="{run_color.hexval()}">{text}</font>')
    return "".join(fragments) or "&#160;", largest or size


def _text(canvas, frame, x, y, width, height, theme, *, size, color, placeholder=None, inherited=None, margins=None):
    left, right, top, bottom = map(_pt, margins or (frame.margin_left, frame.margin_right, frame.margin_top, frame.margin_bottom))
    width -= left + right
    if width <= 1:
        return
    default = inherited.paragraphs[0] if inherited is not None else None
    lines = []
    for paragraph in frame.paragraphs:
        inherited_size = _font_size(default.font.size, size) if default is not None else size
        base_size = _font_size(paragraph.font.size, inherited_size)
        paragraph_color = _color(paragraph.font.color, theme, color)
        markup, largest = _paragraph_text(paragraph, theme, base_size, paragraph_color)
        alignment = paragraph.alignment or (default.alignment if default is not None else None)
        p_pr = paragraph._p.pPr
        bullet_char = p_pr.find(qn("a:buChar")) if p_pr is not None else None
        if bullet_char is not None:
            bullet = bullet_char.get("char")
        elif placeholder in _BODY and (p_pr is None or p_pr.find(qn("a:buNone")) is None) and paragraph.text.strip():
            bullet = "•"
        else:
            bullet = None
        indentation = paragraph.level * 18 if bullet else 0
        line_spacing = paragraph.line_spacing
        if type(line_spacing) is float:
            leading = largest * line_spacing
        elif line_spacing is not None:
            leading = _pt(line_spacing)
        else:
            leading = largest * 1.15
        style = ParagraphStyle(
            "Slide", fontName=font_name(bool(paragraph.font.bold), bool(paragraph.font.italic)), fontSize=base_size,
            textColor=paragraph_color, leading=max(1, leading), leftIndent=indentation + (base_size if bullet else 0),
            bulletIndent=indentation, bulletFontName=font_name(), bulletFontSize=base_size,
            alignment=_ALIGNMENTS.get(alignment, TA_CENTER if placeholder == PP_PLACEHOLDER.CENTER_TITLE else TA_LEFT),
            wordWrap="CJK" if any("\u2e80" <= char <= "\ud7ff" for char in paragraph.text) else None,
        )
        item = Paragraph(markup, style, bulletText=bullet)
        _, block_height = item.wrap(width, height)
        lines.append((item, block_height, _pt(paragraph.space_before), _pt(paragraph.space_after)))
    cursor = y + height - top
    available = height - top - bottom
    total = sum(block_height + before + after for _, block_height, before, after in lines)
    anchor = frame.vertical_anchor or (inherited.vertical_anchor if inherited is not None else None)
    if anchor == MSO_ANCHOR.MIDDLE:
        cursor -= max(0, available - total) / 2
    elif anchor == MSO_ANCHOR.BOTTOM:
        cursor -= max(0, available - total)
    for item, block_height, before, after in lines:
        cursor -= before + block_height
        item.drawOn(canvas, x + left, cursor)
        cursor -= after


def _picture(canvas, shape, width, height):
    try:
        with Image.open(BytesIO(shape.image.blob), formats=_RASTER_FORMATS) as image:
            iw, ih = image.size
            if iw * ih > 16_000_000:
                return
            left = round(iw * shape.crop_left)
            top = round(ih * shape.crop_top)
            right = round(iw * (1 - shape.crop_right))
            bottom = round(ih * (1 - shape.crop_bottom))
            x1, y1, x2, y2 = max(0, left), max(0, top), min(iw, right), min(ih, bottom)
            if x1 >= x2 or y1 >= y2:
                return
            scale_x, scale_y = width / (right - left), height / (bottom - top)
            draw_width, draw_height = (x2 - x1) * scale_x, (y2 - y1) * scale_y
            cropped = image.crop((x1, y1, x2, y2)) if (x1, y1, x2, y2) != (0, 0, iw, ih) else image
            cropped.thumbnail((max(1, int(draw_width * 2)), max(1, int(draw_height * 2))), Image.Resampling.LANCZOS)
            canvas.drawImage(ImageReader(cropped), (x1 - left) * scale_x, (bottom - y2) * scale_y,
                             draw_width, draw_height, mask="auto")
    except (KeyError, OSError, Image.DecompressionBombError):
        # An unsupported embedded image should not hide the rest of the slide.
        pass


def _table(canvas, table, height, theme):
    widths = [_pt(column.width) for column in table.columns]
    heights = [_pt(row.height) for row in table.rows]
    top = height
    for row_index, row_height in enumerate(heights):
        left = 0
        for column_index, column_width in enumerate(widths):
            cell = table.cell(row_index, column_index)
            if not cell.is_spanned:
                width = sum(widths[column_index:column_index + cell.span_width])
                row_span = sum(heights[row_index:row_index + cell.span_height])
                header = row_index == 0 and table.first_row
                default = theme["accent1"] if header else (_CELL_BAND if table.horz_banding and row_index % 2 else white)
                fill = _fill(cell.fill, theme, default)
                canvas.setStrokeColor(_CELL_BORDER)
                canvas.setLineWidth(0.5)
                if fill is not None:
                    canvas.setFillColor(fill)
                canvas.rect(left, top - row_span, width, row_span, stroke=1, fill=int(fill is not None))
                _text(canvas, cell.text_frame, left, top - row_span, width, row_span, theme, size=14,
                      color=white if header and fill == default else black,
                      margins=(cell.margin_left, cell.margin_right, cell.margin_top, cell.margin_bottom))
            left += column_width
        top -= row_height


def _line(canvas, shape, theme, default=None):
    color = _fill(shape.line.fill, theme, _style_color(shape, "lnRef", theme, default))
    if color is None:
        return False
    canvas.setStrokeColor(color)
    canvas.setLineWidth(_pt(shape.line.width) or 1)
    return True


def _geometry(canvas, shape, width, height, theme):
    properties = shape.element.find(qn("p:spPr"))
    preset = properties.find(qn("a:prstGeom")) if properties is not None else None
    kind = preset.get("prst") if preset is not None else None
    fill = _fill(shape.fill, theme, _style_color(shape, "fillRef", theme))
    stroke = _line(canvas, shape, theme)
    if fill is None and not stroke:
        return
    if fill is not None:
        canvas.setFillColor(fill)
    draw = {"stroke": int(stroke), "fill": int(fill is not None)}
    if kind == "rect":
        canvas.rect(0, 0, width, height, **draw)
    elif kind == "roundRect":
        canvas.roundRect(0, 0, width, height, min(width, height) / 6, **draw)
    elif kind == "ellipse":
        canvas.ellipse(0, 0, width, height, **draw)
    elif points := _POLYGONS.get(kind):
        path = canvas.beginPath()
        path.moveTo(points[0][0] * width, points[0][1] * height)
        for x, y in points[1:]:
            path.lineTo(x * width, y * height)
        path.close()
        canvas.drawPath(path, **draw)


def _shape(canvas, shape, page_top, theme, layout=None):
    if isinstance(shape, Connector):
        if _line(canvas, shape, theme, black):
            canvas.line(_pt(shape.begin_x), page_top - _pt(shape.begin_y), _pt(shape.end_x), page_top - _pt(shape.end_y))
        return
    width, height = _pt(shape.width), _pt(shape.height)
    if min(width, height) <= 0:
        return
    x, y = _pt(shape.left), page_top - _pt(shape.top) - height
    canvas.saveState()
    try:
        canvas.translate(x + width / 2, y + height / 2)
        canvas.rotate(-shape.rotation)
        canvas.translate(-width / 2, -height / 2)
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            transform = shape.element.grpSpPr.xfrm
            child_width, child_height = _pt(transform.chExt.cx), _pt(transform.chExt.cy)
            if child_width > 0 and child_height > 0:
                canvas.scale(width / child_width, height / child_height)
                canvas.translate(-_pt(transform.chOff.x), 0)
                for child in shape.shapes:
                    _shape(canvas, child, _pt(transform.chOff.y) + child_height, theme)
        elif isinstance(shape, Picture):
            _picture(canvas, shape, width, height)
        elif shape.has_table:
            _table(canvas, shape.table, height, theme)
        elif shape.has_text_frame:
            _geometry(canvas, shape, width, height, theme)
            frame = shape.text_frame
            if not frame.text:
                return
            kind = shape.placeholder_format.type if shape.is_placeholder else None
            inherited = layout.placeholders.get(shape.placeholder_format.idx) if shape.is_placeholder and layout is not None else None
            size = 36 if kind in _TITLES else (22 if kind else 18)
            if kind == PP_PLACEHOLDER.CENTER_TITLE:
                size = 42
            _text(canvas, frame, 0, 0, width, height, theme, size=size,
                  color=_style_color(shape, "fontRef", theme, theme.get("tx1") or black), placeholder=kind,
                  inherited=inherited.text_frame if inherited is not None and inherited.has_text_frame else None)
    finally:
        canvas.restoreState()


def render_slides(source: Path, output: Path) -> None:
    deck = Presentation(str(source))
    width, height = _pt(deck.slide_width), _pt(deck.slide_height)
    slides = [slide for slide in deck.slides if slide.element.get("show") not in {"0", "false"}]
    if not slides or not 1 <= min(width, height) <= max(width, height) <= 14400:
        raise ValueError("The presentation has no printable slides or its slide size is invalid")
    register_fonts()
    canvas = Canvas(str(output), pagesize=(width, height), pageCompression=1)
    themes = {}
    for slide in slides:
        layout = slide.slide_layout
        master = layout.slide_master
        if master.part.partname not in themes:
            themes[master.part.partname] = _theme(master)
        theme = themes[master.part.partname]
        if (background := _background(slide, theme)) is not None:
            canvas.setFillColor(background)
            canvas.rect(0, 0, width, height, fill=1, stroke=0)
        if slide.element.get("showMasterSp") not in {"0", "false"} and layout.element.get("showMasterSp") not in {"0", "false"}:
            for shape in master.shapes:
                if not shape.is_placeholder:
                    _shape(canvas, shape, height, theme)
        for shape in layout.shapes:
            if not shape.is_placeholder:
                _shape(canvas, shape, height, theme)
        for shape in slide.shapes:
            _shape(canvas, shape, height, theme, layout)
        canvas.showPage()
    canvas.save()
