"""README / homepage hero banner from the app screenshots in docs/images.
usage (from the repo root): python -I tools/hero.py   -> docs/images/hero.png"""
import os
from PIL import Image, ImageDraw, ImageFilter, ImageFont

IMG = "docs/images"
FONTS = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
PINK, BLUE = (244, 114, 182), (76, 194, 255)  # the donut glaze and the app's accent


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def gradient(w, h):
    """Near-black with a pink glow top-left and a blue one bottom-right."""
    bg = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(bg)
    for y in range(h):
        t = y / h
        d.line((0, y, w, y), fill=(round(24 - 10 * t), round(18 - 6 * t), round(30 - 10 * t)))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-w * 0.2, -h * 0.35, w * 0.45, h * 0.4), fill=PINK + (120,))
    g.ellipse((w * 0.55, h * 0.45, w * 1.25, h * 1.35), fill=BLUE + (90,))
    return Image.alpha_composite(bg.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(min(w, h) // 5)))


def shot(name, scale=1.0, crop=None, r=10):
    im = Image.open(os.path.join(IMG, name)).convert("RGB")
    if crop:
        im = im.crop(crop)
    if scale != 1.0:
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), r, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    ImageDraw.Draw(out).rounded_rectangle((0, 0, im.width - 1, im.height - 1), r, outline=(255, 255, 255, 38))
    return out


def place(canvas, win, x, y, blur=28, drop=20, opacity=170):
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    m = Image.new("L", win.size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, win.width - 1, win.height - 1), 14, fill=opacity)
    shadow.paste((0, 0, 0, 255), (x, y + drop), m)
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))
    canvas.alpha_composite(win, (x, y))


def chips(c, labels, y, size=17):
    d = ImageDraw.Draw(c)
    cf = font("seguisb.ttf", size)
    widths = [d.textlength(t, font=cf) + 40 for t in labels]
    x0 = (c.width - (sum(widths) + 14 * (len(labels) - 1))) // 2
    glass = Image.new("RGBA", c.size, (0, 0, 0, 0))
    g, x = ImageDraw.Draw(glass), x0
    for w in widths:
        g.rounded_rectangle((x, y, x + w, y + 36), 18, fill=(255, 255, 255, 26), outline=(255, 255, 255, 80))
        x += w + 14
    c.alpha_composite(glass)
    x = x0
    for t, w in zip(labels, widths):
        d.text((x + w / 2, y + 18), t, font=cf, fill=(244, 240, 250), anchor="mm")
        x += w + 14


def hero():
    W, H = 1600, 1200
    c = gradient(W, H)
    d = ImageDraw.Draw(c)
    logo = Image.open(os.path.join(IMG, "donut.png")).convert("RGBA").resize((112, 112), Image.LANCZOS)
    title, tag = font("segoeuib.ttf", 64), font("segoeuil.ttf", 28)
    name = "Donat Downloader"
    tw = d.textlength(name, font=title)
    x0 = (W - (logo.width + 26 + tw)) // 2
    c.alpha_composite(logo, (round(x0), 62))
    d.text((x0 + logo.width + 26, 62 + logo.height // 2), name, font=title, fill=(255, 255, 255), anchor="lm")
    d.text((W // 2, 214), "Copy a link. Pick a format. Done. Video and music from YouTube and 1,700+ sites.",
           font=tag, fill=(222, 214, 232), anchor="mm")
    chips(c, ["Copy → tray card → download", "Up to 4K · MP3 320 kbps", "Clips & playlists", "One exe, nothing to install"], 254)
    place(c, shot("downloads.png", 0.8), W - round(1122 * 0.8) - 60, 340, opacity=150)
    place(c, shot("dashboard.png", 0.94), 60, 430, blur=34, drop=26, opacity=200)
    card = shot("clipboard-card.png", 1.0, crop=(20, 20, 461, 206), r=9)
    place(c, card, W - card.width - 70, H - card.height - 60, blur=26, drop=18, opacity=210)
    out = os.path.join(IMG, "hero.png")
    c.convert("RGB").save(out, optimize=True)
    print(out, c.size, os.path.getsize(out) // 1024, "KB")


def social():
    """1280x640 card in the Donat Screen layout: donut + name left, overlapping app windows right."""
    W, H = 1280, 640
    c = gradient(W, H)
    d = ImageDraw.Draw(c)
    logo = Image.open(os.path.join(IMG, "donut.png")).convert("RGBA")
    logo = logo.crop(logo.getchannel("A").getbbox()).resize((140, 140), Image.LANCZOS)
    c.alpha_composite(logo, (75, 155))
    title = font("segoeuib.ttf", 72)
    d.text((72, 322), "Donat", font=title, fill=(255, 255, 255))
    d.text((72, 398), "Downloader", font=title, fill=(255, 255, 255))
    d.text((75, 497), "YouTube & 1,700+ sites", font=font("segoeuil.ttf", 27), fill=(222, 214, 232))
    d.text((75, 535), "single exe  ·  free  ·  open source", font=font("segoeuil.ttf", 21), fill=(160, 150, 175))
    place(c, shot("downloads.png", 0.5, r=8), W - 561 - 14, 64, blur=22, drop=14, opacity=170)
    place(c, shot("dashboard.png", 0.5, r=8), 618, 262, blur=24, drop=16, opacity=200)
    out = os.path.join(IMG, "social-preview.png")
    c.convert("RGB").save(out, optimize=True)
    print(out, c.size, os.path.getsize(out) // 1024, "KB")


if __name__ == "__main__":
    hero()
    social()
