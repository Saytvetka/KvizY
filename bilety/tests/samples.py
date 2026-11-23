"""Образцы материалов для тестов: PDF, Word, PowerPoint, текст, фото, скан."""
import io

LECTURE_1 = """Электролиз

Электролиз — это окислительно-восстановительный процесс, который идёт на электродах при прохождении тока через раствор или расплав.
На катоде идёт восстановление, а на аноде идёт окисление.
Законы Фарадея связывают массу вещества на электроде с количеством прошедшего электричества.
Электролиз применяют для получения алюминия, хлора и щелочей.
Гальванопластика основана на электролизе и позволяет покрывать изделия тонким слоем металла.
"""

LECTURE_2 = """Коррозия металлов

Коррозия — это самопроизвольное разрушение металлов под действием окружающей среды.
Химическая коррозия идёт без электрического тока, электрохимическая коррозия сопровождается током.
Ржавчина образуется при коррозии железа во влажном воздухе.
Для защиты от коррозии используют покрытия, ингибиторы и протекторную защиту.
Нержавеющая сталь содержит хром, который образует плотную оксидную плёнку.
"""


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
LECTURE_EN = """Electrolysis

Electrolysis is a redox process that takes place on electrodes when current passes through a solution or melt.
Reduction happens at the cathode and oxidation happens at the anode.
Faraday laws link the mass of a substance on the electrode with the amount of electricity.
"""


def pdf_bytes(text: str = LECTURE_1) -> bytes:
    """PDF с текстом. В лёгком образе контейнера нет шрифта с кириллицей — тогда текст английский
    (сам разбор PDF с русским текстом проверяется в первом шаге CI, где шрифт есть)."""
    import os

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font = "Helvetica"
    if os.path.exists(FONT):
        pdfmetrics.registerFont(TTFont("DV", FONT))
        font = "DV"
    else:
        text = LECTURE_EN
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont(font, 11)
    y = 800
    for line in text.split("\n"):
        while len(line) > 90:
            c.drawString(40, y, line[:90]); y -= 16; line = line[90:]
        c.drawString(40, y, line); y -= 16
    c.save()
    return buf.getvalue()


def scan_pdf_bytes() -> bytes:
    """PDF без текста — только картинка (как скан)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, "PDF")
    return buf.getvalue()


def docx_bytes(text: str = LECTURE_2) -> bytes:
    from docx import Document
    d = Document()
    lines = text.strip().split("\n")
    d.add_heading(lines[0], 1)
    for line in lines[1:]:
        if line.strip():
            d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def pptx_bytes() -> bytes:
    from pptx import Presentation
    p = Presentation()
    for i, line in enumerate([x for x in LECTURE_1.split("\n") if x.strip()][:5]):
        s = p.slides.add_slide(p.slide_layouts[1])
        s.shapes.title.text = f"Слайд про электролиз {i + 1}"
        s.placeholders[1].text = line
    buf = io.BytesIO()
    p.save(buf)
    return buf.getvalue()


def jpg_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(buf, "JPEG")
    return buf.getvalue()
