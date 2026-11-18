# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. Set-of-Mark (SoM) UI Element Tagger (ui_som_tagger.py)
====================================================================
UPGRADE 2: Automatische UI-Element-Erkennung für Windows-Programme & Browser.
- Liest sichtbare interaktive Steuerelemente des aktiven Vordergrundfensters
  über die Windows-UI-Tree- / Control-API (`ctypes`) aus.
- Ergänzt moderne Toolbars / WebViews / CAD- & Slicer-Panels per extrem
  schneller OpenCV-Kontur-/Button-Erkennung.
- Zeichnet kontrastreiche gelbe Nummernschilder `[1]`, `[2]`, `[3]` ... direkt
  auf das Vision-Screenshot-Bild und liefert die strukturierte Tag-Liste sowie
  eine 100%-exakte Auflösung von `tag_id -> (screen_x, screen_y)`.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("jarvis.ui_som")

IS_WINDOWS = platform.system().lower() == "windows"

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    CV2_AVAILABLE = True
except Exception:
    cv2 = None  # type: ignore
    np = None  # type: ignore
    CV2_AVAILABLE = False

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    PIL_AVAILABLE = True
except Exception:
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore
    PIL_AVAILABLE = False


_INTERACTIVE_CLASS_MAP: dict[str, str] = {
    "button": "Button",
    "tbutton": "Button",
    "pushbutton": "Button",
    "edit": "Eingabefeld",
    "richedit": "Eingabefeld",
    "richedit20w": "Eingabefeld",
    "combobox": "Auswahlfeld",
    "syslink": "Link",
    "systabcontrol32": "Tab",
    "toolbarwindow32": "Toolbar",
    "tooltips_class32": "",
    "msctls_trackbar32": "Regler",
    "syslistview32": "Liste",
    "systreeview32": "Baumansicht",
}

_EXCLUDED_WINDOW_CLASSES = {
    "progman",
    "workerw",
    "shell_traywnd",
    "tooltips_class32",
    "#32769",
}


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class UISetOfMarkTagger:
    """Erkennt interaktive UI-Elemente, zeichnet `[1]..[N]` SoM-Tags und löst `tag_id` exakt auf."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_tags: dict[int, dict[str, Any]] = {}
        self._last_updated_at: float = 0.0
        self._last_screen_size: tuple[int, int] = (1920, 1080)

    def _enumerate_windows_controls(
        self,
        screen_w: int,
        screen_h: int,
        max_items: int = 35,
    ) -> list[dict[str, Any]]:
        """Liest sichtbare Child-Controls des Vordergrundfensters über Win32 `EnumChildWindows` aus."""
        if not IS_WINDOWS:
            return []

        results: list[dict[str, Any]] = []
        try:
            user32 = ctypes.windll.user32
            fg_hwnd = user32.GetForegroundWindow()
            if not fg_hwnd:
                return []

            fg_rect = _RECT()
            if not user32.GetWindowRect(fg_hwnd, ctypes.byref(fg_rect)):
                return []

            win_cls_buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(fg_hwnd, win_cls_buf, 128)
            if win_cls_buf.value.strip().lower() in _EXCLUDED_WINDOW_CLASSES:
                return []

            enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            seen_centers: list[tuple[int, int]] = []

            def _callback(hwnd: int, _lparam: int) -> bool:
                if len(results) >= max_items:
                    return False
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    rect = _RECT()
                    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        return True
                    w = int(rect.right - rect.left)
                    h = int(rect.bottom - rect.top)
                    if w < 14 or h < 12 or w > int(screen_w * 0.85) or h > int(screen_h * 0.65):
                        return True

                    cx = int((rect.left + rect.right) // 2)
                    cy = int((rect.top + rect.bottom) // 2)
                    if cx < 2 or cy < 2 or cx >= screen_w - 2 or cy >= screen_h - 2:
                        return True

                    for sx, sy in seen_centers:
                        if abs(cx - sx) < 16 and abs(cy - sy) < 14:
                            return True

                    cls_buf = ctypes.create_unicode_buffer(128)
                    txt_buf = ctypes.create_unicode_buffer(128)
                    user32.GetClassNameW(hwnd, cls_buf, 128)
                    user32.GetWindowTextW(hwnd, txt_buf, 128)
                    cls_name = cls_buf.value.strip().lower()
                    raw_text = " ".join(txt_buf.value.split())[:42]

                    kind = ""
                    for key_pat, label_kind in _INTERACTIVE_CLASS_MAP.items():
                        if key_pat in cls_name:
                            kind = label_kind
                            break
                    if not kind:
                        if raw_text and w <= 420 and h <= 90:
                            kind = "Element"
                        else:
                            return True

                    seen_centers.append((cx, cy))
                    results.append(
                        {
                            "kind": kind,
                            "label": raw_text or kind,
                            "bbox": (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                            "x": cx,
                            "y": cy,
                            "norm_x": max(0, min(1000, int(round((cx / max(1, screen_w)) * 1000.0)))),
                            "norm_y": max(0, min(1000, int(round((cy / max(1, screen_h)) * 1000.0)))),
                            "source": "win32",
                        }
                    )
                except Exception:
                    return True
                return True

            cb = enum_proc_type(_callback)
            user32.EnumChildWindows(fg_hwnd, cb, 0)
        except Exception as exc:
            logger.debug("Win32 Control-Enum Hinweis: %s", exc)
        return results

    def _detect_opencv_ui_boxes(
        self,
        pil_img: Any,
        screen_w: int,
        screen_h: int,
        existing: list[dict[str, Any]],
        max_total: int = 42,
    ) -> list[dict[str, Any]]:
        """Schnelle OpenCV-Erkennung für benutzerdefinierte Buttons, Toolbar-Icons & Eingabefelder."""
        if not CV2_AVAILABLE or cv2 is None or np is None or pil_img is None:
            return []
        if len(existing) >= max_total:
            return []

        detected: list[dict[str, Any]] = []
        try:
            rgb = np.array(pil_img.convert("RGB"))
            img_h, img_w = rgb.shape[:2]
            scale_x = screen_w / max(1, img_w)
            scale_y = screen_h / max(1, img_h)

            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            edges = cv2.Canny(gray, 45, 135)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=1)
            contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            seen_centers = [(int(item["x"]), int(item["y"])) for item in existing]
            candidates: list[tuple[int, dict[str, Any]]] = []

            for cnt in contours:
                x, y, w, h = cv2.boundingRect(cnt)
                scr_w = int(round(w * scale_x))
                scr_h = int(round(h * scale_y))
                if scr_w < 20 or scr_h < 18 or scr_w > 340 or scr_h > 76:
                    continue
                area = scr_w * scr_h
                if area < 420 or area > 22000:
                    continue
                cx = int(round((x + w / 2.0) * scale_x))
                cy = int(round((y + h / 2.0) * scale_y))
                if cy < 12 or cy > screen_h - 10:
                    continue

                if any(abs(cx - sx) < 22 and abs(cy - sy) < 18 for sx, sy in seen_centers):
                    continue

                kind = "Eingabefeld" if (scr_w >= 115 and 20 <= scr_h <= 48) else "Button"
                left = int(round(x * scale_x))
                top = int(round(y * scale_y))
                right = int(round((x + w) * scale_x))
                bottom = int(round((y + h) * scale_y))
                seen_centers.append((cx, cy))
                candidates.append(
                    (
                        (cy // 32) * 10000 + cx,
                        {
                            "kind": kind,
                            "label": f"{kind}@{cx},{cy}",
                            "bbox": (left, top, right, bottom),
                            "x": cx,
                            "y": cy,
                            "norm_x": max(0, min(1000, int(round((cx / max(1, screen_w)) * 1000.0)))),
                            "norm_y": max(0, min(1000, int(round((cy / max(1, screen_h)) * 1000.0)))),
                            "source": "opencv",
                        },
                    )
                )

            candidates.sort(key=lambda pair: pair[0])
            need = max(0, max_total - len(existing))
            detected = [item for _, item in candidates[:need]]
        except Exception as exc:
            logger.debug("OpenCV SoM-Box-Erkennung Hinweis: %s", exc)
        return detected

    def extract_interactive_elements(
        self,
        screen_w: int,
        screen_h: int,
        pil_img: Any = None,
        max_tags: int = 40,
    ) -> list[dict[str, Any]]:
        """Kombiniert Win32-UI-Tree + schnelle OpenCV-Box-Erkennung und vergibt `[1]..[N]` IDs."""
        win_items = self._enumerate_windows_controls(screen_w, screen_h, max_items=min(28, max_tags))
        cv_items = self._detect_opencv_ui_boxes(pil_img, screen_w, screen_h, win_items, max_total=max_tags)
        merged = (win_items + cv_items)[:max_tags]
        merged.sort(key=lambda it: (int(it["y"]) // 28, int(it["x"])))

        tagged: list[dict[str, Any]] = []
        tag_map: dict[int, dict[str, Any]] = {}
        for idx, item in enumerate(merged, start=1):
            entry = {**item, "tag_id": idx}
            tagged.append(entry)
            tag_map[idx] = entry

        with self._lock:
            self._last_tags = tag_map
            self._last_updated_at = time.monotonic()
            self._last_screen_size = (int(screen_w), int(screen_h))
        return tagged

    def annotate_pil_with_som(
        self,
        pil_img: Any,
        screen_w: int,
        screen_h: int,
        max_tags: int = 40,
    ) -> tuple[Any, str, list[dict[str, Any]]]:
        """
        Erkennt interaktive Elemente und zeichnet gelbe `[N]` Nummernschilder direkt
        auf das PIL-Screenshot-Bild (im RAM).
        Gibt `(annotated_pil, prompt_summary, tags_list)` zurück.
        """
        tags = self.extract_interactive_elements(screen_w=screen_w, screen_h=screen_h, pil_img=pil_img, max_tags=max_tags)
        if not tags or pil_img is None or not PIL_AVAILABLE or Image is None or ImageDraw is None:
            return pil_img, "", tags

        try:
            base_rgba = pil_img.convert("RGBA") if pil_img.mode != "RGBA" else pil_img.copy()
            overlay = Image.new("RGBA", base_rgba.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            img_w, img_h = base_rgba.size
            sx = img_w / max(1, screen_w)
            sy = img_h / max(1, screen_h)

            try:
                font = ImageFont.load_default()
            except Exception:
                font = None

            badge_bg = (255, 224, 24, 240)
            badge_border = (20, 20, 20, 255)
            badge_fg = (12, 12, 12, 255)
            box_outline = (255, 224, 24, 155)

            summary_parts: list[str] = []
            for item in tags:
                tid = int(item["tag_id"])
                l, t, r, b = item["bbox"]
                il = max(0, min(img_w - 2, int(round(l * sx))))
                it = max(0, min(img_h - 2, int(round(t * sy))))
                ir = max(il + 2, min(img_w - 1, int(round(r * sx))))
                ib = max(it + 2, min(img_h - 1, int(round(b * sy))))

                draw.rectangle([il, it, ir, ib], outline=box_outline, width=1)

                tag_txt = f"[{tid}]"
                bw = 10 + len(tag_txt) * 6
                bh = 14
                bx = max(1, min(img_w - bw - 1, il))
                by = max(1, it - bh + 2) if it > bh + 2 else min(img_h - bh - 1, it + 1)
                draw.rectangle([bx, by, bx + bw, by + bh], fill=badge_bg, outline=badge_border, width=1)
                draw.text((bx + 3, by + 2), tag_txt, fill=badge_fg, font=font)

                short_lbl = str(item.get("label") or item.get("kind") or "Element")[:26]
                summary_parts.append(
                    f"[{tid}]: {item['kind']} '{short_lbl}' ({item['x']}, {item['y']} | N[{item['norm_x']},{item['norm_y']}])"
                )

            composited = Image.alpha_composite(base_rgba, overlay).convert("RGB")
            base_rgba.close()
            overlay.close()
            return composited, " | ".join(summary_parts[:28]), tags
        except Exception as exc:
            logger.debug("SoM Annotation Hinweis: %s", exc)
            return pil_img, "", tags

    def resolve_tag_to_coords(self, tag_id: int | str) -> Optional[tuple[int, int, str]]:
        """Löst eine `tag_id` (z. B. 2 oder '[2]') exakt in Bildschirm-Pixel `(x, y, label)` auf."""
        try:
            clean_id = int(str(tag_id).strip().strip("[]"))
        except Exception:
            return None
        with self._lock:
            entry = self._last_tags.get(clean_id)
            if not entry:
                return None
            return int(entry["x"]), int(entry["y"]), str(entry.get("label") or entry.get("kind") or f"Tag {clean_id}")

    def get_last_tags(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._last_tags.items(), key=lambda pair: pair[0])]


UI_SOM_TAGGER = UISetOfMarkTagger()
