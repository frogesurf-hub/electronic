"""本地文本、PDF、表格和扫描图片解析；不调用语言模型或上传文件。"""

import json
import re
import subprocess
from pathlib import Path
from uuid import uuid4

from agent_core.retrieval.documents import _HEADING, PROJECT_ROOT

IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
SUPPORTED_TYPES = IMAGE_TYPES | {".md", ".txt", ".pdf"}


def _box(words: list[dict]) -> list[float]:
    return [
        min(w["bbox"][0] for w in words),
        min(w["bbox"][1] for w in words),
        max(w["bbox"][2] for w in words),
        max(w["bbox"][3] for w in words),
    ]


def _line_centers(values: list[int]) -> list[float]:
    groups: list[list[int]] = []
    for value in values:
        if not groups or value > groups[-1][-1] + 1:
            groups.append([])
        groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _reading_words(words: list[dict]) -> list[dict]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: w["bbox"][1]):
        if not rows or (
            word["bbox"][1] - rows[-1][0]["bbox"][1]
            > (word["bbox"][3] - word["bbox"][1]) / 2
        ):
            rows.append([])
        rows[-1].append(word)
    return [w for row in rows for w in sorted(row, key=lambda w: w["bbox"][0])]


def _ocr_tables(image, blocks: list[dict], width: float, height: float) -> list[dict]:
    """只为连续网格生成待复核候选表格；复杂版面保留 OCR 行和词坐标。"""
    mask = image.convert("L").point(lambda p: 255 if p < 120 else 0)
    iw, ih = mask.size
    ys = _line_centers(
        [
            y
            for y in range(ih)
            if mask.crop((0, y, iw, y + 1)).histogram()[255] > iw * 0.5
        ]
    )
    if not 3 <= len(ys) <= 100:
        return blocks
    top, bottom = int(ys[0]), int(ys[-1])
    xs = _line_centers(
        [
            x
            for x in range(iw)
            if mask.crop((x, top, x + 1, bottom + 1)).histogram()[255]
            > (bottom - top) * 0.85
        ]
    )
    if not 3 <= len(xs) <= 30:
        return blocks
    left, right = int(xs[0]), int(xs[-1])
    if any(
        mask.crop((left, int(y), right + 1, int(y) + 1)).histogram()[255]
        < (right - left) * 0.8
        for y in ys
    ):
        return blocks
    xs = [x * width / iw for x in xs]
    ys = [y * height / ih for y in ys]
    inside: list[dict] = []
    outside: list[dict] = []
    for block in blocks:
        x0, y0, x1, y1 = block["bbox"]
        group = (
            inside
            if (xs[0] < (x0 + x1) / 2 < xs[-1] and ys[0] < (y0 + y1) / 2 < ys[-1])
            else outside
        )
        group.append(block)
    rows = []
    words = [w for b in inside for w in b["words"]]
    for y0, y1 in zip(ys, ys[1:]):
        cells = []
        for x0, x1 in zip(xs, xs[1:]):
            selected = [
                w
                for w in words
                if (
                    x0 < (w["bbox"][0] + w["bbox"][2]) / 2 < x1
                    and y0 < (w["bbox"][1] + w["bbox"][3]) / 2 < y1
                )
            ]
            text = " ".join(w["text"] for w in _reading_words(selected))
            cells.append(re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", text))
        if not any(cells):
            return blocks
        rows.append(cells)
    header = " | ".join(rows[0])
    for row_number, cells in enumerate(rows, 1):
        text = " | ".join(cells)
        outside.append(
            {
                "kind": "table_row",
                "method": "windows_ocr_table",
                "text": text if row_number == 1 else header + "\n" + text,
                "cells": cells,
                "table_id": "OCR-T1",
                "row_number": row_number,
                "bbox": [xs[0], ys[row_number - 1], xs[-1], ys[row_number]],
                "header_bbox": [xs[0], ys[0], xs[-1], ys[1]],
                "raw_ocr_lines": [
                    b
                    for b in inside
                    if (
                        ys[row_number - 1]
                        < (b["bbox"][1] + b["bbox"][3]) / 2
                        < ys[row_number]
                    )
                ],
                "structure_status": "candidate_requires_review",
            }
        )
    return sorted(outside, key=lambda b: (b["bbox"][1], b["bbox"][0]))


def ocr_image(image, *, language: str, width: float, height: float) -> list[dict]:
    """OCR 坐标映射回页面；保留原始识别文字和每个词的位置。"""
    from PIL import ImageOps

    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((2000, 2000))
    temporary_root = PROJECT_ROOT / ".cache" / "document-ocr"
    temporary_root.mkdir(parents=True, exist_ok=True)
    path = temporary_root / f"page-{uuid4().hex}.png"
    try:
        image.save(path)
        process = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(PROJECT_ROOT / "scripts" / "ocr-image.ps1"),
                "-ImagePath",
                str(path),
                "-Language",
                language,
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        if process.returncode:
            raise ValueError("本地 OCR 失败；请检查 Windows OCR 语言和图片。")
        result = json.loads(process.stdout)
    finally:
        path.unlink(missing_ok=True)
    scale_x, scale_y = width / result["width"], height / result["height"]
    blocks = []
    for line in result["lines"]:
        if not line["text"].strip() or not line["words"]:
            continue
        words = [
            {
                "text": w["text"],
                "bbox": [
                    w["bbox"][0] * scale_x,
                    w["bbox"][1] * scale_y,
                    w["bbox"][2] * scale_x,
                    w["bbox"][3] * scale_y,
                ],
            }
            for w in line["words"]
        ]
        blocks.append(
            {
                "kind": "ocr_line",
                "text": re.sub(
                    r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])",
                    "",
                    line["text"].strip(),
                ),
                "raw_ocr_text": line["text"],
                "words": words,
                "bbox": _box(words),
                "method": "windows_ocr",
                "language": result["language"],
            }
        )
    # OCR 引擎可能按列返回表格文字；先按版面行、列排序。
    groups: list[list[dict]] = []
    for block in sorted(blocks, key=lambda b: b["bbox"][1]):
        if (
            not groups
            or abs(groups[-1][0]["bbox"][1] - block["bbox"][1])
            > (block["bbox"][3] - block["bbox"][1]) / 2
        ):
            groups.append([])
        groups[-1].append(block)
    blocks = [b for group in groups for b in sorted(group, key=lambda b: b["bbox"][0])]
    return _ocr_tables(image, blocks, width, height)


def _text_file(path: Path, title: str) -> list[dict]:
    text = path.read_bytes().decode("utf-8-sig")
    blocks: list[dict] = []
    headings: list[tuple[int, str]] = []
    section = title
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        heading = _HEADING.match(line.strip()) if path.suffix.lower() == ".md" else None
        if heading:
            level = len(heading.group(1))
            headings = [(n, t) for n, t in headings if n < level]
            headings.append((level, heading.group(2)))
            section = " / ".join(t for _, t in headings)
        blocks.append(
            {
                "kind": "heading" if heading else "text",
                "text": line,
                "line_start": line_number,
                "line_end": line_number,
                "section": section,
                "section_basis": "markdown_heading" if headings else "document_title",
                "method": "utf8_text",
                "bbox": None,
            }
        )
    return [
        {
            "number": None,
            "width": None,
            "height": None,
            "units": "lines",
            "blocks": blocks,
        }
    ]


def _pdf_pages(path: Path, title: str, language: str, ocr_mode: str) -> list[dict]:
    import pdfplumber
    import pypdfium2 as pdfium

    pages = []
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) > 100:
            raise ValueError("每份 PDF 最多 100 页。")
        for number, page in enumerate(pdf.pages, 1):
            if number == 1 and pdf.metadata.get("Encrypt"):
                raise ValueError("请先提供可直接读取的 PDF。")
            use_ocr = ocr_mode == "always" or not (page.extract_text() or "").strip()
            if use_ocr:
                if ocr_mode == "never":
                    raise ValueError(f"第 {number} 页没有文字层，需开启 OCR。")
                document = pdfium.PdfDocument(str(path))
                try:
                    pdf_page = document[number - 1]
                    try:
                        bitmap = pdf_page.render(
                            scale=min(2.5, 2000 / max(page.width, page.height))
                        )
                        try:
                            blocks = ocr_image(
                                bitmap.to_pil(),
                                language=language,
                                width=page.width,
                                height=page.height,
                            )
                        finally:
                            bitmap.close()
                    finally:
                        pdf_page.close()
                finally:
                    document.close()
            else:
                tables = page.find_tables()
                words = page.extract_words(extra_attrs=["size"])
                lines: list[list[dict]] = []
                for word in sorted(words, key=lambda w: (round(w["top"] / 3), w["x0"])):
                    if any(
                        t.bbox[0] <= (word["x0"] + word["x1"]) / 2 <= t.bbox[2]
                        and t.bbox[1] <= (word["top"] + word["bottom"]) / 2 <= t.bbox[3]
                        for t in tables
                    ):
                        continue
                    if not lines or abs(lines[-1][0]["top"] - word["top"]) > 3:
                        lines.append([])
                    lines[-1].append(word)
                blocks = []
                for line in lines:
                    line.sort(key=lambda w: w["x0"])
                    text = " ".join(w["text"] for w in line).strip()
                    blocks.append(
                        {
                            "kind": "heading"
                            if len(text) <= 80 and max(w["size"] for w in line) >= 14
                            else "text",
                            "text": text,
                            "bbox": [
                                min(w["x0"] for w in line),
                                min(w["top"] for w in line),
                                max(w["x1"] for w in line),
                                max(w["bottom"] for w in line),
                            ],
                            "method": "pdf_text",
                        }
                    )
                for table_number, table in enumerate(tables, 1):
                    rows = table.extract()
                    for row_number, row in enumerate(rows, 1):
                        values = [
                            str(cell or "").replace("\n", " ").strip() for cell in row
                        ]
                        if not any(values):
                            continue
                        header = " | ".join(
                            str(cell or "").replace("\n", " ") for cell in rows[0]
                        )
                        blocks.append(
                            {
                                "kind": "table_row",
                                "text": (header + "\n" if row_number > 1 else "")
                                + " | ".join(values),
                                "cells": values,
                                "table_id": f"P{number}-T{table_number}",
                                "row_number": row_number,
                                "bbox": list(table.rows[row_number - 1].bbox),
                                "header_bbox": list(table.rows[0].bbox),
                                "method": "pdf_table",
                            }
                        )
                blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
                # 含文字的混合页不静默声称图片里的内容已解析。
                if page.images:
                    blocks.append(
                        {
                            "kind": "warning",
                            "text": "",
                            "warning": "文字页含图片，请复核或使用 always OCR。",
                        }
                    )
            section = title
            section_basis = "document_title"
            for block in blocks:
                if block["kind"] == "heading":
                    section = block["text"]
                    section_basis = "pdf_layout_heading"
                block["section"] = section
                block["section_basis"] = section_basis
            pages.append(
                {
                    "number": number,
                    "width": page.width,
                    "height": page.height,
                    "units": "pdf_points",
                    "blocks": blocks,
                }
            )
    return pages


def parse_file(path: Path, *, title: str, language: str, ocr_mode: str) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return _text_file(path, title)
    if suffix == ".pdf":
        try:
            return _pdf_pages(path, title, language, ocr_mode)
        except (ImportError, ValueError):
            raise
        except Exception as error:
            raise ValueError(
                f"PDF 解析失败（{type(error).__name__}），请检查文件。"
            ) from None
    if suffix in IMAGE_TYPES:
        from PIL import Image, ImageOps

        if ocr_mode == "never":
            raise ValueError("图片需要 OCR，不能使用 never 模式。")
        pages = []
        with Image.open(path) as image:
            if image.width * image.height > 30_000_000:
                raise ValueError("图片超过 3000 万像素，请先分成较小页面。")
            if getattr(image, "n_frames", 1) > 100:
                raise ValueError("每份图片文件最多 100 帧。")
            for frame in range(getattr(image, "n_frames", 1)):
                image.seek(frame)
                rendered = ImageOps.exif_transpose(image.copy())
                blocks = ocr_image(
                    rendered,
                    language=language,
                    width=rendered.width,
                    height=rendered.height,
                )
                for block in blocks:
                    block["section"] = title
                    block["section_basis"] = "document_title"
                pages.append(
                    {
                        "number": frame + 1,
                        "width": rendered.width,
                        "height": rendered.height,
                        "units": "oriented_image_pixels",
                        "blocks": blocks,
                    }
                )
        return pages
    raise ValueError(f"暂不支持 {suffix} 格式。")
