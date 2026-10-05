"""生成带明确检测标记的多格式资料；不作为真实业务规则或 SQL 口径。"""

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "自动生成的导入检测资料，不是业务依据"


def create(folder: Path) -> dict:
    folder = folder.resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("检测资料目录不是空目录；不覆盖已有文件。")
    folder.mkdir(parents=True, exist_ok=True)
    font_path = Path("C:/Windows/Fonts/simsun.ttc")
    pdfmetrics.registerFont(TTFont("FixtureChinese", str(font_path)))
    (folder / "检测说明.md").write_text(
        "# 导入检测说明\n\n## 目的\n\n所有文件只用于检测导入，不是业务资料。\n"
        "## 检测标记\n\nMarkdown 检测标记 MD-READY-101。\n",
        encoding="utf-8",
    )
    (folder / "长行文本.txt").write_text(
        "长行切片检测 TXT-READY-202。\n" + "字符不得丢失或改写。" * 230,
        encoding="utf-8",
    )
    pdf = canvas.Canvas(str(folder / "文字与表格.pdf"), pagesize=A4)
    pdf.setFont("FixtureChinese", 18)
    pdf.drawString(50, 790, "文档导入检测说明（非业务依据）")
    pdf.setFont("FixtureChinese", 12)
    pdf.drawString(50, 750, "文字页检测标记 PDF-READY-303。")
    pdf.drawString(50, 722, "文件需保留版本、页码、章节和位置。")
    pdf.showPage()
    pdf.setFont("FixtureChinese", 18)
    pdf.drawString(50, 790, "表格检测（仅测试数据）")
    data = [["检测项", "检测值"], ["表格标记", "TABLE-404"], ["检测计数", "17"]]
    table = Table(data, colWidths=[180, 260], rowHeights=[36] * 3)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "FixtureChinese"),
                ("FONTSIZE", (0, 0), (-1, -1), 12),
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf2fa")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    table.wrapOn(pdf, 440, 200)
    table.drawOn(pdf, 50, 610)
    pdf.save()
    image = Image.new("RGB", (1400, 1100), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 42)
    for y, line in zip(
        [60, 160, 260, 360],
        [
            "扫描导入检测资料",
            "仅用于检测，不作为业务依据",
            "SCAN-505",
            "检查识别文字和引用位置",
        ],
        strict=True,
    ):
        draw.text((70, y), line, font=font, fill="black")
    # 扫描表格需保留位置并提示复核，不能宣称自动恢复可靠列结构。
    for y in [510, 620, 730]:
        draw.line((70, y, 1250, y), fill="black", width=3)
    for x in [70, 660, 1250]:
        draw.line((x, 510, x, 730), fill="black", width=3)
    draw.text((100, 535), "检测项", font=font, fill="black")
    draw.text((700, 535), "检测值", font=font, fill="black")
    draw.text((100, 645), "扫描表格", font=font, fill="black")
    draw.text((700, 645), "OCR-606", font=font, fill="black")
    image.save(folder / "扫描图片.png")
    pdf = canvas.Canvas(str(folder / "扫描文件.pdf"), pagesize=A4)
    pdf.drawImage(str(folder / "扫描图片.png"), 20, 200, width=555, height=436)
    pdf.save()
    documents = []
    for number, name in enumerate(
        [
            "检测说明.md",
            "长行文本.txt",
            "文字与表格.pdf",
            "扫描文件.pdf",
            "扫描图片.png",
        ],
        1,
    ):
        documents.append(
            {
                "file": name,
                "doc_id": f"IMPORT-CHECK-{number:03d}",
                "title": Path(name).stem,
                "version": "test-v1",
                "source": SOURCE,
                "valid_from": "2024-01-01",
                "valid_until": None,
                "ocr_review_sha256": None,
            }
        )
    manifest = {
        "schema_version": 1,
        "collection_id": "import_detection",
        "data_domain": "import_detection",
        "documents": documents,
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "created",
        "directory": str(folder),
        "document_count": 5,
        "note": SOURCE,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", type=Path, default=ROOT / ".local" / "document-fixtures"
    )
    args = parser.parse_args()
    print(json.dumps(create(args.target), ensure_ascii=False, indent=2))
