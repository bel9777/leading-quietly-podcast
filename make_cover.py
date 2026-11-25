"""Generate 1400x1400 Apple Podcasts cover art for Leading Quietly."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

S = 1400
NAVY = "#0F2537"
SLATE = "#1A3A52"
GOLD = "#D4A853"
CREAM = "#F5F2EB"

img = Image.new("RGB", (S, S), NAVY)
d = ImageDraw.Draw(img)
d.rectangle([0, 0, S, 48], fill=GOLD)
d.rectangle([0, S - 48, S, S], fill=GOLD)
d.rectangle([110, 810, S - 110, 822], fill=GOLD)

fonts = Path(r"C:\Windows\Fonts")
serif = str(fonts / "georgiab.ttf")
sans = str(fonts / "segoeuib.ttf")
sans_reg = str(fonts / "segoeui.ttf")


def fit(text, font_path, start, max_w):
    size = start
    while size > 36:
        f = ImageFont.truetype(font_path, size)
        if d.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return f


f_tag = fit("DAILY LEADERSHIP COACHING", sans, 54, S - 240)
d.text((S / 2, 210), "DAILY LEADERSHIP COACHING", font=f_tag, fill=GOLD, anchor="mm")

f1 = fit("LEADING", serif, 230, S - 220)
d.text((S / 2, 470), "LEADING", font=f1, fill=CREAM, anchor="mm")

f2 = fit("QUIETLY", serif, 230, S - 220)
d.text((S / 2, 680), "QUIETLY", font=f2, fill=CREAM, anchor="mm")

f3 = fit("PREPARED · CLEAR · STEADY", sans_reg, 62, S - 240)
d.text((S / 2, 955), "PREPARED · CLEAR · STEADY", font=f3, fill=CREAM, anchor="mm")

f4 = fit("LEADERSHIP, MY WAY", sans, 48, S - 260)
d.text((S / 2, 1080), "LEADERSHIP, MY WAY", font=f4, fill=GOLD, anchor="mm")

out = Path(__file__).resolve().parent / "docs" / "cover.png"
out.parent.mkdir(parents=True, exist_ok=True)
img.save(out)
print("wrote", out)
