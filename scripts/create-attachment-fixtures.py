"""Create synthetic native PDF, scanned PDF and PNG acceptance inputs."""

import argparse
from pathlib import Path


def main():
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdfmetrics.registerFont(
        TTFont("FixtureLatin", "/usr/share/fonts/TTF/DejaVuSans.ttf")
    )
    page = canvas.Canvas(str(args.output / "native.pdf"), pagesize=(600, 800))
    page.setFont("FixtureLatin", 22)
    page.drawString(50, 730, "SYNTHETIC FAN TEST")
    page.setFont("STSong-Light", 20)
    page.drawString(50, 680, "风扇调速测试")
    page.setFont("FixtureLatin", 18)
    for y, cells in [
        (590, ("Parameter", "Value")),
        (540, ("PWM", "128")),
        (490, ("RPM", "2400")),
    ]:
        page.drawString(65, y, cells[0])
        page.drawString(310, y, cells[1])
    for y in (620, 570, 520, 470):
        page.line(50, y, 550, y)
    for x in (50, 295, 550):
        page.line(x, 470, x, 620)
    page.setFont("FixtureLatin", 12)
    page.drawString(50, 400, "Synthetic fixture only. Not a hardware procedure.")
    page.save()

    image = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 38)
    for y, text in [
        (100, "SYNTHETIC FAN TEST"),
        (210, "风扇调速测试"),
        (360, "Parameter                 Value"),
        (460, "PWM                         128"),
        (560, "RPM                         2400"),
    ]:
        draw.text((100, y), text, fill="black", font=font)
    image.save(args.output / "scan.png")
    image.save(args.output / "scan.pdf", resolution=144)

    table_image = Image.new("RGB", (1200, 1600), "white")
    draw = ImageDraw.Draw(table_image)
    draw.text((100, 100), "SYNTHETIC FAN TEST", fill="black", font=font)
    draw.text((100, 210), "风扇调速测试", fill="black", font=font)
    draw.text((140, 345), "Fan telemetry", fill="black", font=font)
    for y, cells in [
        (445, ("Parameter", "Value")),
        (545, ("PWM", "128")),
        (645, ("RPM", "2400")),
    ]:
        draw.text((140, y), cells[0], fill="black", font=font)
        draw.text((660, y), cells[1], fill="black", font=font)
    for y in (320, 420, 520, 620, 720):
        draw.line((100, y, 1100, y), fill="black", width=3)
    for x in (100, 1100):
        draw.line((x, 320, x, 720), fill="black", width=3)
    draw.line((600, 420, 600, 720), fill="black", width=3)
    table_image.save(args.output / "scan-table.png")
    table_image.save(args.output / "scan-table.pdf", resolution=144)


if __name__ == "__main__":
    main()
