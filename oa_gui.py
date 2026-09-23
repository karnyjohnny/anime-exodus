#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OA -> MAL / AniList Converter : GUI (DearPyGui 2.x)
===================================================

Nowoczesne, ciemne GUI dla klasy ``OaToMalConverter`` z pliku ``oa_converter.py``.

Wymagania (dokładnie te wersje, na których testowane):
    pip install dearpygui==2.3.1 requests rapidfuzz

Uruchomienie (w katalogu, w którym leży ``oa_converter.py``):
    python oa_gui.py

Okna aplikacji:
    * OKNO GŁÓWNE (pełny ekran / zmaksymalizowane) - pipeline:
        1. wybór pliku CSV z ogladajanime.pl (file browser) + walidacja + statystyki,
        2. start analizy w wątku roboczym, animowany progress + statystyki + dziennik,
        3. generowanie pliku XML (MAL format) do importu.
    * OKNO WERYFIKACJI - lista niepewnych dopasowań:
        [nazwa z ogladajanime.pl] + dropdown z propozycjami AniList + przycisk Zapisz,
        dodatkowo ręczne MAL ID oraz odrzucenie wpisu.
    * OKNO "INSTRUKCJA"      - co, jak i dlaczego działa ten pipeline.
    * OKNO "IMPORT MAL / ANILIST" - krok po kroku jak wgrać wygenerowany XML.

Uwagi techniczne (DearPyGui 2.x):
    * ``search_all()`` NIGDY nie jest wywoływane w wątku renderowania - leci w
      ``threading.Thread``, a postęp wraca przez ``queue.Queue`` i jest aplikowany
      w ticku ramki (``set_frame_callback`` samoprzezbrajalny), więc widgety
      aktualizuje wyłącznie wątek UI.
    * Anulowanie przez ``threading.Event``.
    * Wątek roboczy niczego nie rysuje - tylko wrzuca joby do kolejki UI.
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
import contextlib

try:
    import dearpygui.dearpygui as dpg
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Brak biblioteki dearpygui.\nZainstaluj:  pip install dearpygui"
    ) from exc

try:
    from oa_converter import OaToMalConverter
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Brak pliku oa_converter.py w tym samym katalogu.\n"
        "Skopiuj wcześniejszą klasę OaToMalConverter do pliku oa_converter.py."
    ) from exc

# ---------------------------------------------------------------- DPI (Cross-platform)
# Obsługa High DPI tylko dla Windowsa (na macOS/Linux DearPyGui robi to natywnie)
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDpiAware()

APP_NAME = "ANIME EXODUS"
APP_VERSION = "1.1"

# Tutaj możesz dostroić tempo analizy AniList, np.:
#   CONVERTER_KWARGS = {"request_delay": 1.5, "max_requests_per_minute": 40}
# Domyślnie: ciągłe, konserwatywne tempo ~27 zapytań/min z adaptacyjnym
# zwalnianiem po 429 (szczegóły w oknie „Instrukcja”).
CONVERTER_KWARGS: dict = {}

# --------------------------------------------------------------------------- #
#  PALETA (dark mode)                                                          #
# --------------------------------------------------------------------------- #
BG = (12, 14, 20, 255)  # tło viewportu / okien
PANEL = (17, 20, 28, 255)  # panele (child windows)
PANEL_HI = (23, 27, 37, 255)  # panele podświetlone / karty
FRAME = (26, 30, 41, 255)  # tło inputów / combo
FRAME_HOV = (33, 38, 52, 255)
FRAME_ACT = (41, 47, 64, 255)
BORDER = (37, 43, 58, 255)
TEXT = (226, 232, 240, 255)
MUTED = (126, 137, 156, 255)
ACCENT = (88, 158, 255, 255)  # niebieski akcent
ACCENT_HOV = (116, 178, 255, 255)
ACCENT_ACT = (62, 130, 235, 255)
VIOLET = (158, 122, 255, 255)  # drugi kolor animowanego progressa
GREEN = (62, 196, 128, 255)
GREEN_HOV = (92, 216, 150, 255)
AMBER = (233, 170, 80, 255)
RED = (239, 92, 99, 255)
RED_HOV = (255, 118, 124, 255)
CYAN = (80, 205, 226, 255)

URL_MAL = "https://myanimelist.net/import.php"
URL_ANILIST = "https://anilist.co/settings/import"


# --------------------------------------------------------------------------- #
#  Drobne helpery                                                              #
# --------------------------------------------------------------------------- #
def _lerp_color(c1, c2, t: float):
    t = min(1.0, max(0.0, t))
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1[:3], c2[:3])) + (255,)


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m:02d}:{s:02d}"


def _find_font(candidates) -> str | None:
    for path in candidates:
        p = Path(path)
        if p.exists() and p.is_file():
            return str(p)
    return None


REGULAR_FONTS = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/calibri.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]
BOLD_FONTS = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/seguisb.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


# --------------------------------------------------------------------------- #
#  Aplikacja                                                                   #
# --------------------------------------------------------------------------- #
class ConverterApp:
    def __init__(self) -> None:
        self.converter = OaToMalConverter(**CONVERTER_KWARGS)

        self.ui_queue: "queue.Queue" = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None

        self.searching = False
        self.csv_loaded = False
        self.search_done = False
        self.search_result = None
        self.load_result = None

        self._search_t0 = 0.0
        self._progress_cur = 0
        self._progress_tot = 0
        self._last_progress_ts = 0.0
        self._pulse = 0.0

        self._last_csv_dir: str | None = None
        self._compact: bool | None = None
        self._fonted: list = []
        self._force_relayout = 0
        self._last_size: tuple | None = None
        self.font_reg: str | None = None
        self.font_bold: str | None = None
        self.font_big: str | None = None

        # tagi widgetów
        self.w = {}
        self.review_rows: dict[int, dict] = {}
        self.review_remaining = 0

    # fixy
    def _get_viewport_size(self) -> tuple[int, int]:
        """Pobiera realny rozmiar viewportu z bezpiecznym fallbackiem (Win/Linux/macOS)."""
        cw = dpg.get_viewport_client_width() or dpg.get_viewport_width()
        ch = dpg.get_viewport_client_height() or dpg.get_viewport_height()

        # Domyślny, bezpieczny rozmiar dla małych ekranów jeśli OS jeszcze nie przekazał wymiarów
        if not cw or cw <= 0:
            cw = 1200
        if not ch or ch <= 0:
            ch = 700
        return cw, ch

    # ------------------------------------------------------------------ UI post
    def post(self, fn) -> None:
        """Bezpieczne wywołanie funkcji w wątku UI (z wątku roboczego)."""
        self.ui_queue.put(fn)

    def log(self, message: str, color=TEXT) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.post(lambda: self._log_line(f"[{stamp}] {message}", color))

    def _log_line(self, text: str, color) -> None:
        if not dpg.does_item_exist(self.w["log_child"]):
            return
        line = dpg.add_text(
            text,
            color=color,
            wrap=getattr(self, "log_wrap", 400),
            parent=self.w["log_child"],
        )
        dpg.set_y_scroll(self.w["log_child"], dpg.get_y_scroll_max(self.w["log_child"]))
        # tnij dziennik, żeby nie rósł w nieskończoność
        kids = dpg.get_item_children(self.w["log_child"], slot=1) or []
        if len(kids) > 400:
            for old in kids[: len(kids) - 400]:
                dpg.delete_item(old)
        del line

    # ------------------------------------------------------------------- MOTYW
    def _build_theme(self) -> None:
        with dpg.theme() as theme:
            with dpg.theme_component(0):
                dpg.add_theme_color(dpg.mvThemeCol_Text, TEXT)
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, MUTED)
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, BG)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, PANEL)
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (20, 23, 32, 250))
                dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER)
                dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, FRAME)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, FRAME_HOV)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, FRAME_ACT)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (16, 19, 27, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (24, 29, 41, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, (12, 14, 20, 255))
                dpg.add_theme_color(dpg.mvThemeCol_MenuBarBg, PANEL)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (14, 16, 23, 60))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (48, 55, 73, 255))
                dpg.add_theme_color(
                    dpg.mvThemeCol_ScrollbarGrabHovered, (62, 71, 93, 255)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_ScrollbarGrabActive, (78, 89, 116, 255)
                )
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, ACCENT_HOV)
                dpg.add_theme_color(dpg.mvThemeCol_Button, FRAME_HOV)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, FRAME_ACT)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (50, 57, 78, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Header, FRAME_HOV)
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, FRAME_ACT)
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (52, 60, 82, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Separator, BORDER)
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorHovered, (60, 68, 90, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorActive, (74, 84, 110, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ResizeGrip, (70, 80, 105, 120))
                dpg.add_theme_color(
                    dpg.mvThemeCol_ResizeGripHovered, (90, 102, 132, 160)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_ResizeGripActive, (110, 124, 160, 200)
                )
                dpg.add_theme_color(dpg.mvThemeCol_Tab, (22, 26, 36, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TabHovered, ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_TabSelected, (32, 38, 53, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TabSelectedOverline, ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_TabDimmed, (18, 21, 29, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TabDimmedSelected, (26, 31, 43, 255))
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, ACCENT)
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogramHovered, ACCENT_HOV)
                dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, PANEL_HI)
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, BORDER)
                dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (30, 35, 47, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, (255, 255, 255, 8))
                dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, (88, 158, 255, 70))
                dpg.add_theme_color(dpg.mvThemeCol_ModalWindowDimBg, (5, 6, 10, 190))

                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 14, 12)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_WindowBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_ChildBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_PopupBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 9, 6)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 7)
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 10, 8)
                dpg.add_theme_style(dpg.mvStyleVar_ItemInnerSpacing, 8, 6)
                dpg.add_theme_style(dpg.mvStyleVar_IndentSpacing, 18)
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 8, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 12)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, 8)
                dpg.add_theme_style(dpg.mvStyleVar_GrabMinSize, 10)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 6)
                dpg.add_theme_style(dpg.mvStyleVar_TabRounding, 7)
                dpg.add_theme_style(dpg.mvStyleVar_WindowTitleAlign, 0.0, 0.5)
                dpg.add_theme_style(dpg.mvStyleVar_ButtonTextAlign, 0.5, 0.5)
                dpg.add_theme_style(dpg.mvStyleVar_SeparatorTextAlign, 0.0, 0.5)
                dpg.add_theme_style(dpg.mvStyleVar_SeparatorTextPadding, 0, 4)
        dpg.bind_theme(theme)

        # --- motywy punktowe -------------------------------------------------
        def _btn_theme(base, hov, act, text_color=TEXT, rounding=7.0):
            with dpg.theme() as th:
                with dpg.theme_component(0):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, base)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hov)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, act)
                    dpg.add_theme_color(dpg.mvThemeCol_Text, text_color)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, rounding)
                    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 12, 7)
            return th

        self.w["th_accent"] = _btn_theme(
            ACCENT_ACT, ACCENT, ACCENT_HOV, (255, 255, 255, 255)
        )
        self.w["th_green"] = _btn_theme(
            (38, 148, 96, 255), GREEN, GREEN_HOV, (255, 255, 255, 255)
        )
        self.w["th_red"] = _btn_theme(
            (150, 52, 58, 255), RED, RED_HOV, (255, 255, 255, 255)
        )
        self.w["th_violet"] = _btn_theme(
            (104, 78, 178, 255), VIOLET, (178, 148, 255, 255), (255, 255, 255, 255)
        )
        self.w["th_quiet"] = _btn_theme(FRAME, FRAME_HOV, FRAME_ACT, TEXT)

        # panel / karta
        with dpg.theme() as th:
            with dpg.theme_component(0):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, PANEL_HI)
                dpg.add_theme_color(dpg.mvThemeCol_Border, (45, 52, 70, 255))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 9)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 10)
        self.w["th_card"] = th

        with dpg.theme() as th:
            with dpg.theme_component(0):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (0, 0, 0, 0))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 0, 0)
        self.w["th_flat"] = th

        # progress (kolor paska animowany w ticku)
        with dpg.theme() as th:
            with dpg.theme_component(0):
                self.w["prog_color"] = dpg.add_theme_color(
                    dpg.mvThemeCol_PlotHistogram, ACCENT
                )
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (22, 26, 36, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (45, 52, 70, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 8)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 4, 4)
        self.w["th_progress"] = th

    # ------------------------------------------------------------------ CZCIONKI
    def _build_fonts(self) -> None:
        reg = _find_font(REGULAR_FONTS)
        bold = _find_font(BOLD_FONTS)
        with dpg.font_registry():
            if reg:
                # zestaw normalny + kompaktowy (male ekrany / wysokie DPI)
                self.font_reg = dpg.add_font(reg, 17, tag="font_reg")
                self.font_big = dpg.add_font(bold or reg, 26, tag="font_big")
                self.font_bold = dpg.add_font(bold or reg, 17, tag="font_bold")
                self.font_small = dpg.add_font(reg, 14, tag="font_small")
                self.font_reg_c = dpg.add_font(reg, 14, tag="font_reg_c")
                self.font_big_c = dpg.add_font(bold or reg, 20, tag="font_big_c")
                self.font_bold_c = dpg.add_font(bold or reg, 14, tag="font_bold_c")
                self.font_small_c = dpg.add_font(reg, 12, tag="font_small_c")
            else:
                self.log(
                    "Nie znaleziono czcionki systemowej - używam wbudowanej "
                    "(polskie znaki mogą być niepełne).",
                    AMBER,
                )
        if self.font_reg:
            dpg.bind_font(self.font_reg)

    # -- fonty: rejestr ról, żeby móc przełączać gęstość UI w locie ----------
    def _bindf(self, item, role: str) -> None:
        self._fonted.append((item, role))
        font = self._font_for(role)
        if font:
            dpg.bind_item_font(item, font)

    def _font_for(self, role: str):
        if role == "big":
            return self.font_big_c if self._compact else self.font_big
        if role == "bold":
            return self.font_bold_c if self._compact else self.font_bold
        if role == "small":
            return self.font_small_c if self._compact else self.font_small
        return self.font_reg_c if self._compact else self.font_reg

    def _apply_density(self, cw: int, ch: int) -> None:
        # Przełącza UI między trybem normalnym a kompaktowym (małe ekrany).
        compact = bool(cw < 1400 or ch < 820)
        if compact == self._compact:
            return
        self._compact = compact
        if not self.font_reg:
            return
        dpg.bind_font(self._font_for("reg"))
        for item, role in self._fonted:
            if dpg.does_item_exist(item):
                dpg.bind_item_font(item, self._font_for(role))
        card_w, card_h = (148, 78) if compact else (168, 90)
        for tag in getattr(self, "_card_tags", []):
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, width=card_w, height=card_h)
        sizes = {
            "btn_pick_csv": 36 if compact else 44,
            "btn_reload_csv": 36 if compact else 44,
            "btn_start": 30 if compact else 34,
            "btn_cancel": 30 if compact else 34,
            "btn_review": 30 if compact else 34,
            "btn_xml": 30 if compact else 34,
        }
        for key, height in sizes.items():
            tag = self.w.get(key)
            if tag and dpg.does_item_exist(tag):
                dpg.configure_item(tag, height=height)
        if dpg.does_item_exist(self.w["progress"]):
            dpg.configure_item(self.w["progress"], height=24 if compact else 30)

    # ------------------------------------------------------------------- BUDOWA
    def _hook_rate_events(self) -> None:
        """GUI podłącza się pod hook rate-limitu klienta AniList (logi + tempo)."""
        client = getattr(self.converter, "client", None)
        if client is not None and hasattr(client, "on_rate_event"):
            client.on_rate_event = self._on_rate_event

    def _on_rate_event(self, kind: str, info: dict) -> None:
        """Wywoływane z wątku roboczego - tylko loguje przez kolejkę UI."""
        if kind == "backoff":
            self.log(
                f"AniList HTTP {info.get('status')}: zwalniam do "
                f"{info.get('delay_after')} s/zapytanie (~{info.get('pace')}/min), "
                f"pauza {info.get('pause')} s - jadę dalej",
                AMBER,
            )
        elif kind == "window_wait":
            self.log(
                f"Limit {info.get('max_rpm'):.0f} zapytań/min: płynnie czekam "
                f"{info.get('wait')} s i kontynuuję",
                MUTED,
            )
        elif kind == "speedup":
            self.log(
                f"Stabilnie: przyspieszam do {info.get('delay')} s/zapytanie "
                f"(~{info.get('pace')}/min)",
                CYAN,
            )
        elif kind == "network_retry":
            self.log(
                f"Błąd sieci (próba {info.get('attempt')}): czekam "
                f"{info.get('pause')} s i próbuję dalej",
                RED,
            )

    def _rate_info(self) -> dict:
        try:
            client = getattr(self.converter, "client", None)
            if client is not None and hasattr(client, "rate_info"):
                return client.rate_info() or {}
        except Exception:  # noqa: BLE001
            pass
        return {}

    def build(self) -> None:
        dpg.create_context()
        dpg.create_viewport(
            title=APP_NAME,
            width=1600,
            height=950,
            min_width=1180,
            min_height=720,
            clear_color=BG,
        )
        self._build_theme()
        self._build_fonts()

        self._build_main_window()
        self._build_review_window()
        self._build_help_window()
        self._build_import_window()
        self._build_modal()
        self._build_file_dialogs()

        self._hook_rate_events()
        dpg.set_primary_window(self.w["main"], True)
        dpg.set_viewport_resize_callback(lambda *a: self._on_resize(*a))

        # tick UI: opróżnia kolejkę jobów + animacje
        dpg.set_frame_callback(1, lambda *a: self._arm_tick())

    # ------------------------------------------------------------- OKNO GŁÓWNE
    def _build_main_window(self) -> None:
        with dpg.window(
            tag="win_main",
            label="Konwerter",
            no_title_bar=True,
            no_close=True,
            no_collapse=True,
        ):
            self.w["main"] = "win_main"

            # ---- pasek górny
            with dpg.group(horizontal=True, horizontal_spacing=14):
                title = dpg.add_text("OA → MAL", color=ACCENT)
                if self.font_big:
                    self._bindf(title, "big")
                sub = dpg.add_text(
                    "konwerter listy ogladajanime.pl → MyAnimeList / AniList",
                    color=MUTED,
                )
                dpg.add_spacer(width=-1)
                dpg.add_button(
                    label="Instrukcja  (co i dlaczego)",
                    width=230,
                    callback=lambda *a: self._show("win_help"),
                )
                dpg.add_button(
                    label="Import do MAL / AniList",
                    width=210,
                    callback=lambda *a: self._show("win_import"),
                )
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ---- korpus: lewa kolumna + dziennik
            with dpg.group(horizontal=True, horizontal_spacing=12):
                with dpg.child_window(
                    tag="main_left", width=-1, height=-1, border=False
                ):
                    self._section("1", "PLIK CSV Z OGLADAJANIME.PL")
                    with dpg.group(horizontal=True, horizontal_spacing=10):
                        self.w["btn_pick_csv"] = dpg.add_button(
                            label="Wybierz plik CSV…",
                            width=320,
                            height=44,
                            callback=lambda *a: self._open_csv_dialog(*a),
                        )
                        self.w["btn_reload_csv"] = dpg.add_button(
                            label="Wczytaj ponownie",
                            width=180,
                            height=44,
                            callback=lambda *a: self._on_load_csv(*a),
                            enabled=False,
                        )
                    dpg.bind_item_theme(self.w["btn_pick_csv"], self.w["th_accent"])
                    with dpg.tooltip(self.w["btn_pick_csv"]):
                        dpg.add_text(
                            "Otworzy się okno wyboru pliku (File & Directory\n"
                            "Selector). Wskaż plik CSV pobrany z\n"
                            "ogladajanime.pl - wczyta się automatycznie.",
                            color=TEXT,
                            wrap=-1,
                        )
                    self.w["csv_path_label"] = dpg.add_text(
                        "Nie wybrano jeszcze pliku.", color=MUTED, wrap=-1
                    )
                    with dpg.collapsing_header(
                        label="Zaawansowane: wpisz ścieżkę ręcznie", show=False
                    ):
                        with dpg.group(horizontal=True, horizontal_spacing=8):
                            self.w["csv_path"] = dpg.add_input_text(
                                hint="ścieżka do pliku CSV wyeksportowanego z ogladajanime.pl",
                                width=-1,
                                on_enter=True,
                                callback=lambda *a: self._on_csv_enter(*a),
                            )
                            dpg.add_button(
                                label="Wczytaj", width=110, callback=lambda *a: self._on_load_csv(*a)
                            )
                    self.w["csv_status"] = dpg.add_text(
                        "Nie wczytano jeszcze żadnego pliku.", color=MUTED, wrap=-1
                    )
                    with dpg.collapsing_header(
                        label="Błędy parsowania CSV", tag="csv_errors_hdr", show=False
                    ):
                        with dpg.child_window(
                            tag="csv_errors_child", height=140, border=False
                        ):
                            pass
                    dpg.add_spacer(height=6)
                    dpg.add_separator()
                    dpg.add_spacer(height=6)

                    self._section("2", "DOPASOWANIE TYTUŁÓW  (AniList → MAL ID)")
                    with dpg.group(horizontal=True, horizontal_spacing=8):
                        self.w["btn_start"] = dpg.add_button(
                            label="Rozpocznij analizę",
                            width=210,
                            height=34,
                            callback=lambda *a: self._on_start_search(*a),
                            enabled=False,
                        )
                        self.w["btn_cancel"] = dpg.add_button(
                            label="Anuluj",
                            width=110,
                            height=34,
                            callback=lambda *a: self._on_cancel(*a),
                            enabled=False,
                        )
                        self.w["spinner"] = dpg.add_loading_indicator(
                            style=0,
                            circle_count=8,
                            speed=1.5,
                            radius=5.0,
                            thickness=3.0,
                            color=ACCENT,
                            secondary_color=VIOLET,
                            show=False,
                        )
                        self.w["search_phase"] = dpg.add_text("", color=MUTED)
                    dpg.bind_item_theme(self.w["btn_start"], self.w["th_accent"])
                    dpg.bind_item_theme(self.w["btn_cancel"], self.w["th_red"])
                    dpg.add_spacer(height=6)

                    self.w["progress"] = dpg.add_progress_bar(
                        default_value=0.0, overlay="bezczynny", width=-1, height=30
                    )
                    dpg.bind_item_theme(self.w["progress"], self.w["th_progress"])
                    dpg.add_spacer(height=4)
                    with dpg.group(horizontal=True, horizontal_spacing=18):
                        self.w["prog_current"] = dpg.add_text("", color=MUTED, wrap=-1)
                        self.w["prog_eta"] = dpg.add_text("", color=MUTED)
                    self.w["prog_pace"] = dpg.add_text("", color=MUTED)
                    dpg.add_spacer(height=8)

                    self.w["cards_hint"] = dpg.add_text(
                        "Tu pojawią się statystyki analizy: dopasowane automatycznie, "
                        "do weryfikacji, niska pewność, brak sugestii, błędy.",
                        color=(94, 104, 122, 255),
                        wrap=-1,
                    )
                    self.w["cards_group"] = dpg.add_group(
                        horizontal=True, horizontal_spacing=10, show=False
                    )
                    self._build_stat_cards(self.w["cards_group"])
                    dpg.add_spacer(height=6)
                    dpg.add_separator()
                    dpg.add_spacer(height=6)

                    self._section("3", "PLIK WYNIKOWY  (XML w formacie MyAnimeList)")
                    with dpg.group(horizontal=True, horizontal_spacing=8):
                        self.w["btn_review"] = dpg.add_button(
                            label="Weryfikacja niepewnych (0)",
                            width=260,
                            height=34,
                            callback=lambda *a: self._open_review(*a),
                            enabled=False,
                        )
                        self.w["btn_xml"] = dpg.add_button(
                            label="Generuj XML",
                            width=170,
                            height=34,
                            callback=lambda *a: self._on_generate_click(*a),
                            enabled=False,
                        )
                        self.w["xml_path"] = dpg.add_input_text(
                            default_value=str(Path.cwd() / "mal_import.xml"), width=-1
                        )
                        dpg.add_button(
                            label="Zmień…",
                            width=90,
                            callback=lambda *a: self._show("dlg_save_xml"),
                        )
                    dpg.bind_item_theme(self.w["btn_review"], self.w["th_violet"])
                    dpg.bind_item_theme(self.w["btn_xml"], self.w["th_green"])
                    self.w["xml_status"] = dpg.add_text(
                        "XML będzie zawierał wyłącznie wpisy potwierdzone "
                        "(automatycznie lub ręcznie).",
                        color=MUTED,
                        wrap=-1,
                    )
                    self.w["confirmed_line"] = dpg.add_text("", color=MUTED)

                with dpg.child_window(
                    tag="log_child", width=430, height=-1, border=True
                ) as _lc:
                    self.w["log_child"] = _lc
                    hdr = dpg.add_text("Dziennik", color=ACCENT)
                    if self.font_bold:
                        self._bindf(hdr, "bold")
                    dpg.add_spacer(height=2)

    def _section(self, number: str, title: str) -> None:
        with dpg.group(horizontal=True, horizontal_spacing=10):
            chip = dpg.add_text(number, color=ACCENT)
            if self.font_big:
                self._bindf(chip, "big")
            t = dpg.add_text(title, color=TEXT)
            if self.font_bold:
                self._bindf(t, "bold")
        dpg.add_spacer(height=4)

    def _build_stat_cards(self, parent) -> None:
        self._card_tags = []
        cards = [
            ("card_auto", "dopasowane auto", GREEN),
            ("card_review", "do weryfikacji", AMBER),
            ("card_low", "niska pewność", VIOLET),
            ("card_none", "brak sugestii", RED),
            ("card_err", "błędy", RED),
        ]
        for tag, label, color in cards:
            with dpg.child_window(
                tag=tag,
                width=168,
                height=90,
                border=True,
                no_scrollbar=True,
                parent=parent,
            ):
                dpg.bind_item_theme(tag, self.w["th_card"])
                val = dpg.add_text("—", color=color)
                if self.font_big:
                    self._card_tags.append(tag)
                    self._bindf(val, "big")
                lbl = dpg.add_text(label, color=MUTED)
                if self.font_small:
                    self._bindf(lbl, "small")
            self.w[tag] = f"{tag}_val"
            # zapamiętaj tag textu wartości
            dpg.set_item_user_data(tag, val)
            self.w[tag] = val

    # ---------------------------------------------------------- OKNO WERYFIKACJI
    def _build_review_window(self) -> None:
        with dpg.window(
            tag="win_review",
            label="Weryfikacja niepewnych dopasowań",
            width=1150,
            height=780,
            show=False,
            no_collapse=True,
        ):
            with dpg.group(horizontal=True, horizontal_spacing=12):
                t = dpg.add_text("Niepewne dopasowania", color=ACCENT)
                if self.font_big:
                    self._bindf(t, "big")
                self.w["review_left"] = dpg.add_text("", color=MUTED)
            dpg.add_text(
                "Wybierz propozycję z listy i kliknij „Zapisz wybór”, "
                "wpisz MAL ID ręcznie albo odrzuć wpis.",
                color=MUTED,
                wrap=-1,
            )
            dpg.add_separator()
            with dpg.child_window(tag="review_list", height=-1, border=False):
                pass
            with dpg.group(horizontal=True, horizontal_spacing=10):
                dpg.add_button(
                    label="Generuj XML teraz",
                    width=190,
                    height=32,
                    callback=lambda *a: self._on_generate_click(*a),
                )
                dpg.add_button(
                    label="Zamknij",
                    width=110,
                    height=32,
                    callback=lambda *a: dpg.configure_item("win_review", show=False),
                )
                self.w["review_done_msg"] = dpg.add_text("", color=GREEN)

    # ------------------------------------------------------------- OKNO POMOCY
    def _build_help_window(self) -> None:
        with dpg.window(
            tag="win_help",
            label="Instrukcja  ·  co, jak i dlaczego",
            width=860,
            height=760,
            pos=(60, 60),
            no_collapse=True,
        ):
            self._help_h1("Co robi ten program?")
            self._help_p(
                "Konwertuje eksport CSV z ogladajanime.pl do minimalnego pliku XML "
                "w formacie MyAnimeList, który można zaimportować do MAL-a oraz do AniList."
            )
            self._help_p(
                "CSV z ogladajanime.pl  →  wczytanie wpisów  →  wyszukiwanie tytułów "
                "w AniList (GraphQL)  →  pobranie idMal  →  scoring dopasowania  →  "
                "podział na pewne / niepewne  →  ręczna weryfikacja niepewnych  →  XML."
            )
            self._help_h2("Dlaczego akurat AniList?")
            self._help_p(
                "AniList pełni rolę wyłącznie lookupu „tytuł → MAL ID”. Serwis ma bardzo "
                "dobrą wyszukiwarkę tytułów (romaji / english / native) i zwraca pole idMal, "
                "czyli identyfikator z MyAnimeList. To właśnie idMal trafia do XML-a jako "
                "<series_animedb_id>. Prawdziwe dane Twojej listy (postęp, ocena, status) "
                "biorą się z CSV-a, nie z AniList."
            )
            self._help_h2("Progi pewności")
            self._help_p(
                "•  confidence ≥ 92  i sugestia ma MAL ID  →  wpis potwierdzany automatycznie,\n"
                "•  75 ≤ confidence < 92  →  trafia do okna weryfikacji,\n"
                "•  confidence < 75  →  też do weryfikacji, ale liczone jako „niska pewność”,\n"
                "•  brak kandydatów / błąd sieci  →  wpis zostaje do obsłużenia ręcznie."
            )
            self._help_p(
                "Progi można zmienić w kodzie: OaToMalConverter(auto_threshold=…, "
                "review_threshold=…)."
            )
            self._help_h2("Cache zapytań")
            self._help_p(
                "Wyniki wyszukiwania AniList zapisują się w pliku anilist_search_cache.json "
                "obok skryptu. Dzięki temu ponowne uruchomienie dla tych samych tytułów jest "
                "prawie natychmiastowe, a przerwanie pracy nie kasuje postępu. Nie usuwaj tego "
                "pliku, jeśli nie chcesz wymuszać pełnego wyszukiwania od zera."
            )
            self._help_h2("Tempo zapytań i 429 (ciągła analiza)")
            self._help_p(
                "Klient AniList pracuje w trybie ciągłym, bez przestojów: startowe tempo to "
                "~27 zapytań/min (2.2 s przerwy) plus twarde, suwakowe okno 32 zapytań na 60 s. "
                "Jeśli AniList odpowie 429 (Too Many Requests), klient respektuje Retry-After "
                "i na stałe zwalnia (opóźnienie *1.5, sufit 8 s), a po 20 udanych zapytaniach "
                "minimalnie przyspiesza (podłoga 2.0 s). Dzięki temu analiza nigdy nie staje "
                "w miejscu - jedzie wolniej, ale bez przerwy. Aktualne tempo, licznik 429 i "
                "trafienia w cache widać pod paskiem postępu, a każde zdarzenie limitu "
                "trafia do dziennika. Tempo zmienisz stałymi CONVERTER_KWARGS w oa_gui.py."
            )
            self._help_h2("Wątki i anulowanie")
            self._help_p(
                "Wyszukiwanie działa w osobnym wątku, więc interfejs nie zamiera. Postęp "
                "wraca do UI przez kolejkę zadań i jest rysowany wyłącznie w wątku UI. "
                "Przycisk „Anuluj” ustawia threading.Event - przetwarzanie zatrzymuje się "
                "bezpiecznie po zakończeniu bieżącego wpisu."
            )
            self._help_h2("Duplikaty tytułów")
            self._help_p(
                "Jeśli dwa wpisy CSV mają identyczny tytuł, słownik niepewnych kluczowany "
                "tytułem mógłby je mylić. Dlatego GUI pracuje na strukturze "
                "uncertain_by_entry_id (klucz = numer wpisu z kolumny „#”)."
            )
            self._help_h2("Sugestia bez MAL ID")
            self._help_p(
                "Czasem AniList znajdzie tytuł, ale nie zwróci idMal. Taka sugestia nie może "
                "trafić do XML-a - wtedy albo wpiszesz MAL ID ręcznie (pole obok przycisku "
                "„Zapisz ręcznie”), albo odrzucisz wpis."
            )
            self._help_h2("Ważne ostrzeżenia")
            self._help_p(
                "•  Ponowne kliknięcie „Rozpocznij analizę” CZYŚCI ręczne potwierdzenia.\n"
                "•  Do XML-a trafiają wyłącznie wpisy potwierdzone (auto lub ręcznie).\n"
                "•  Oceny z CSV są zaokrąglane do liczb całkowitych 1-10; „-” i puste = brak oceny.\n"
                "•  Statusy mapowane są: Oglądam→Watching, Obejrzane→Completed, "
                "Planuje→Plan to Watch, Wstrzymane→On-Hold, Porzucone→Dropped, "
                "Nie oglądam→On-Hold."
            )
            self._help_h2("Skrót obsługi")
            self._help_p(
                "1.  Przeglądaj… → wskaż CSV → Wczytaj (sprawdź statystyki i błędy).\n"
                "2.  Rozpocznij analizę → poczekaj na progress (możesz anulować).\n"
                "3.  Otwórz „Weryfikację niepewnych” i pozamiataj resztę.\n"
                "4.  Generuj XML → zaimportuj plik wg okna „Import do MAL / AniList”."
            )
            dpg.add_spacer(height=6)
            dpg.add_button(
                label="Zamknij",
                width=110,
                callback=lambda *a: dpg.configure_item("win_help", show=False),
            )

    def _help_h1(self, text: str) -> None:
        t = dpg.add_text(text, color=ACCENT)
        if self.font_big:
            self._bindf(t, "big")
        dpg.add_spacer(height=2)

    def _help_h2(self, text: str) -> None:
        dpg.add_spacer(height=6)
        t = dpg.add_text(text, color=CYAN)
        if self.font_bold:
            self._bindf(t, "bold")
        dpg.add_spacer(height=2)

    def _help_p(self, text: str) -> None:
        dpg.add_text(text, color=TEXT, wrap=800)
        dpg.add_spacer(height=2)

    # --------------------------------------------------------- OKNO IMPORTU
    def _build_import_window(self) -> None:
        with dpg.window(
            tag="win_import",
            label="Jak zaimportować wygenerowany XML",
            width=820,
            height=760,
            pos=(950, 90),
            no_collapse=True,
        ):
            self._help_h1("MyAnimeList  (MAL)")
            self._help_p("1.  Zaloguj się na myanimelist.net.")
            self._help_p("2.  Wejdź na stronę importu:")
            self._url_row(URL_MAL)
            self._help_p(
                "3.  W sekcji importu z MAL kliknij „Choose File” / „Browse” i wskaż swój "
                "plik mal_import.xml, następnie zatwierdź przyciskiem importu."
            )
            self._help_p(
                "4.  Poczekaj na przetworzenie - pozycje z pliku pojawią się na Twojej liście "
                "anime (wpisy już istniejące zostaną zaktualizowane, bo XML ma "
                "update_on_import=1)."
            )
            self._help_p(
                "Uwaga: MAL przyjmuje dokładnie taki format XML, jaki generuje ten program "
                "(to format oficjalnego eksportu MAL). Niczego nie przerabiaj ani nie pakuj."
            )
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=8)

            self._help_h1("AniList")
            self._help_p("1.  Zaloguj się na anilist.co.")
            self._help_p("2.  Wejdź do ustawień importu:")
            self._url_row(URL_ANILIST)
            self._help_p(
                "3.  Znajdź sekcję „MyAnimeList: Import Anime List (Make sure to unzip the "
                ".xml.gz file first!)”."
            )
            self._help_p(
                "4.  Zaznacz opcję „Overwrite anime already on my list”, jeśli chcesz nadpisać "
                "wpisy, które już masz na liście."
            )
            self._help_p("5.  Wskaż plik mal_import.xml i zatwierdź import.")
            self._help_p(
                "Uwaga o .xml.gz: komunikat o rozpakowyaniu dotyczy eksportów pobranych "
                "bezpośrednio z MAL-a, które przychodzą spakowane gzipem. Plik wygenerowany "
                "przez ten konwerter jest zwykłym, czystym XML-em - nie trzeba go rozpakowywać."
            )
            dpg.add_spacer(height=8)
            dpg.add_separator()
            dpg.add_spacer(height=8)
            self._help_h2("Kolejność sugerowana")
            self._help_p(
                "Najpierw zaimportuj listę do MAL-a (źródło prawdy o ID), potem do AniList. "
                "Jeśli nie masz konta MAL i celujesz tylko w AniList - samo okno AniList "
                "wystarczy, import przyjmie XML w formacie MAL."
            )
            dpg.add_spacer(height=6)
            dpg.add_button(
                label="Zamknij",
                width=110,
                callback=lambda *a: dpg.configure_item("win_import", show=False),
            )

    def _url_row(self, url: str) -> None:
        with dpg.group(horizontal=True, horizontal_spacing=8):
            u = dpg.add_text(url, color=ACCENT)
            dpg.add_button(
                label="Kopiuj",
                width=90,
                small=True,
                callback=lambda *a: self._copy_url(url),
            )
            dpg.add_button(
                label="Otwórz w przeglądarce",
                width=190,
                small=True,
                callback=lambda *a: self._open_url(url),
            )
        dpg.set_item_user_data(u, url)
        dpg.add_spacer(height=2)

    # ------------------------------------------------------------------ MODAL
    def _build_modal(self) -> None:
        with dpg.window(
            tag="win_modal",
            modal=True,
            show=False,
            autosize=True,
            no_resize=True,
            no_move=False,
            no_collapse=True,
            label="Komunikat",
            pos=(500, 300),
        ):
            self.w["modal_text"] = dpg.add_text("", wrap=620)
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True, horizontal_spacing=8):
                dpg.add_button(
                    label="OK",
                    width=110,
                    height=30,
                    callback=lambda *a: dpg.configure_item("win_modal", show=False),
                )
                self.w["modal_btn2"] = dpg.add_button(
                    label="", width=210, height=30, show=False, callback=None
                )

    # ---------------------------------------------------------- FILE DIALOGS
    def _build_file_dialogs(self) -> None:
        with dpg.file_dialog(
            tag="dlg_open_csv",
            show=False,
            modal=True,
            width=860,
            height=640,
            callback=lambda *a: self._on_csv_picked(*a),
            cancel_callback=lambda *a: None,
        ):
            dpg.add_file_extension(".csv", color=GREEN)
            dpg.add_file_extension(".txt", color=MUTED)
            dpg.add_file_extension(".*", color=MUTED)
        with dpg.file_dialog(
            tag="dlg_save_xml",
            show=False,
            modal=True,
            width=860,
            height=640,
            callback=lambda *a: self._on_xml_path_picked(*a),
            default_filename="mal_import.xml",
        ):
            dpg.add_file_extension(".xml", color=GREEN)
            dpg.add_file_extension(".*", color=MUTED)

    def _show(self, tag: str) -> None:
        dpg.configure_item(tag, show=True)

    def _copy_url(self, url: str) -> None:
        try:
            dpg.set_clipboard_text(url)
            self.log(f"Skopiowano do schowka: {url}", CYAN)
        except Exception:  # noqa: BLE001
            pass

    def _open_url(self, url: str) -> None:
        try:
            webbrowser.open(url)
            self.log(f"Otwieram przeglądarkę: {url}", CYAN)
        except Exception as exc:  # noqa: BLE001
            self._modal(f"Nie udało się otworzyć przeglądarki:\n{exc}")

    def _modal(
        self, text: str, color=TEXT, second_button: str | None = None, second_cb=None
    ) -> None:
        def _do():
            dpg.configure_item(self.w["modal_text"], default_value=text, color=color)
            if second_button:
                dpg.configure_item(
                    self.w["modal_btn2"],
                    label=second_button,
                    show=True,
                    callback=second_cb,
                )
            else:
                dpg.configure_item(self.w["modal_btn2"], show=False)
            dpg.configure_item("win_modal", show=True)

        self.post(_do)

    # ------------------------------------------------------------------ TICK UI
    def _arm_tick(self) -> None:
        self._tick()

    def _tick(self, *args) -> None:
        try:
            # 1) joby z kolejki (wątek roboczy -> UI)
            deadline = time.perf_counter() + 0.02
            while True:
                try:
                    fn = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
                if time.perf_counter() > deadline:
                    break
            # 2) auto-heal layoutu: system potrafi nadać oknu realne wymiary
            #    z opóźnieniem (maximize, DPI), więc co klatkę sprawdzamy
            #    rozmiar i przeliczamy układ TYLKO przy zmianie (+ wymuszone
            #    pierwsze klatki po starcie).
            try:
                cw, ch = self._get_viewport_size()
                if (cw, ch) != self._last_size or self._force_relayout > 0:
                    if self._force_relayout > 0:
                        self._force_relayout -= 1
                    self._last_size = (cw, ch)
                    self._apply_density(cw, ch)
                    self.layout_all()
                    if dpg.get_item_configuration("win_review").get("show"):
                        self._place_review()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

            # 3) animacja progressa (pulsujący gradient niebieski -> fiolet)
            if self.searching:
                self._pulse += 0.045
                t = (self._pulse % 2.0) / 2.0
                tri = 1.0 - abs(1.0 - 2.0 * t)  # trójkąt 0..1..0
                color = _lerp_color(ACCENT, VIOLET, tri)
                dpg.configure_item(self.w["prog_color"], value=list(color))
                dots = "." * (int(self._pulse * 2) % 3 + 1)
                if dpg.does_item_exist(self.w["search_phase"]):
                    base = (dpg.get_value(self.w["search_phase"]) or "").rstrip(".")
                    if base:
                        dpg.set_value(self.w["search_phase"], base + dots)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            dpg.set_frame_callback(dpg.get_frame_count() + 1, self._tick)

    # ---------------------------------------------------------------- CSV: LOAD
    def _default_dir(self) -> str:
        """Katalog startowy dla osób, które rzadko używają przeglądarek plików."""
        if self._last_csv_dir and Path(self._last_csv_dir).is_dir():
            return self._last_csv_dir
        home = Path.home()
        for cand in (
            home / "Desktop",
            home / "Pulpit",
            home / "Downloads",
            home / "Pobrane",
            home,
            Path.cwd(),
        ):
            if cand.is_dir():
                return str(cand)
        return str(Path.cwd())

    def _open_csv_dialog(self, sender=None, app_data=None, user_data=None) -> None:
        dpg.configure_item("dlg_open_csv", default_path=self._default_dir())
        self._show("dlg_open_csv")

    def _on_csv_enter(self, sender=None, app_data=None, user_data=None) -> None:
        self._on_load_csv()

    def _dialog_app_data(self, tag: str, args) -> dict:
        # Najpierw app_data z callbacku (jesli DPG raczył je przekazać)...
        for cand in args:
            if isinstance(cand, dict):
                return cand
        # ...a jeśli nie (build Nuitka bywa oszczędny), czytamy wybór
        # bezpośrednio z okna dialogu - działa zawsze.
        try:
            info = dpg.get_file_dialog_info(tag)
            if isinstance(info, dict):
                return info
        except Exception:  # noqa: BLE001
            pass
        return {}

    def _on_csv_picked(self, *args) -> None:
        app_data = self._dialog_app_data("dlg_open_csv", args)
        path = app_data.get("file_path_name") if isinstance(app_data, dict) else None
        if not path:
            selections = (app_data or {}).get("selections") or {}
            path = next(iter(selections.values()), None)
        if not path:
            return
        dpg.set_value(self.w["csv_path"], str(path))
        self._last_csv_dir = str(Path(path).parent)
        self._on_load_csv()

    def _on_load_csv(self, sender=None, app_data=None, user_data=None) -> None:
        raw = dpg.get_value(self.w["csv_path"]).strip().strip('"')
        if not raw:
            self._set_csv_status("Najpierw wskaż plik CSV (Przeglądaj…).", AMBER)
            return
        path = Path(raw)
        if not path.exists():
            dpg.configure_item(
                self.w["csv_path_label"],
                default_value=f"Wybrany plik:  {path}  (NIE ISTNIEJE)",
                color=RED,
            )
            self._set_csv_status(f"Plik nie istnieje: {path}", RED)
            self._modal(f"Nie znaleziono pliku:\n{path}", RED)
            return
        if path.suffix.lower() not in (".csv", ".txt"):
            self._set_csv_status(
                f"To nie wygląda na CSV (rozszerzenie: {path.suffix or 'brak'}). "
                "Wybierz eksport z ogladajanime.pl.",
                RED,
            )
            self._modal(
                "Wybrany plik nie ma rozszerzenia .csv.\n"
                "Wskaż plik wyeksportowany z ogladajanime.pl.",
                RED,
            )
            return
        try:
            loaded = self.converter.load_csv(path)
        except Exception as exc:  # noqa: BLE001
            self.csv_loaded = False
            dpg.configure_item(self.w["btn_start"], enabled=False)
            self._set_csv_status(f"Błąd wczytywania: {exc}", RED)
            self._modal(
                f"Nie udało się wczytać pliku:\n{exc}\n\n"
                "Upewnij się, że to eksport CSV z ogladajanime.pl "
                "(kolumny: #, Okładka, Tytuł, Status, Ocena, Postęp, Typ).",
                RED,
            )
            self.log(f"CSV: BŁĄD wczytania {path.name}: {exc}", RED)
            return

        self.load_result = loaded
        self.csv_loaded = loaded.count > 0
        self.search_done = False
        self._last_csv_dir = str(path.parent)
        if dpg.does_item_exist(self.w.get("csv_path_label", "")):
            dpg.configure_item(
                self.w["csv_path_label"],
                default_value=f"Wybrany plik:  {path}",
                color=TEXT,
            )
        dpg.configure_item(self.w["btn_reload_csv"], enabled=True)
        dpg.configure_item(self.w["btn_start"], enabled=self.csv_loaded)
        dpg.configure_item(self.w["btn_xml"], enabled=False)
        dpg.configure_item(self.w["btn_review"], enabled=False)

        if loaded.count == 0:
            self._set_csv_status(
                "Plik wczytany, ale nie zawiera żadnego wpisu anime. "
                "Sprawdź, czy to na pewno eksport z ogladajanime.pl.",
                RED,
            )
            self._modal("Plik nie zawiera żadnych wpisów anime.", RED)
            self.log(f"CSV: {path.name} -> 0 wpisów", RED)
            return

        msg = f"Wczytano {loaded.count} wpisów z {path.name}." + (
            f"  Błędy parsowania: {len(loaded.errors)}." if loaded.errors else ""
        )
        self._set_csv_status(msg, GREEN if not loaded.errors else AMBER)
        self.log(
            f"CSV: {path.name} -> {loaded.count} wpisów"
            + (f", {len(loaded.errors)} błędów" if loaded.errors else ""),
            GREEN,
        )

        # lista błędów parsowania
        has_errors = bool(loaded.errors)
        dpg.configure_item(
            "csv_errors_hdr",
            show=has_errors,
            label=f"Błędy parsowania CSV ({len(loaded.errors)})",
        )
        if has_errors:
            dpg.delete_item("csv_errors_child", children_only=True)
            for err in loaded.errors[:200]:
                dpg.add_text(err, color=RED, wrap=-1, parent="csv_errors_child")

        # reset progressu
        dpg.configure_item(
            self.w["progress"], default_value=0.0, overlay="wczytano CSV"
        )
        dpg.configure_item(self.w["prog_color"], value=list(ACCENT))
        dpg.set_value(self.w["prog_current"], "")
        dpg.set_value(self.w["prog_eta"], "")
        dpg.set_value(self.w["prog_pace"], "")
        dpg.configure_item(self.w["cards_group"], show=False)
        dpg.configure_item(self.w["cards_hint"], show=True)

    def _set_csv_status(self, text: str, color) -> None:
        dpg.configure_item(self.w["csv_status"], default_value=text, color=color)

    # ------------------------------------------------------------- SEARCH RUN
    def _on_start_search(self, sender=None, app_data=None, user_data=None) -> None:
        if self.searching:
            return
        if not self.csv_loaded:
            self._modal("Najpierw wczytaj plik CSV.", AMBER)
            return
        manual = sum(
            1 for m in self.converter.confirmed_matches if m.source == "manual"
        )
        if manual:
            self._modal(
                f"Uwaga: ponowne wyszukiwanie WYCZYŚCI {manual} ręcznych potwierdzeń.\n"
                "Jeśli chcesz tylko dokończyć weryfikację - zamknij to okno.\n\n"
                "Uruchomić analizę ponownie?",
                AMBER,
                second_button="Tak, uruchom ponownie",
                second_cb=lambda *a: (
                    dpg.configure_item("win_modal", show=False),
                    self._start_search_now(),
                ),
            )
            return
        self._start_search_now()

    def _start_search_now(self, sender=None, app_data=None, user_data=None) -> None:
        self.searching = True
        self.search_done = False
        self.cancel_event.clear()
        self._search_t0 = time.perf_counter()
        self._progress_cur = 0
        self._progress_tot = self.load_result.count if self.load_result else 0
        self._last_progress_ts = self._search_t0

        dpg.configure_item(self.w["btn_start"], enabled=False)
        dpg.configure_item(self.w["btn_cancel"], enabled=True)
        dpg.configure_item(self.w["btn_xml"], enabled=False)
        dpg.configure_item(self.w["btn_review"], enabled=False)
        dpg.configure_item(self.w["spinner"], show=True)
        dpg.set_value(self.w["search_phase"], "szukam w AniList")
        dpg.configure_item(self.w["progress"], default_value=0.0, overlay="0%")
        dpg.configure_item(self.w["prog_color"], value=list(ACCENT))
        dpg.configure_item(self.w["cards_group"], show=False)
        dpg.configure_item(self.w["cards_hint"], show=True)
        self.log("Analiza: start wyszukiwania w AniList…", CYAN)

        self.worker = threading.Thread(target=self._worker_run, daemon=True)
        self.worker.start()

    def _worker_run(self) -> None:
        try:
            result = self.converter.search_all(
                progress_callback=lambda *a: self._on_progress(*a),
                cancel_event=self.cancel_event,
            )
            self.post(lambda: self._on_search_done(result))
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            self.post(lambda: self._on_search_error(exc, tb))

    def _on_progress(self, current: int, total: int, entry) -> None:
        title = getattr(entry, "title", "?")
        self.post(lambda: self._update_progress(current, total, title))

    def _update_progress(self, current: int, total: int, title: str) -> None:
        self._progress_cur = current
        self._progress_tot = total or 1
        frac = current / self._progress_tot if self._progress_tot else 0.0
        now = time.perf_counter()
        self._last_progress_ts = now
        dpg.configure_item(
            self.w["progress"],
            default_value=frac,
            overlay=f"{frac * 100:.0f}%   {current}/{self._progress_tot}",
        )
        dpg.set_value(self.w["prog_current"], f"bieżący tytuł: {title}")
        elapsed = now - self._search_t0
        if current > 0:
            eta = elapsed / current * (self._progress_tot - current)
            dpg.set_value(
                self.w["prog_eta"],
                f"czas: {_fmt_time(elapsed)}   pozostało: ~{_fmt_time(eta)}",
            )
        else:
            dpg.set_value(self.w["prog_eta"], f"czas: {_fmt_time(elapsed)}")
        info = self._rate_info()
        if info:
            dpg.set_value(
                self.w["prog_pace"],
                f"tempo AniList: ~{info.get('pace_per_min')} zapytań/min"
                f"   •   opóźnienie {info.get('current_delay')} s"
                f"   •   zapytań {info.get('requests')}"
                f"   •   429: {info.get('http_429')}"
                f"   •   cache: {info.get('cache_hits')}",
            )

    def _on_cancel(self, sender=None, app_data=None, user_data=None) -> None:
        if not self.searching:
            return
        self.cancel_event.set()
        dpg.configure_item(self.w["btn_cancel"], enabled=False)
        dpg.set_value(self.w["search_phase"], "anulowanie")
        self.log("Analiza: anulowano - zatrzymam się po bieżącym wpisie.", AMBER)

    def _on_search_error(self, exc: Exception, tb: str) -> None:
        self.searching = False
        dpg.configure_item(self.w["spinner"], show=False)
        dpg.configure_item(self.w["btn_start"], enabled=self.csv_loaded)
        dpg.configure_item(self.w["btn_cancel"], enabled=False)
        dpg.configure_item(self.w["progress"], overlay="BŁĄD")
        dpg.configure_item(self.w["prog_color"], value=list(RED))
        self.log(f"Analiza: WYJĄTEK {exc}", RED)
        self._modal(
            f"Wyszukiwanie przerwało się błędem:\n{exc}\n\n"
            "Szczegóły w konsoli. Cache został zachowany - można spróbować ponownie.",
            RED,
        )
        del tb

    def _on_search_done(self, result) -> None:
        self.searching = False
        self.search_result = result
        self.search_done = True
        cancelled = self.cancel_event.is_set()

        dpg.configure_item(self.w["spinner"], show=False)
        dpg.configure_item(self.w["btn_cancel"], enabled=False)
        dpg.configure_item(
            self.w["btn_start"], enabled=self.csv_loaded and not cancelled
        )
        dpg.set_value(
            self.w["search_phase"], "anulowano" if cancelled else "zakończono"
        )

        stats = result.stats
        dpg.configure_item(
            self.w["progress"],
            default_value=1.0
            if not cancelled
            else (self._progress_cur / max(1, self._progress_tot)),
            overlay="anulowano" if cancelled else "100%  •  gotowe",
        )
        dpg.configure_item(
            self.w["prog_color"], value=list(AMBER if cancelled else GREEN)
        )
        dpg.set_value(
            self.w["prog_current"],
            f"przetworzono {self._progress_cur}/{self._progress_tot} wpisów"
            + ("  (przerwane)" if cancelled else ""),
        )
        dpg.set_value(
            self.w["prog_eta"],
            f"czas: {_fmt_time(time.perf_counter() - self._search_t0)}",
        )
        info = self._rate_info()
        if info:
            dpg.set_value(
                self.w["prog_pace"],
                f"tempo AniList: ~{info.get('pace_per_min')} zapytań/min"
                f"   •   zapytań {info.get('requests')}"
                f"   •   429: {info.get('http_429')}"
                f"   •   z cache: {info.get('cache_hits')}"
                f"   •   wygładzeń okna: {info.get('window_waits')}",
            )

        # karty statystyk
        dpg.configure_item(self.w["cards_hint"], show=False)
        dpg.configure_item(self.w["cards_group"], show=True)
        values = {
            "card_auto": stats.confirmed_auto,
            "card_review": stats.uncertain,
            "card_low": stats.low_confidence,
            "card_none": stats.no_candidates,
            "card_err": stats.errors,
        }
        for tag, val in values.items():
            dpg.set_value(self.w[tag], str(val))

        self.review_remaining = len(self.converter.uncertain_entries)
        dpg.configure_item(
            self.w["btn_review"],
            enabled=self.review_remaining > 0,
            label=f"Weryfikacja niepewnych ({self.review_remaining})",
        )
        dpg.configure_item(
            self.w["btn_xml"], enabled=len(self.converter.confirmed_matches) > 0
        )
        self._refresh_confirmed_line()

        self.log(
            f"Analiza: gotowe. auto={stats.confirmed_auto}, "
            f"weryfikacja={stats.uncertain}, niska={stats.low_confidence}, "
            f"brak sugestii={stats.no_candidates}, błędy={stats.errors}",
            GREEN,
        )
        if result.errors:
            for err in result.errors[:20]:
                self.log(f"   ! {err}", AMBER)

        if self.review_remaining > 0:
            self._open_review()
            self._modal(
                f"Dopasowano automatycznie: {stats.confirmed_auto}\n"
                f"Do ręcznej weryfikacji: {self.review_remaining}\n\n"
                "Otworzyłem okno weryfikacji - przejrzysz tam niepewne tytuły.",
                AMBER,
            )
        else:
            self._modal(
                f"Gotowe! Automatycznie dopasowano {stats.confirmed_auto} wpisów.\n"
                "Możesz generować XML.",
                GREEN,
            )

    def _refresh_confirmed_line(self) -> None:
        manual = sum(
            1 for m in self.converter.confirmed_matches if m.source == "manual"
        )
        dpg.set_value(
            self.w["confirmed_line"],
            f"Wpisy potwierdzone (trafią do XML): "
            f"{len(self.converter.confirmed_matches)}"
            + (f"  (w tym ręcznie: {manual})" if manual else ""),
        )

    # ------------------------------------------------------------------ REVIEW
    def _open_review(self, sender=None, app_data=None, user_data=None) -> None:
        if not self.review_rows:
            self._build_review_rows()
        try:
            vw = dpg.get_viewport_client_width() or 1600
            vh = dpg.get_viewport_client_height() or 900
        except Exception:  # noqa: BLE001
            vw, vh = 1600, 900
        self._place_review()
        self._show("win_review")

    def _place_review(self) -> None:
        try:
            vw = dpg.get_viewport_client_width() or 1600
            vh = dpg.get_viewport_client_height() or 900
        except Exception:  # noqa: BLE001
            vw, vh = 1600, 900
        w = min(1150, max(760, vw - 80))
        h = min(780, max(480, vh - 90))
        self.review_wrap = w - 150
        dpg.configure_item(
            "win_review",
            width=w,
            height=h,
            pos=(max(10, (vw - w) // 2), max(10, (vh - h) // 2)),
        )
        dpg.configure_item("review_list", height=max(200, h - 215))

    def _build_review_rows(self) -> None:
        dpg.delete_item("review_list", children_only=True)
        self.review_rows.clear()
        entries = list(self.converter.uncertain_entries)
        self.review_remaining = len(entries)
        self._update_review_counter()
        for entry in entries:
            suggs = self.converter.uncertain_by_entry_id.get(entry.local_id, [])
            self._add_review_row(entry, suggs)

    def _update_review_counter(self) -> None:
        dpg.set_value(self.w["review_left"], f"pozostało: {self.review_remaining}")
        dpg.configure_item(
            self.w["btn_review"],
            label=f"Weryfikacja niepewnych ({self.review_remaining})",
        )
        if self.review_remaining == 0:
            dpg.set_value(
                self.w["review_done_msg"],
                "Wszystko zweryfikowane - można generować XML.",
            )
        else:
            dpg.set_value(self.w["review_done_msg"], "")

    def _sugg_label(self, s) -> str:
        mal = f"MAL {s.mal_id}" if s.mal_id else "BRAK MAL ID"
        fmt = s.format or "?"
        ep = f"{s.episodes} odc." if s.episodes else "? odc."
        return f"{s.rank}.  {s.title_romaji}   •   {fmt} · {ep}   •   {mal}   •   {s.confidence:.1f}%"

    def _add_review_row(self, entry, suggs) -> None:
        row_tag = f"rev_row_{entry.local_id}"
        rowref: list = []   # closure łapie wiersz bez polegania na sender/app_data
        with dpg.group(tag=row_tag, parent="review_list"):
            with dpg.group(horizontal=True, horizontal_spacing=10):
                t = dpg.add_text(f"#{entry.local_id}  {entry.title}", color=TEXT)
                if self.font_bold:
                    self._bindf(t, "bold")
                rating = "-" if entry.rating is None else f"{entry.rating:g}"
                dpg.add_text(
                    f"[{entry.status_pl} · ocena {rating} · "
                    f"{entry.watched}/{entry.total} · {entry.type}]",
                    color=MUTED,
                )
            if suggs:
                labels = [self._sugg_label(s) for s in suggs]
                combo = dpg.add_combo(
                    items=labels,
                    default_value=labels[0],
                    width=640,
                    callback=lambda *a: self._on_sugg_combo(*a, row=rowref[0] if rowref else None),
                )
                with dpg.group(horizontal=True, horizontal_spacing=8):
                    dpg.add_button(
                        label="Zapisz wybór",
                        width=140,
                        height=30,
                        callback=lambda *a: self._on_save_choice(row=rowref[0] if rowref else None),
                    )
                    dpg.add_button(
                        label="Odrzuć wpis",
                        width=130,
                        height=30,
                        callback=lambda *a: self._on_reject(row=rowref[0] if rowref else None),
                    )
                detail = dpg.add_text(
                    "", color=MUTED, wrap=getattr(self, "review_wrap", 1000)
                )
                with dpg.group(horizontal=True, horizontal_spacing=8):
                    mal_in = dpg.add_input_int(
                        default_value=0,
                        min_value=0,
                        max_value=999999,
                        width=140,
                        min_clamped=True,
                    )
                    dpg.add_button(
                        label="Zapisz ręcznie MAL ID",
                        width=200,
                        height=30,
                        callback=lambda *a: self._on_save_manual(row=rowref[0] if rowref else None),
                    )
                    dpg.add_text("(gdy AniList nie zwróciło idMal)", color=MUTED)
            else:
                combo = None
                detail = dpg.add_text(
                    "Brak sugestii z AniList - wpisz MAL ID ręcznie albo odrzuć wpis.",
                    color=AMBER,
                    wrap=getattr(self, "review_wrap", 1000),
                )
                with dpg.group(horizontal=True, horizontal_spacing=8):
                    mal_in = dpg.add_input_int(
                        default_value=0,
                        min_value=0,
                        max_value=999999,
                        width=140,
                        min_clamped=True,
                    )
                    dpg.add_button(
                        label="Zapisz ręcznie MAL ID",
                        width=200,
                        height=30,
                        callback=lambda *a: self._on_save_manual(row=rowref[0] if rowref else None),
                    )
                    dpg.add_button(
                        label="Odrzuć wpis",
                        width=130,
                        height=30,
                        callback=lambda *a: self._on_reject(row=rowref[0] if rowref else None),
                    )
            dpg.add_separator()
            dpg.add_spacer(height=2)

        row = {
            "entry": entry,
            "suggs": suggs,
            "combo": combo,
            "detail": detail,
            "mal_in": mal_in,
            "group": row_tag,
        }
        dpg.set_item_user_data(combo or detail, row_tag)
        dpg.set_item_user_data(detail, row_tag)
        dpg.set_item_user_data(mal_in, row_tag)
        for child in dpg.get_item_children(row_tag, slot=1) or []:
            for sub in [child] + (dpg.get_item_children(child, slot=1) or []):
                dpg.set_item_user_data(sub, row_tag)
        rowref.append(row)
        self.review_rows[entry.local_id] = row
        if suggs:
            self._fill_detail(row, suggs[0])

    def _row_of(self, sender) -> dict | None:
        tag = dpg.get_item_user_data(sender)
        if not tag:
            parent = dpg.get_item_info(sender).get("parent")
            while parent:
                tag = dpg.get_item_user_data(parent)
                if tag:
                    break
                parent = dpg.get_item_info(parent).get("parent")
        if not tag:
            return None
        local_id = int(str(tag).rsplit("_", 1)[-1])
        return self.review_rows.get(local_id)

    def _fill_detail(self, row: dict, sugg) -> None:
        parts = [f"romaji: {sugg.title_romaji}"]
        if sugg.title_english:
            parts.append(f"english: {sugg.title_english}")
        if sugg.title_native:
            parts.append(f"native: {sugg.title_native}")
        parts.append(f"format: {sugg.format or '?'}")
        parts.append(f"odcinki: {sugg.episodes if sugg.episodes else '?'}")
        parts.append(f"status: {sugg.status or '?'}")
        parts.append(
            f"MAL ID: {sugg.mal_id if sugg.mal_id else 'BRAK (trzeba wpisać ręcznie)'}"
        )
        parts.append(f"pewność: {sugg.confidence:.1f}%")
        dpg.configure_item(row["detail"], default_value="   •   ".join(parts))

    def _on_sugg_combo(self, sender=None, app_data=None, user_data=None, row=None) -> None:
        row = row or self._row_of(sender)
        if not row:
            return
        selected = app_data if isinstance(app_data, str) else dpg.get_value(row["combo"])
        if not isinstance(selected, str):
            return
        for sugg in row["suggs"]:
            if self._sugg_label(sugg) == selected:
                self._fill_detail(row, sugg)
                break

    def _selected_sugg(self, row: dict):
        if not row["suggs"]:
            return None
        current = dpg.get_value(row["combo"])
        for sugg in row["suggs"]:
            if self._sugg_label(sugg) == current:
                return sugg
        return row["suggs"][0]

    def _drop_row(self, row: dict) -> None:
        local_id = row["entry"].local_id
        if dpg.does_item_exist(row["group"]):
            dpg.delete_item(row["group"])
        self.review_rows.pop(local_id, None)
        self.review_remaining = max(0, self.review_remaining - 1)
        self._update_review_counter()

    def _on_save_choice(self, sender=None, app_data=None, user_data=None, row=None) -> None:
        row = row or self._row_of(sender)
        if not row:
            return
        sugg = self._selected_sugg(row)
        if sugg is None:
            self._modal(
                "Ten wpis nie ma sugestii z AniList - użyj ręcznego MAL ID.", AMBER
            )
            return
        if sugg.mal_id is None:
            self._modal(
                "Wybrana sugestia nie ma MAL ID.\n"
                "Wybierz inną propozycję albo wpisz MAL ID ręcznie.",
                AMBER,
            )
            return
        try:
            match = self.converter.confirm_suggestion(sugg)
        except Exception as exc:  # noqa: BLE001
            self._modal(f"Nie udało się zapisać:\n{exc}", RED)
            return
        self.log(
            f"Weryfikacja: #{row['entry'].local_id} {row['entry'].title} -> "
            f"MAL {match.mal_id} ({match.title_romaji})",
            GREEN,
        )
        self._drop_row(row)
        self._refresh_confirmed_line()
        dpg.configure_item(
            self.w["btn_xml"], enabled=len(self.converter.confirmed_matches) > 0
        )

    def _on_save_manual(self, sender=None, app_data=None, user_data=None, row=None) -> None:
        row = row or self._row_of(sender)
        if not row:
            return
        mal_id = int(dpg.get_value(row["mal_in"]) or 0)
        if mal_id <= 0:
            self._modal("Wpisz dodatnie MAL ID (liczba, np. 37150).", AMBER)
            return
        sugg = self._selected_sugg(row)
        try:
            match = self.converter.confirm_manual_mal_id(
                entry_local_id=row["entry"].local_id,
                mal_id=mal_id,
                anilist_id=getattr(sugg, "anilist_id", None),
                title_romaji=getattr(sugg, "title_romaji", None) or row["entry"].title,
                total_episodes=getattr(sugg, "episodes", None),
            )
        except Exception as exc:  # noqa: BLE001
            self._modal(f"Nie udało się zapisać ręcznego MAL ID:\n{exc}", RED)
            return
        self.log(
            f"Weryfikacja: #{row['entry'].local_id} {row['entry'].title} -> "
            f"ręczne MAL {match.mal_id}",
            GREEN,
        )
        self._drop_row(row)
        self._refresh_confirmed_line()
        dpg.configure_item(
            self.w["btn_xml"], enabled=len(self.converter.confirmed_matches) > 0
        )

    def _on_reject(self, sender=None, app_data=None, user_data=None, row=None) -> None:
        row = row or self._row_of(sender)
        if not row:
            return
        removed = self.converter.reject_entry(row["entry"].local_id)
        self.log(
            f"Weryfikacja: odrzucono #{row['entry'].local_id} {row['entry'].title}"
            + ("" if removed else " (nie był na liście niepewnych)"),
            AMBER,
        )
        self._drop_row(row)

    # -------------------------------------------------------------------- XML
    def _on_generate_click(self, sender=None, app_data=None, user_data=None) -> None:
        if not self.converter.confirmed_matches:
            self._modal(
                "Brak potwierdzonych wpisów - nie ma czego zapisywać.\n"
                "Zweryfikuj niepewne dopasowania albo uruchom analizę.",
                AMBER,
            )
            return
        path = dpg.get_value(self.w["xml_path"]).strip().strip('"')
        if not path:
            self._modal("Ustaw ścieżkę pliku wyjściowego XML.", AMBER)
            return
        if not path.lower().endswith(".xml"):
            path += ".xml"
        self._do_generate(path)

    def _on_pick_xml_path(self, sender=None, app_data=None, user_data=None) -> None:
        if not self.converter.confirmed_matches:
            self._modal(
                "Brak potwierdzonych wpisów - nie ma czego zapisywać.\n"
                "Zweryfikuj niepewne dopasowania albo uruchom analizę.",
                AMBER,
            )
            return
        self._show("dlg_save_xml")

    def _on_xml_path_picked(self, *args) -> None:
        app_data = self._dialog_app_data("dlg_save_xml", args)
        path = app_data.get("file_path_name") if isinstance(app_data, dict) else None
        if not path:
            selections = (app_data or {}).get("selections") or {}
            path = next(iter(selections.values()), None)
        if not path:
            return
        if not str(path).lower().endswith(".xml"):
            path = f"{path}.xml"
        dpg.set_value(self.w["xml_path"], str(path))
        self._do_generate(str(path))

    def _do_generate(self, path: str) -> None:
        try:
            result = self.converter.generate_xml(output_path=path)
        except Exception as exc:  # noqa: BLE001
            self._modal(f"Generowanie XML nie powiodło się:\n{exc}", RED)
            self.log(f"XML: BŁĄD zapisu {path}: {exc}", RED)
            return
        msg = (
            f"XML: {result.count} wpisów"
            + (
                f", pominięto {result.skipped_missing_mal_id} bez MAL ID"
                if result.skipped_missing_mal_id
                else ""
            )
            + f" -> {result.path}"
        )
        self.log(msg, GREEN)
        dpg.configure_item(
            self.w["xml_status"],
            default_value=f"Zapisano {result.count} wpisów do {result.path}"
            + (
                f"  (pominięto bez MAL ID: {result.skipped_missing_mal_id})"
                if result.skipped_missing_mal_id
                else ""
            )
            + "  →  teraz okno „Import do MAL / AniList”.",
            color=GREEN,
        )
        self._modal(
            f"Zapisano plik:\n{result.path}\n\n"
            f"Wpisów w XML: {result.count}\n"
            + (
                f"Pominiętych (brak MAL ID): {result.skipped_missing_mal_id}\n"
                if result.skipped_missing_mal_id
                else ""
            )
            + "\nDalej: myanimelist.net/import.php  albo  anilist.co/settings/import\n"
            "(szczegóły w oknie „Import do MAL / AniList”).",
            GREEN,
            second_button="Otwórz okno importu",
            second_cb=lambda *a: (
                dpg.configure_item("win_modal", show=False),
                self._show("win_import"),
            ),
        )

    # ---------------------------------------------------------------- LAYOUT
    def layout_main(self) -> None:
        """Dopasowuje kolumny okna głównego do realnego rozmiaru viewportu."""
        cw, ch = self._get_viewport_size()

        log_w = 430 if cw > 1450 else (360 if cw > 1250 else 300)
        self.log_wrap = log_w - 26
        left_w = max(420, cw - log_w - 12 - 28 - 6)
        self.left_wrap = left_w - 40
        body_h = max(300, ch - 118)

        dpg.configure_item("main_left", width=left_w, height=body_h)
        for key in ("csv_status", "xml_status", "confirmed_line", "prog_current"):
            item = self.w.get(key)
            if item and dpg.does_item_exist(item):
                dpg.configure_item(item, wrap=self.left_wrap)
        dpg.configure_item("log_child", width=log_w, height=body_h)

    def layout_info_windows(self) -> None:
        """Ustawia okna informacyjne zależnie od realnego rozmiaru viewportu."""
        cw, ch = self._get_viewport_size()

        h_w = min(860, max(560, int(cw * 0.46)))
        i_w = min(820, max(520, int(cw * 0.44)))
        height = max(420, ch - 130)

        dpg.configure_item("win_help", width=h_w, height=height, pos=(28, 66))
        dpg.configure_item(
            "win_import",
            width=i_w,
            height=height,
            pos=(max(h_w + 60, cw - i_w - 28), 66),
        )

        for tag, width in (("win_help", h_w - 60), ("win_import", i_w - 60)):
            for child in dpg.get_item_children(tag, slot=1) or []:
                info = dpg.get_item_info(child)
                if info.get("type") == "mvText":
                    cur = dpg.get_item_configuration(child).get("wrap", -1)
                    if cur not in (None, -1):
                        dpg.configure_item(child, wrap=width)

    def layout_all(self) -> None:
        self.layout_main()
        self.layout_info_windows()

    def _on_resize(self, *args) -> None:
        self.layout_all()
        try:
            if dpg.get_item_configuration("win_review").get("show"):
                self._place_review()
        except Exception:
            pass

    # -------------------------------------------------------------------- RUN
    def run(self) -> None:
        dpg.setup_dearpygui()
        dpg.show_viewport(maximized=True)
        try:
            dpg.maximize_viewport()
        except Exception:
            pass

        # Rejestrujemy callback dla zmiany rozmiaru okna
        dpg.set_viewport_resize_callback(lambda *a: self._on_resize(*a))

        # Przez pierwszych ~30 klatek wymuszamy przeliczenie układu co klatkę:
        # system potrafi nadać oknu realne wymiary (maximize / DPI) z opóźnieniem.
        # UWAGA: nic nie rejestruj pod set_frame_callback(1, ...) - ten slot
        # należy do ticka (patrz build()), inaczej kolejka jobów UI umiera
        # (pusty dziennik, martwy progress).
        self._force_relayout = 30
        self.layout_all()

        self.log("Aplikacja gotowa. Wybierz plik CSV, żeby zacząć.", CYAN)
        dpg.start_dearpygui()
        dpg.destroy_context()


def main() -> None:
    app = ConverterApp()
    app.build()
    app.run()


if __name__ == "__main__":
    main()
