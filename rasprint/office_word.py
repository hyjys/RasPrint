"""Render the printable body of a DOCX file when LibreOffice is unavailable."""

from contextlib import suppress
from html import escape
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.table import Table as WordTable
from docx.text.paragraph import Paragraph as WordParagraph
from docx.text.run import Run
from PIL import Image as PillowImage, UnidentifiedImageError
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, NextPageTemplate, PageBreak, PageTemplate, Paragraph, Spacer, Table,
)

from .office_pdf import font_name, register_fonts


_BASIC_ALIGNMENT = {
    WD_ALIGN_PARAGRAPH.LEFT: TA_LEFT, WD_ALIGN_PARAGRAPH.CENTER: TA_CENTER,
    WD_ALIGN_PARAGRAPH.RIGHT: TA_RIGHT, WD_ALIGN_PARAGRAPH.JUSTIFY: TA_JUSTIFY,
}
_TEXT_NODES = {qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr"), qn("w:noBreakHyphen")}
_RASTER_FORMATS = {"PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP"}
_MAX_IMAGE_PIXELS = 12_000_000


def _styles(style):
    # A malformed input may create an inheritance cycle.
    seen = set()
    while style is not None and style.style_id not in seen and len(seen) < 32:
        seen.add(style.style_id)
        yield style
        style = style.base_style


def _property(objects, name, default=None):
    return next((value for obj in objects if (value := getattr(obj, name)) is not None), default)


def _points(length, default):
    return length.pt if length is not None else default


def _paragraph_style(paragraph, width, font):
    styles = tuple(_styles(paragraph.style))
    formats = (paragraph.paragraph_format, *(style.paragraph_format for style in styles))
    size = min(144, max(2, _points(_property((style.font for style in styles), "size"), 11)))
    leading = _property(formats, "line_spacing")
    if isinstance(leading, float):
        leading *= size * 1.2
    else:
        leading = _points(leading, size * 1.2)
    first_indent = _points(_property(formats, "first_line_indent"), 0)
    left = _points(_property(formats, "left_indent"), 0)
    right = _points(_property(formats, "right_indent"), 0)
    style = ParagraphStyle(
        "Word", fontName=font, fontSize=size, leading=min(288, max(size, leading)), autoLeading="max",
        alignment=_BASIC_ALIGNMENT.get(_property(formats, "alignment"), TA_LEFT),
        leftIndent=min(width / 3, max(0, left)), rightIndent=min(width / 3, max(0, right)),
        firstLineIndent=max(-width / 3, min(width / 3, first_indent)),
        spaceBefore=min(144, max(0, _points(_property(formats, "space_before"), 0))),
        spaceAfter=min(144, max(0, _points(_property(formats, "space_after"), 6))),
        keepWithNext=_property(formats, "keep_with_next", False),
    )
    return style, _property(formats, "page_break_before", False), tuple(item.font for item in styles)


def _text_style(run, inherited):
    fonts = (run.font, *(style.font for style in _styles(run.style)), *inherited)
    name = font_name(_property(fonts, "bold", False), _property(fonts, "italic", False))
    size = _property(fonts, "size")
    color = next((value for font in fonts if (value := font.color.rgb) is not None), None)
    attrs = [f'name="{name}"']
    if size is not None:
        attrs.append(f'size="{min(144, max(2, size.pt))}"')
    if color is not None:
        attrs.append(f'color="#{color}"')
    opening = f'<font {" ".join(attrs)}>'
    closing = "</font>"
    for enabled, tag in ((_property(fonts, "underline"), "u"), (_property(fonts, "strike"), "strike"),
                         (_property(fonts, "superscript"), "super"), (_property(fonts, "subscript"), "sub")):
        if enabled:
            opening += f"<{tag}>"
            closing = f"</{tag}>" + closing
    return opening, closing


def _drawing(paragraph, node, width, height):
    blip = node.find(f".//{qn('a:blip')}")
    if blip is None or not (identifier := blip.get(qn("r:embed"))):
        return None
    part = paragraph.part.related_parts.get(identifier)
    if part is None or not part.content_type.startswith("image/"):
        return None
    data = BytesIO(part.blob)
    try:
        with PillowImage.open(data) as picture:
            px_width, px_height = picture.size
            if picture.format not in _RASTER_FORMATS or px_width * px_height > _MAX_IMAGE_PIXELS:
                return None
        extent = node.find(f".//{qn('wp:extent')}")
        w, h = px_width * .75, px_height * .75
        if extent is not None:
            w, h = (int(extent.get(name, 0)) / 12_700 for name in ("cx", "cy"))
        if min(w, h) > 0:
            scale = min(1, width / w, height / h)
            data.seek(0)
            image = Image(data, width=w * scale, height=h * scale)
            image.hAlign = {TA_LEFT: "LEFT", TA_RIGHT: "RIGHT"}.get(
                _BASIC_ALIGNMENT.get(paragraph.alignment, TA_LEFT), "CENTER",
            )
            return image
    except (UnidentifiedImageError, OSError, ValueError, PillowImage.DecompressionBombError):
        return None
    return None


def _paragraph_flows(paragraph, width, height, font, *, allow_breaks=True, label=""):
    style, break_before, inherited = _paragraph_style(paragraph, width, font)
    blocks = []
    fragments = [escape(label)] if label else []

    def flush():
        if fragments:
            blocks.append(Paragraph("".join(fragments), style))
            fragments.clear()

    # Hyperlink and field runs are rendered as text, without an active target.
    runs = paragraph._p.xpath("./w:r | ./w:hyperlink/w:r | ./w:ins/w:r | ./w:fldSimple/w:r")
    for node in runs:
        opening, closing = _text_style(Run(node, paragraph), inherited)
        run_text = []

        def flush_run():
            if run_text:
                fragments.extend((opening, "".join(run_text), closing))
                run_text.clear()

        for item in node:
            if item.tag == qn("w:drawing"):
                if image := _drawing(paragraph, item, width, height):
                    flush_run()
                    flush()
                    blocks.append(image)
                continue
            if item.tag not in _TEXT_NODES:
                continue
            if item.tag == qn("w:t"):
                run_text.append(escape(item.text or ""))
            elif item.tag == qn("w:tab"):
                run_text.append("\u00a0" * 4)
            elif item.tag == qn("w:noBreakHyphen"):
                run_text.append("‑")
            elif allow_breaks and item.get(qn("w:type")) == "page":
                flush_run()
                flush()
                blocks.append(PageBreak())
            else:
                run_text.append("<br/>")
        flush_run()
    flush()
    return blocks or [Spacer(1, style.leading + style.spaceAfter)], break_before


class _ListLabels:
    def __init__(self, document):
        self.definitions = {}
        self.counters = {}
        with suppress(KeyError, NotImplementedError):
            root = document.part.numbering_part.element
            definitions = {}
            for abstract in root.iterfind(qn("w:abstractNum")):
                levels = {}
                for level in abstract.iterfind(qn("w:lvl")):
                    kind = level.find(qn("w:numFmt"))
                    start = level.find(qn("w:start"))
                    levels[level.get(qn("w:ilvl"))] = (
                        kind.get(qn("w:val"), "bullet") if kind is not None else "bullet",
                        int(start.get(qn("w:val"), 1)) if start is not None else 1,
                    )
                definitions[abstract.get(qn("w:abstractNumId"))] = levels
            for instance in root.iterfind(qn("w:num")):
                reference = instance.find(qn("w:abstractNumId"))
                if reference is not None:
                    identifier = reference.get(qn("w:val"))
                    self.definitions[instance.get(qn("w:numId"))] = definitions.get(identifier, {})

    def label(self, paragraph):
        for element in (paragraph._p, *(style.element for style in _styles(paragraph.style))):
            ids = element.xpath("./w:pPr/w:numPr/w:numId/@w:val")
            if not ids:
                continue
            levels = element.xpath("./w:pPr/w:numPr/w:ilvl/@w:val")
            number_id, level = ids[0], levels[0] if levels else "0"
            kind, start = self.definitions.get(number_id, {}).get(level, ("none", 1))
            if kind == "none":
                return ""
            if kind == "bullet":
                return "\u2022\u00a0\u00a0"
            key = number_id, level
            number = self.counters.get(key, start)
            self.counters[key] = number + 1
            return f"{number}.\u00a0\u00a0"
        return ""


def _table_flow(table, width, height, font, labels):
    widths = [max(1, _points(column.width, 1)) for column in table.columns]
    if not widths or not table.rows:
        return Spacer(1, 1)
    unit = width / sum(widths)
    widths = [column * unit for column in widths]
    rows = []
    styles = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), .4, colors.lightgrey)]
    seen_cells = set()
    for row_index, row in enumerate(table.rows):
        contents = [""] * len(widths)
        for column_index, cell in enumerate(row.cells, start=getattr(row, "grid_cols_before", 0)):
            if column_index >= len(widths) or cell._tc in seen_cells:
                continue
            seen_cells.add(cell._tc)
            end = min(column_index + cell._tc.grid_span, len(widths))
            cell_width = max(1, sum(widths[column_index:end]) - 12)
            for paragraph in cell.paragraphs:
                flows, _ = _paragraph_flows(paragraph, cell_width, height * .8, font, allow_breaks=False,
                                            label=labels.label(paragraph))
                if not contents[column_index]:
                    contents[column_index] = []
                contents[column_index].extend(flows)
            if end > column_index + 1:
                styles.append(("SPAN", (column_index, row_index), (end - 1, row_index)))
            fills = cell._tc.xpath("./w:tcPr/w:shd/@w:fill")
            if fills and len(fills[0]) == 6:
                with suppress(ValueError):
                    styles.append(("BACKGROUND", (column_index, row_index), (end - 1, row_index),
                                   colors.HexColor(f"#{fills[0]}")))
        rows.append(contents)
    repeat = int(bool(table.rows[0]._tr.xpath("./w:trPr/w:tblHeader")))
    return Table(rows, colWidths=widths, style=styles, repeatRows=repeat, splitInRow=1, hAlign="LEFT")


def _page_layout(section, index):
    size = (min(2880, max(144, _points(section.page_width, 612))),
            min(2880, max(144, _points(section.page_height, 792))))
    left = min(size[0] / 3, max(0, _points(section.left_margin, 72)))
    right = min(size[0] / 3, max(0, _points(section.right_margin, 72)))
    top = min(size[1] / 3, max(0, _points(section.top_margin, 72)))
    bottom = min(size[1] / 3, max(0, _points(section.bottom_margin, 72)))
    width, height = size[0] - left - right, size[1] - top - bottom
    frame = Frame(left, bottom, width, height, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    return PageTemplate(id=str(index), frames=[frame], pagesize=size), width, height


def render_word(source: Path, output: Path) -> None:
    document = Document(str(source))
    font = register_fonts()
    labels = _ListLabels(document)
    layouts = [_page_layout(section, index) for index, section in enumerate(document.sections)]
    if not layouts:
        raise ValueError("Missing Word section settings")
    section_index = 0
    _, width, height = layouts[section_index]
    story = []
    for element in document.element.body:
        if element.tag == qn("w:p"):
            paragraph = WordParagraph(element, document)
            blocks, break_before = _paragraph_flows(paragraph, width, height, font, label=labels.label(paragraph))
            if break_before and story and not isinstance(story[-1], PageBreak):
                story.append(PageBreak())
            story.extend(blocks)
            if element.xpath("./w:pPr/w:sectPr") and section_index + 1 < len(layouts):
                section_index += 1
                template, width, height = layouts[section_index]
                story.append(NextPageTemplate(template.id))
                if document.sections[section_index].start_type != WD_SECTION_START.CONTINUOUS:
                    story.append(PageBreak())
        elif element.tag == qn("w:tbl"):
            story.append(_table_flow(WordTable(element, document), width, height, font, labels))
    if not story or isinstance(story[-1], PageBreak):
        story.append(Spacer(1, 1))
    pdf = BaseDocTemplate(str(output), pagesize=layouts[0][0].pagesize,
                          title=document.core_properties.title or "Word document")
    pdf.addPageTemplates([template for template, _, _ in layouts])
    pdf.build(story)
