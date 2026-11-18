#!/usr/bin/env python3
"""
J.A.R.V.I.S. // High-Resolution Sci-Fi HUD & Arc-Reactor Icon Generator
========================================================================
Generiert per Pillow (supersampled 1024x1024 -> LANCZOS) das leuchtende
J.A.R.V.I.S. App-Icon (#070b14 Carbon-Basis, #00f0ff Cyan-Kern, #ff7b29 Akzente):
  1. assets/jarvis_icon.png (512x512) & assets/jarvis_icon.ico (Multi-Size bis 256x256)
  2. mobile_ui/icon-192.png & mobile_ui/icon-512.png (PWA Manifest Icons)
  3. Android Mipmap PNGs (48, 72, 96, 144, 192, 512) in assets/android_res/
     sowie direkt in android/app/src/main/res/mipmap-*
  4. Fertige_Pi_Version/assets/jarvis_icon.png (Raspberry Pi 5 Desktop-Logo)
"""

from __future__ import annotations

import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = ROOT_DIR / "assets"
ANDROID_RES_STAGING = ASSETS_DIR / "android_res"
MOBILE_UI_DIR = ROOT_DIR / "mobile_ui"
ANDROID_MAIN_RES = ROOT_DIR / "android" / "app" / "src" / "main" / "res"
PI_ASSETS_DIR = ROOT_DIR / "Fertige_Pi_Version" / "assets"

BG_DARK = (7, 11, 20, 255)          # #070b14 Carbon
BG_INNER = (11, 18, 34, 255)
CYAN = (0, 240, 255, 255)           # #00f0ff Cyan Core
CYAN_SOFT = (0, 185, 225, 190)
CYAN_DIM = (18, 86, 118, 170)
ORANGE = (255, 123, 41, 255)        # #ff7b29 Tech Accent
ORANGE_SOFT = (255, 165, 88, 220)
WHITE_CORE = (225, 253, 255, 255)


def render_jarvis_master_icon(size: int = 1024, mask_round: bool = False) -> Image.Image:
    """Rendert das J.A.R.V.I.S. Sci-Fi HUD / Arc-Reactor Master-Icon."""
    S = max(512, int(size))
    cx = cy = S / 2.0

    # 1. Hintergrund (Carbon #070b14 mit subtilem radialem HUD-Glow)
    base = Image.new("RGBA", (S, S), BG_DARK)
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)

    # Äußerer dunkler Ring-Verlauf + Cyan-Atmosphäre
    for step in range(18, 0, -1):
        frac = step / 18.0
        r = S * 0.44 * frac
        alpha = int(28 * (1.0 - frac) + 12)
        gdraw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(0, 225, 255, alpha),
        )
    # Zentrales starkes Cyan-Leuchten
    for step in range(10, 0, -1):
        frac = step / 10.0
        r = S * 0.21 * frac
        alpha = int(68 * (1.0 - frac * 0.7))
        gdraw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=(0, 240, 255, alpha),
        )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=S * 0.025))
    img = Image.alpha_composite(base, glow)
    draw = ImageDraw.Draw(img)

    # 2. Subtiles Tech-Fadenkreuz im Hintergrund
    line_r0 = S * 0.43
    line_r1 = S * 0.27
    for deg in (0, 90, 180, 270):
        rad = math.radians(deg)
        x0 = cx + math.cos(rad) * line_r1
        y0 = cy + math.sin(rad) * line_r1
        x1 = cx + math.cos(rad) * line_r0
        y1 = cy + math.sin(rad) * line_r0
        draw.line([(x0, y0), (x1, y1)], fill=(0, 240, 255, 95), width=max(2, int(S * 0.006)))

    # 3. Äußerer dünner HUD-Rahmenring + feine Skalenstriche
    r_outer_ring = S * 0.435
    draw.ellipse(
        [cx - r_outer_ring, cy - r_outer_ring, cx + r_outer_ring, cy + r_outer_ring],
        outline=CYAN_DIM,
        width=max(2, int(S * 0.008)),
    )
    for deg in range(0, 360, 10):
        rad = math.radians(deg)
        is_major = (deg % 30 == 0)
        r_a = S * (0.408 if is_major else 0.418)
        r_b = S * 0.432
        col = CYAN if is_major else CYAN_DIM
        draw.line(
            [
                (cx + math.cos(rad) * r_a, cy + math.sin(rad) * r_a),
                (cx + math.cos(rad) * r_b, cy + math.sin(rad) * r_b),
            ],
            fill=col,
            width=max(2, int(S * (0.007 if is_major else 0.004))),
        )

    # 4. Segmentierter äußerer Cyan-Tech-Ring (8 Arc-Reactor Spulen)
    r_seg_outer = S * 0.375
    bbox_seg_outer = [
        cx - r_seg_outer,
        cy - r_seg_outer,
        cx + r_seg_outer,
        cy + r_seg_outer,
    ]
    w_outer = max(6, int(S * 0.030))
    for i in range(8):
        start_a = i * 45 + 7
        end_a = (i + 1) * 45 - 7
        draw.arc(bbox_seg_outer, start=start_a, end=end_a, fill=CYAN, width=w_outer)

    # 5. Orangefarbene High-Tech Akzent-Segmente (#ff7b29)
    r_orange = S * 0.315
    bbox_orange = [cx - r_orange, cy - r_orange, cx + r_orange, cy + r_orange]
    w_orange = max(5, int(S * 0.022))
    for start_a, end_a in ((25, 110), (145, 230), (265, 350)):
        draw.arc(bbox_orange, start=start_a, end=end_a, fill=ORANGE, width=w_orange)

    # 6. Feiner innerer Zwischenring + 6 orange Spulen-Knoten
    r_mid = S * 0.255
    draw.ellipse(
        [cx - r_mid, cy - r_mid, cx + r_mid, cy + r_mid],
        outline=CYAN_SOFT,
        width=max(3, int(S * 0.010)),
    )
    for i in range(6):
        rad = math.radians(i * 60 + 30)
        nx = cx + math.cos(rad) * S * 0.255
        ny = cy + math.sin(rad) * S * 0.255
        nr = S * 0.019
        draw.ellipse([nx - nr, ny - nr, nx + nr, ny + nr], fill=ORANGE_SOFT, outline=BG_DARK, width=max(2, int(S * 0.004)))

    # 7. Arc-Reactor Dreieck-Tech-Rahmen um den Kern + leuchtender Cyan-Kern
    r_core_ring = S * 0.185
    draw.ellipse(
        [cx - r_core_ring, cy - r_core_ring, cx + r_core_ring, cy + r_core_ring],
        fill=BG_INNER,
        outline=CYAN,
        width=max(4, int(S * 0.015)),
    )

    # Hexagon/Reaktor-Strahlen zum Kern
    for i in range(3):
        rad = math.radians(i * 120 - 90)
        px0 = cx + math.cos(rad) * (S * 0.09)
        py0 = cy + math.sin(rad) * (S * 0.09)
        px1 = cx + math.cos(rad) * (S * 0.175)
        py1 = cy + math.sin(rad) * (S * 0.175)
        draw.line([(px0, py0), (px1, py1)], fill=CYAN, width=max(3, int(S * 0.011)))

    # Leuchtender Arc-Kern (#00f0ff + weißer Hochglanz-Fokus)
    r_core = S * 0.118
    draw.ellipse([cx - r_core, cy - r_core, cx + r_core, cy + r_core], fill=CYAN)
    r_inner_dark = S * 0.072
    draw.ellipse([cx - r_inner_dark, cy - r_inner_dark, cx + r_inner_dark, cy + r_inner_dark], fill=BG_DARK, outline=WHITE_CORE, width=max(3, int(S * 0.009)))
    r_pupil = S * 0.040
    draw.ellipse([cx - r_pupil, cy - r_pupil, cx + r_pupil, cy + r_pupil], fill=WHITE_CORE)

    # Optional runde Maske (Android ic_launcher_round)
    if mask_round:
        mask = Image.new("L", (S, S), 0)
        mdraw = ImageDraw.Draw(mask)
        pad = int(S * 0.02)
        mdraw.ellipse([pad, pad, S - pad, S - pad], fill=255)
        out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        out.paste(img, (0, 0), mask)
        return out

    return img


def generate_all_icons() -> dict[str, Path]:
    """Erstellt alle Ziel-Icons (Windows .ico, PNG, Mobile PWA, Android Mipmaps, Pi 5)."""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    ANDROID_RES_STAGING.mkdir(parents=True, exist_ok=True)
    MOBILE_UI_DIR.mkdir(parents=True, exist_ok=True)
    PI_ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    master_sq = render_jarvis_master_icon(1024, mask_round=False)
    master_round = render_jarvis_master_icon(1024, mask_round=True)
    resample = Image.Resampling.LANCZOS

    generated: dict[str, Path] = {}

    # 1. Master-PNG (512x512) & Multi-Size Windows ICO (bis 256x256)
    ico_png = master_sq.resize((512, 512), resample)
    png_path = ASSETS_DIR / "jarvis_icon.png"
    ico_png.save(str(png_path), format="PNG", optimize=True)
    generated["assets_png"] = png_path

    ico_path = ASSETS_DIR / "jarvis_icon.ico"
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master_sq.resize((256, 256), resample).save(str(ico_path), format="ICO", sizes=ico_sizes)
    generated["assets_ico"] = ico_path

    # Kopie für web_ui/favicon.ico aktuell halten (ohne HUD-Layout zu berühren)
    web_fav = ROOT_DIR / "web_ui" / "favicon.ico"
    if (ROOT_DIR / "web_ui").exists():
        master_sq.resize((256, 256), resample).save(str(web_fav), format="ICO", sizes=ico_sizes)
        generated["web_favicon"] = web_fav

    # 2. Raspberry Pi 5 Asset-Icon
    pi_png = PI_ASSETS_DIR / "jarvis_icon.png"
    ico_png.save(str(pi_png), format="PNG", optimize=True)
    generated["pi_png"] = pi_png

    # 3. PWA Manifest Icons in mobile_ui/
    for sz in (192, 512):
        out_p = MOBILE_UI_DIR / f"icon-{sz}.png"
        master_sq.resize((sz, sz), resample).save(str(out_p), format="PNG", optimize=True)
        generated[f"pwa_{sz}"] = out_p

    # 4. Android Mipmap & Adaptive Größen (48, 72, 96, 144, 192, 512)
    density_map = {
        "mipmap-mdpi": 48,
        "mipmap-hdpi": 72,
        "mipmap-xhdpi": 96,
        "mipmap-xxhdpi": 144,
        "mipmap-xxxhdpi": 192,
    }
    for folder, px in density_map.items():
        sq_resized = master_sq.resize((px, px), resample)
        rd_resized = master_round.resize((px, px), resample)

        stage_dir = ANDROID_RES_STAGING / folder
        stage_dir.mkdir(parents=True, exist_ok=True)
        sq_resized.save(str(stage_dir / "ic_launcher.png"), format="PNG", optimize=True)
        rd_resized.save(str(stage_dir / "ic_launcher_round.png"), format="PNG", optimize=True)
        sq_resized.save(str(stage_dir / "ic_launcher_foreground.png"), format="PNG", optimize=True)

        if ANDROID_MAIN_RES.exists():
            dst_dir = ANDROID_MAIN_RES / folder
            dst_dir.mkdir(parents=True, exist_ok=True)
            sq_resized.save(str(dst_dir / "ic_launcher.png"), format="PNG", optimize=True)
            rd_resized.save(str(dst_dir / "ic_launcher_round.png"), format="PNG", optimize=True)
            sq_resized.save(str(dst_dir / "ic_launcher_foreground.png"), format="PNG", optimize=True)

    # 512x512 PlayStore / High-Res Icon im Staging ablegen
    store_512 = ANDROID_RES_STAGING / "ic_launcher_512.png"
    ico_png.save(str(store_512), format="PNG", optimize=True)
    generated["android_512"] = store_512

    print("[generate_icons.py] Alle JARVIS Icons erfolgreich generiert:")
    for k, p in generated.items():
        print(f"  - {k}: {p} ({p.stat().st_size} Bytes)")
    return generated


if __name__ == "__main__":
    generate_all_icons()
