"""Render visible Excel worksheets as paginated PDF tables."""

from itertools import product
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.worksheet.print_settings import PrintArea
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, A4, A5, LEGAL, LETTER, TABLOID, landscape
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from .office_pdf import font_name

PAPER_SIZES = {"1": LETTER, "3": TABLOID, "5": LEGAL, "8": A3, "9": A4, "11": A5}
MAX_CELLS = 200_000
MAX_PAGES = 1_000


def worksheet_areas(sheet):
    if sheet.print_area:
        for area in PrintArea.from_string(sheet.print_area).ranges:
            yield area.bounds
    else:
        yield 1, 1, sheet.max_column, sheet.max_row


def sizes(sheet, first_col, first_row, last_col, last_row):
    default_width = sheet.sheet_format.defaultColWidth or 8.43
    default_height = sheet.sheet_format.defaultRowHeight or 15
    widths = {col: max(12, default_width * 5.25 + 3) for col in range(first_col, last_col + 1)}
    for dimension in sheet.column_dimensions.values():
        if dimension.min is None or dimension.max is None:
            continue
        for col in range(max(first_col, dimension.min), min(last_col, dimension.max) + 1):
            widths[col] = 0 if dimension.hidden else max(12, (dimension.width or default_width) * 5.25 + 3)
    heights = {}
    for row in range(first_row, last_row + 1):
        dimension = sheet.row_dimensions.get(row)
        heights[row] = (
            default_height if dimension is None
            else 0 if dimension.hidden
            else dimension.height or default_height
        )
    return widths, heights


def groups(dimensions, limit, breaks):
    result = []
    group = []
    used = 0
    for index, size in dimensions.items():
        if not size:
            continue
        if group and (used + size > limit + 0.01 or group[-1] in breaks):
            result.append(group)
            group, used = [], 0
        group.append(index)
        used += size
    if group:
        result.append(group)
    return result


def rgb_color(value, default):
    if value is not None and value.type == "rgb" and isinstance(value.rgb, str):
        return colors.HexColor("#" + value.rgb[-6:])
    return default


def draw_cell(pdf, cell, x, top, width, height):
    fill = cell.fill
    if fill.patternType == "solid":
        pdf.setFillColor(rgb_color(fill.fgColor, colors.white))
        pdf.rect(x, top - height, width, height, stroke=0, fill=1)
    pdf.setStrokeColor(colors.HexColor("#D1D5DB"))
    pdf.setLineWidth(0.3)
    pdf.rect(x, top - height, width, height)
    if cell.value is None or width < 6 or height < 5:
        return

    text = str(cell.value)
    font = font_name(cell.font.bold, cell.font.italic)
    size = min(max(cell.font.sz or 11, 5), 36)
    leading = size * 1.18
    line_limit = max(1, min(100, int(height / leading)))
    lines = []
    for line in text.splitlines():
        if cell.alignment.wrap_text:
            current = ""
            used = 0.0
            for char in line:
                advance = stringWidth(char, font, size)
                if current and used + advance > width - 5:
                    lines.append(current)
                    current, used = "", 0.0
                    if len(lines) >= line_limit:
                        break
                current += char
                used += advance
            if len(lines) < line_limit:
                lines.append(current)
        else:
            lines.append(line)
        if len(lines) >= line_limit:
            break
    if not lines:
        return

    pdf.saveState()
    clip = pdf.beginPath()
    clip.rect(x + 2, top - height, width - 4, height)
    pdf.clipPath(clip, stroke=0, fill=0)
    pdf.setFont(font, size)
    pdf.setFillColor(rgb_color(cell.font.color, colors.black))
    alignment = cell.alignment.horizontal
    vertical = cell.alignment.vertical
    if vertical == "top":
        y = top - size
    elif vertical == "center":
        y = top - (height - len(lines) * leading) / 2 - size
    else:
        y = top - height + 2 + (len(lines) - 1) * leading
    for line in lines:
        if alignment == "center":
            pdf.drawCentredString(x + width / 2, y, line)
        elif alignment == "right" or (alignment in {None, "general"} and cell.data_type == "n"):
            pdf.drawRightString(x + width - 2, y, line)
        else:
            pdf.drawString(x + 2, y, line)
        y -= leading
    pdf.restoreState()


def offsets(indices, dimensions):
    positions = {}
    offset = 0
    for index in indices:
        positions[index] = offset
        offset += dimensions[index]
    return positions


def draw_sheet_page(pdf, sheet, rows, columns, heights, widths):
    x_positions = offsets(columns, widths)
    y_positions = offsets(rows, heights)
    merged = {}
    covered = set()
    for area in sheet.merged_cells.ranges:
        if area.min_row > rows[-1] or area.max_row < rows[0] or area.min_col > columns[-1] or area.max_col < columns[0]:
            continue
        visible_rows = [row for row in rows if area.min_row <= row <= area.max_row]
        visible_cols = [col for col in columns if area.min_col <= col <= area.max_col]
        if not visible_rows or not visible_cols:
            continue
        first = visible_rows[0], visible_cols[0]
        merged[first] = (
            sheet.cell(area.min_row, area.min_col),
            sum(widths[col] for col in visible_cols),
            sum(heights[row] for row in visible_rows),
        )
        covered.update(product(visible_rows, visible_cols))
    for row, col in product(rows, columns):
        key = row, col
        if key in merged:
            cell, width, height = merged[key]
        elif key in covered:
            continue
        else:
            cell, width, height = sheet.cell(row, col), widths[col], heights[row]
        draw_cell(pdf, cell, x_positions[col], -y_positions[row], width, height)


def render_excel(source: Path, output: Path) -> None:
    workbook = load_workbook(source, data_only=True, keep_links=False)
    try:
        pdf = Canvas(str(output), pagesize=A4, pageCompression=1)
        total_cells = page_count = 0
        for sheet in workbook.worksheets:
            if sheet.sheet_state != "visible":
                continue
            setup = sheet.page_setup
            page = PAPER_SIZES.get(str(setup.paperSize), A4)
            page = landscape(page) if setup.orientation == "landscape" else page
            left = max(0, min(sheet.page_margins.left * 72, page[0] / 3))
            right = max(0, min(sheet.page_margins.right * 72, page[0] / 3))
            top = max(0, min(sheet.page_margins.top * 72, page[1] / 3))
            bottom = max(0, min(sheet.page_margins.bottom * 72, page[1] / 3))
            page_width, page_height = page[0] - left - right, page[1] - top - bottom
            fit = bool(sheet.sheet_properties.pageSetUpPr.fitToPage)
            row_breaks = {item.id for item in sheet.row_breaks.brk} if not fit else set()
            col_breaks = {item.id for item in sheet.col_breaks.brk} if not fit else set()
            for first_col, first_row, last_col, last_row in worksheet_areas(sheet):
                total_cells += (last_col - first_col + 1) * (last_row - first_row + 1)
                if total_cells > MAX_CELLS:
                    raise ValueError("Workbook print area exceeds the cell limit")
                widths, heights = sizes(sheet, first_col, first_row, last_col, last_row)
                scale = max(0.1, min(4, (setup.scale or 100) / 100))
                if fit:
                    scale = 1.0
                    if setup.fitToWidth and sum(widths.values()):
                        scale = min(scale, page_width * setup.fitToWidth / sum(widths.values()))
                    if setup.fitToHeight and sum(heights.values()):
                        scale = min(scale, page_height * setup.fitToHeight / sum(heights.values()))
                row_groups = groups(heights, page_height / scale, row_breaks)
                col_groups = groups(widths, page_width / scale, col_breaks)
                page_count += len(row_groups) * len(col_groups)
                if page_count > MAX_PAGES:
                    raise ValueError("Workbook exceeds the page limit")
                pages = (
                    product(row_groups, col_groups) if setup.pageOrder == "overThenDown"
                    else ((rows, cols) for cols, rows in product(col_groups, row_groups))
                )
                for rows, cols in pages:
                    pdf.setPageSize(page)
                    pdf.saveState()
                    clip = pdf.beginPath()
                    clip.rect(left, bottom, page_width, page_height)
                    pdf.clipPath(clip, stroke=0, fill=0)
                    pdf.translate(left, page[1] - top)
                    pdf.scale(scale, scale)
                    draw_sheet_page(pdf, sheet, rows, cols, heights, widths)
                    pdf.restoreState()
                    pdf.showPage()
        if not page_count:
            pdf.showPage()
        pdf.save()
    finally:
        workbook.close()
