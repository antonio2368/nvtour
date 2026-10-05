#!/usr/bin/env python3
"""Render a ~50 s trailer of nvtour as an MP4, without a terminal or display.

Like render_demo.py, a private `nvim --embed` is drawn from its UI grid and the tour runs through the real
CLI. Around it: title cards, an agent pane that types the commands, captions and key overlays. Each command
glows while it runs and sends a dot to nvim; the screen cells it changed (a diff of the UI grid before and
after) are outlined and labelled, and the pane that acts stays bright while the other one dims.

    python3 scripts/render_trailer.py                     # writes docs/nvtour.mp4 and docs/trailer-poster.png
    python3 scripts/render_trailer.py --stills /tmp/st    # also writes a PNG at each scene
    python3 scripts/render_trailer.py --colorscheme gruvbox --out /tmp/trailer-gruvbox.mp4

Needs pynvim, Pillow and imageio-ffmpeg (`pip install imageio-ffmpeg`; it bundles an ffmpeg binary).
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_demo as rd  # noqa: E402

W, H = 1920, 1080
BG = (13, 15, 20)
PANE = (21, 24, 31)
CHROME = (31, 35, 45)
BORDER = (48, 53, 66)
TEXT = (222, 226, 232)
DIM = (128, 136, 150)
ACCENT = (110, 196, 250)
GREEN = (126, 211, 134)
AMBER = (236, 190, 100)
RED = (240, 115, 115)
FLAGC = (150, 165, 190)
STRC = (226, 200, 150)
FX = (255, 196, 87)  # effects layer: command glow, flying dot, change outlines, callouts
INK = (24, 20, 12)
DIM_LEVEL = 0.4  # how much the pane that does not act is dimmed (more makes small text break up in the video)

SOLO_RECT = (310, 80, 1300, 820)  # x, y, w, h
CHAT_RECT = (36, 64, 540, 864)
NVIM_RECT = (596, 64, 1288, 864)
LABEL_Y = 26
TITLE_H = 40
CAPTION_Y = 958
NVIM_FONT, NVIM_PAD = 20, 10
NO_LIGATURES = ["-liga", "-calt"]  # keep `->` and `<<` as typed
PLUGIN_DIRS = [Path.home() / ".local/share/nvim/lazy", Path.home() / ".local/share/nvim/site/pack"]

REPO_URL = "github.com/antonio2368/nvtour"
NB = "\xa0"  # keeps "line 16" on one row

CODE = (re.compile(r"`[^`]+`"), ACCENT, "regular")
LINE_REF = (re.compile(rf"[Ll]ines?{NB}\d+(?:-\d+)?"), AMBER, "bold")
FLAG = (re.compile(r"(?<= )--[\w-]+"), FLAGC, "regular")
STRING = (re.compile(r"'[^']*'"), STRC, "regular")

# kind: (prefix, prefix colour, text colour, text style, highlight rules)
KINDS = {
    "user": ("> ", TEXT, TEXT, "bold", ()),
    "agent": ("● ", ACCENT, TEXT, "regular", (CODE, LINE_REF)),
    "cmd": ("$ ", GREEN, TEXT, "regular", (FLAG, STRING)),
    "out": ("  ", DIM, DIM, "regular", ()),
}

ANSWER = re.sub(r"([Ll]ines?) (\d)", rf"\1{NB}\2", (
    "`get()` finds the entry on line 16 and keeps the iterator `it`. Line 19 calls `refresh(key)`, which "
    "(lines 50-55) calls `entries.extract(key)` on line 52. That unlinks the node and invalidates `it`; line 54 "
    "puts it back with `insert()`. Back in `get()`, line 20 reads `it->second.value` through the invalid "
    "iterator. `cleanupExpired()` on lines 30-40 also erases entries (line 36), but under the same mutex, so "
    "that is not it. To fix it, change line 19 to update `expires_at` through `it` and remove lines 49-55."))


class Fonts:
    def __init__(self, paths: dict[str, Path]):
        self.paths = paths
        self.cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}

    def get(self, size: int, style: str = "regular") -> ImageFont.FreeTypeFont:
        if (size, style) not in self.cache:
            self.cache[size, style] = ImageFont.truetype(str(self.paths[style]), size)
        return self.cache[size, style]

    def set(self, size: int) -> dict[str, ImageFont.FreeTypeFont]:
        return {k: self.get(size, k) for k in self.paths}


def mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def attrs_for(text: str, color: tuple, style: str, rules=()) -> list[tuple]:
    attrs = [(color, style)] * len(text)
    for rx, c, s in rules:
        for m in rx.finditer(text):
            attrs[m.start():m.end()] = [(c, s)] * (m.end() - m.start())
    return attrs


def draw_attrs(d: ImageDraw.ImageDraw, x: float, y: float, text: str, attrs: list, fonts: Fonts, size: int,
               fade: float = 1.0) -> None:
    cw = fonts.get(size).getlength("M")
    i = 0
    while i < len(text):
        j = i
        while j < len(text) and attrs[j] == attrs[i]:
            j += 1
        color, style = attrs[i]
        d.text((x + i * cw, y), text[i:j], font=fonts.get(size, style), fill=mix(BG, color, fade),
               features=NO_LIGATURES)
        i = j


def wrap(text: str, width: int) -> list[tuple[int, int]]:
    """Greedy word wrap that returns (start, end) offsets into `text`, so highlights survive wrapping."""
    rows, pos = [], 0
    for para in text.split("\n"):
        start, end = pos, pos + len(para)
        if start == end:
            rows.append((start, end))
        while start < end:
            if end - start <= width:
                rows.append((start, end))
                break
            cut = text.rfind(" ", start, start + width + 1)
            if cut <= start:
                cut = start + width
            rows.append((start, cut))
            start = cut
            while start < end and text[start] == " ":
                start += 1
        pos = end + 1
    return rows


def center(d: ImageDraw.ImageDraw, y: int, text: str, font: ImageFont.FreeTypeFont, fill: tuple) -> None:
    d.text(((W - d.textlength(text, font=font, features=NO_LIGATURES)) / 2, y), text, font=font, fill=fill,
           features=NO_LIGATURES)


def pane(w: int, h: int, title: str, fill: tuple, fonts: Fonts) -> Image.Image:
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=14, fill=fill)
    d.rounded_rectangle([0, 0, w - 1, TITLE_H + 14], radius=14, fill=CHROME)
    d.rectangle([0, TITLE_H, w - 1, TITLE_H + 14], fill=fill)
    for i, c in enumerate([(237, 106, 94), (245, 191, 79), (98, 197, 84)]):
        cx, cy = 22 + i * 22, TITLE_H // 2
        d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=mix(CHROME, c, 0.8))
    font = fonts.get(18)
    d.text(((w - d.textlength(title, font=font)) / 2, TITLE_H // 2 - 11), title, font=font, fill=DIM)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=14, outline=BORDER, width=1)
    return img


@dataclass
class Item:
    kind: str
    text: str
    shown: int | None = None  # characters typed so far; None = complete
    active: bool = False  # the command that is running: drawn with a glow


def render_chat(items: list[Item], w: int, h: int, title: str, fonts: Fonts, size: int) -> tuple[Image.Image, int | None]:
    """The chat pane, and the y of the last row of the active item (relative to the pane) if it is visible."""
    img = pane(w, h, title, PANE, fonts)
    d = ImageDraw.Draw(img)
    cw = fonts.get(size).getlength("M")
    lh, pad = round(size * 1.5), 22
    cols = int((w - 2 * pad) / cw) - 2
    rows: list[tuple] = []  # (prefix, prefix colour, text, attrs, cursor, active)
    for it in items:
        if it.kind == "gap":
            rows.append(("", TEXT, "", [], False, False))
            continue
        prefix, pcolor, color, style, rules = KINDS[it.kind]
        attrs = attrs_for(it.text, color, style, rules)
        limit = len(it.text) if it.shown is None else it.shown
        for n, (a, b) in enumerate(wrap(it.text, cols)):
            if a > limit or (a == limit and n > 0):
                break
            e = min(b, limit)
            rows.append((prefix if n == 0 else "  ", pcolor, it.text[a:e], attrs[a:e],
                         it.shown is not None and e == limit, it.active))
    rows = rows[-((h - TITLE_H - 2 * pad) // lh):]
    y, active_y = TITLE_H + pad, None
    for prefix, pcolor, text, attrs, cursor, active in rows:
        if active:
            d.rectangle([pad - 12, y - 5, w - pad + 8, y + lh - 5], fill=mix(PANE, FX, 0.13))
            d.rectangle([pad - 12, y - 5, pad - 9, y + lh - 5], fill=FX)
            active_y = y + lh // 2 - 4
        d.text((pad, y), prefix, font=fonts.get(size, "bold"), fill=pcolor)
        draw_attrs(d, pad + 2 * cw, y, text, attrs, fonts, size)
        if cursor:
            x = pad + (2 + len(text)) * cw
            d.rectangle([x, y + 2, x + cw - 1, y + size + 4], fill=TEXT)
        y += lh
    return img, active_y


def smooth(t: float) -> float:
    return t * t * (3 - 2 * t)


def changed_boxes(before: list[list], after: list[list]) -> list[tuple[int, int, int, int, int]]:
    """Boxes (r0, c0, r1, c1, cells) around the grid cells that changed, largest first.

    Each window segment of a row is compared with the same segment a few rows up or down too, so lines that
    only moved (for example below an inserted note) do not count as changed. The status line and the
    command line are ignored.
    """
    rows, cols = len(after) - 2, len(after[0])
    seps = [c for c in range(cols) if sum(after[r][c][0] == "│" for r in range(rows)) > rows // 2]
    bounds = [-1, *seps, cols]
    segments = [(a + 1, b) for a, b in zip(bounds, bounds[1:]) if b > a + 1]
    todo = set()
    for r in range(rows):
        for a, b in segments:
            best = None
            for k in sorted(range(-6, 7), key=abs):
                if 0 <= r + k < rows:
                    diff = [c for c in range(a, b) if after[r][c] != before[r + k][c]]
                    if best is None or len(diff) < len(best):
                        best = diff
            todo.update((r, c) for c in best or [])
    boxes = []
    while todo:
        stack, cells = [todo.pop()], []
        while stack:
            r, c = stack.pop()
            cells.append((r, c))
            for dr in (-1, 0, 1):
                for dc in range(-3, 4):
                    if (r + dr, c + dc) in todo:
                        todo.remove((r + dr, c + dc))
                        stack.append((r + dr, c + dc))
        rs, cs = [r for r, _ in cells], [c for _, c in cells]
        boxes.append((min(rs), min(cs), max(rs), max(cs), len(cells)))
    return sorted((b for b in boxes if b[4] >= 2), key=lambda b: -b[4])


POSTER_AT = "fx-step"  # the first step, with its callouts


def save_poster(frame: Image.Image, path: Path) -> None:
    """The README image: a frame of the trailer with a play button, at 1280x720."""
    img = frame.convert("RGBA")
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([0, 0, W, H], fill=(0, 0, 0, 70))
    cx, cy, r = W / 2, H / 2 - 40, 92
    d.ellipse([cx - r, cy - r + 8, cx + r, cy + r + 8], fill=(0, 0, 0, 120))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*FX, 240))
    d.polygon([(cx - 28, cy - 44), (cx - 28, cy + 44), (cx + 48, cy)], fill=(*INK, 255))
    img = Image.alpha_composite(img, layer).convert("RGB")
    img.resize((1280, 720), Image.Resampling.LANCZOS).save(path, optimize=True)


def heavier(paths: dict[str, Path]) -> dict[str, Path]:
    """Medium and ExtraBold instead of Regular and Bold when they exist: thin strokes break up when the
    browser scales the video down."""
    names = {"regular": ("Regular", "Medium"), "bold": ("Bold", "ExtraBold"), "italic": ("Italic", "MediumItalic"),
             "bold_italic": ("BoldItalic", "ExtraBoldItalic")}
    out = dict(paths)
    for style, (old, new) in names.items():
        p = paths[style]
        q = p.with_name(p.name.replace(f"-{old}.", f"-{new}."))
        if q != p and q.exists():
            out[style] = q
    return out


class Video:
    def __init__(self, path: str, fps: int, stills: Path | None, poster: Path | None = None,
                 size: tuple[int, int] = (W, H)):
        self.fps, self.stills, self.poster, self.frames = fps, stills, poster, 0
        # Frames are drawn at W x H; ffmpeg scales them to `size` with lanczos. A browser scales video with
        # a simple filter, which drops pixels of small text when it shrinks by more than about 2x, so a
        # smaller video looks cleaner in a README.
        scale = [] if size == (W, H) else ["-vf", f"scale={size[0]}:{size[1]}:flags=lanczos"]
        self.gen = imageio_ffmpeg.write_frames(
            path, (W, H), fps=fps, codec="libx264", quality=None, macro_block_size=8,  # type: ignore[arg-type]
            output_params=[*scale, "-crf", "16", "-preset", "slow", "-tune", "animation",
                           "-movflags", "+faststart"])
        self.gen.send(None)
        self.last = Image.new("RGB", (W, H), (0, 0, 0))

    def emit(self, img: Image.Image) -> None:
        self.gen.send(img.tobytes())
        self.last = img
        self.frames += 1

    def hold(self, seconds: float) -> None:
        data = self.last.tobytes()
        for _ in range(round(seconds * self.fps)):
            self.gen.send(data)
            self.frames += 1

    def fade(self, img: Image.Image, seconds: float) -> None:
        start, n = self.last, max(1, round(seconds * self.fps))
        for i in range(1, n + 1):
            self.emit(Image.blend(start, img, i / n))

    def still(self, name: str) -> None:
        if self.stills:
            self.last.save(self.stills / f"{self.frames:05d}-{name}.png")
        if self.poster and name == POSTER_AT:
            save_poster(self.last, self.poster)
            self.poster = None

    def close(self) -> None:
        self.gen.close()


@dataclass
class Callout:
    label: str
    target: tuple[float, float]
    pos: tuple[float, float]
    fade: float = 0.0


class Stage:
    """The two-pane scene: agent chat on the left, nvim on the right, a caption below, effects on top."""

    def __init__(self, video: Video, fonts: Fonts):
        self.v, self.f = video, fonts
        self.layout = "solo"
        self.items: list[Item] = []
        self.nvim_pane: Image.Image | None = None
        self.caption, self.cap_fade = "", 1.0
        self.key, self.key_fade = "", 0.0
        self.dim = {"agent": 0.0, "nvim": 0.0}
        self.active_y: float | None = None
        self.packet: list[tuple[float, float]] = []  # positions of the flying dot, newest last
        self.boxes: list[tuple[float, float, float, float]] = []
        self.box_fade, self.box_grow = 0.0, 0.0
        self.effect, self.effect_at, self.effect_fade = "", (0.0, 0.0), 0.0
        self.callouts: list[Callout] = []

    # Drawing.

    def render(self) -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        if self.layout == "solo":
            x, y, w, h = SOLO_RECT
            img.paste(render_chat(self.items, w, h, "agent", self.f, 26)[0], (x, y))
        else:
            x, y, w, h = CHAT_RECT
            chat, ay = render_chat(self.items, w, h, "agent", self.f, 19)
            self.active_y = None if ay is None else y + ay
            img.paste(chat, (x, y))
            assert self.nvim_pane is not None
            img.paste(self.nvim_pane, NVIM_RECT[:2])
            for who, rect in (("agent", CHAT_RECT), ("nvim", NVIM_RECT)):
                if self.dim[who] > 0:
                    x, y, w, h = rect
                    box = (x, y, x + w, y + h)
                    img.paste(Image.blend(img.crop(box), Image.new("RGB", (w, h), BG), self.dim[who]), box)
            d = ImageDraw.Draw(img)
            self.pane_label(d, CHAT_RECT, "AGENT", "runs nvtour commands", self.dim["agent"])
            self.pane_label(d, NVIM_RECT, "YOUR NEOVIM", "already open", self.dim["nvim"])
            img = self.effects(img)
        if self.caption:
            size = 46
            cw = self.f.get(size).getlength("M")
            d = ImageDraw.Draw(img)
            draw_attrs(d, (W - len(self.caption) * cw) / 2, CAPTION_Y, self.caption,
                       attrs_for(self.caption, TEXT, "regular", (CODE,)), self.f, size, self.cap_fade)
        return img

    def pane_label(self, d: ImageDraw.ImageDraw, rect: tuple, name: str, note: str, dim: float) -> None:
        on = 1 - dim / DIM_LEVEL
        x = rect[0] + 6
        d.ellipse([x, LABEL_Y + 4, x + 14, LABEL_Y + 18], fill=mix(BORDER, FX, on))
        bold = self.f.get(24, "bold")
        d.text((x + 24, LABEL_Y - 6), name, font=bold, fill=mix(DIM, TEXT, on))
        d.text((x + 24 + d.textlength(name + "  ", font=bold), LABEL_Y - 3), note, font=self.f.get(20),
               fill=mix(BORDER, DIM, on))

    def pill(self, d: ImageDraw.ImageDraw, x: float, y: float, text: str, alpha: int) -> tuple[float, float, float, float]:
        font = self.f.get(24, "bold")
        w = d.textlength(text, font=font, features=NO_LIGATURES) + 30
        x = min(max(x, NVIM_RECT[0] + 8), NVIM_RECT[0] + NVIM_RECT[2] - w - 8)
        d.rounded_rectangle([x, y + 4, x + w, y + 46], radius=12, fill=(0, 0, 0, alpha // 2))
        d.rounded_rectangle([x, y, x + w, y + 42], radius=12, fill=(*FX, alpha))
        d.text((x + 15, y + 6), text, font=font, fill=(*INK, alpha), features=NO_LIGATURES)
        return x, y, x + w, y + 42

    def effects(self, img: Image.Image) -> Image.Image:
        if not (self.boxes and self.box_fade > 0 or self.effect and self.effect_fade > 0 or self.packet
                or self.callouts or self.key and self.key_fade > 0):
            return img
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if self.box_fade > 0:
            a, g = round(255 * self.box_fade), self.box_grow
            for x0, y0, x1, y1 in self.boxes:
                d.rounded_rectangle([x0 - g - 5, y0 - g - 5, x1 + g + 5, y1 + g + 5], radius=10,
                                    outline=(*FX, a // 4), width=7)
                d.rounded_rectangle([x0 - g, y0 - g, x1 + g, y1 + g], radius=7, outline=(*FX, a), width=3)
        if self.effect and self.effect_fade > 0:
            self.pill(d, *self.effect_at, self.effect, round(255 * self.effect_fade))
        for c in self.callouts:
            a = round(255 * c.fade)
            if a == 0:
                continue
            tx, ty = c.target
            x0, y0, x1, y1 = self.pill(d, c.pos[0], c.pos[1], c.label, a)
            ax = min(max(tx, x0 + 12), x1 - 12)
            ay = y0 if ty < y0 else y1
            d.line([ax, ay, tx, ty], fill=(*FX, a), width=3)
            d.ellipse([tx - 7, ty - 7, tx + 7, ty + 7], fill=(*FX, a), outline=(*INK, a), width=2)
        for i, (px, py) in enumerate(self.packet):
            k = (i + 1) / len(self.packet)
            r = 4 + 7 * k
            d.ellipse([px - r, py - r, px + r, py + r], fill=(*FX, round(220 * k)))
        if self.packet:
            px, py = self.packet[-1]
            d.ellipse([px - 24, py - 24, px + 24, py + 24], fill=(*FX, 55))
        if self.key and self.key_fade > 0:
            font = self.f.get(54, "bold")
            tw = d.textlength(self.key, font=font)
            x, y, w, h = NVIM_RECT
            bw, bh = tw + 70, 100
            bx, by = x + (w - bw) / 2, y + h - bh - 40
            a = round(255 * self.key_fade)
            d.rounded_rectangle([bx, by + 6, bx + bw, by + bh + 6], radius=16, fill=(0, 0, 0, a // 2))
            d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=16, fill=(42, 47, 60, a), outline=(*FX, a),
                                width=3)
            d.text((bx + 35, by + 14), self.key, font=font, fill=(*TEXT, a))
            d.text((bx + bw + 18, by + 34), "you", font=self.f.get(24, "bold"), fill=(*FX, a))
        return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    # Animation.

    def show(self) -> None:
        self.v.emit(self.render())

    def hold(self, seconds: float) -> None:
        self.show()
        self.v.hold(max(0.0, seconds - 1 / self.v.fps))

    def animate(self, seconds: float, step) -> None:
        n = max(1, round(seconds * self.v.fps))
        for i in range(1, n + 1):
            step(i / n)
            self.show()

    def focus(self, who: str, seconds: float = 0.2) -> None:
        """Dim the pane that does not act. `who`: agent, nvim or both."""
        goal = {"agent": DIM_LEVEL if who == "nvim" else 0.0, "nvim": DIM_LEVEL if who == "agent" else 0.0}
        start = dict(self.dim)
        if start == goal:
            return

        def step(t: float) -> None:
            for k in goal:
                self.dim[k] = start[k] + (goal[k] - start[k]) * smooth(t)
        self.animate(seconds, step)

    def say(self, text: str) -> None:
        if self.caption:
            for i in range(5, -1, -1):
                self.cap_fade = i / 6
                self.show()
        self.caption = text
        for i in range(1, 10):
            self.cap_fade = i / 9
            self.show()

    def add(self, kind: str, text: str = "") -> None:
        self.items.append(Item(kind, text))

    def type(self, kind: str, text: str, cps: float) -> Item:
        item = Item(kind, text, 0)
        self.items.append(item)
        typed = 0.0
        while item.shown < len(text):  # type: ignore[operator]
            typed += cps / self.v.fps
            item.shown = min(len(text), int(typed))
            self.show()
        item.shown = None
        return item

    def fly(self, target: tuple[float, float], seconds: float = 0.55) -> None:
        """Send a dot from the running command to `target` in nvim, moving the focus to nvim on the way."""
        x0 = CHAT_RECT[0] + CHAT_RECT[2] - 26
        y0 = self.active_y if self.active_y is not None else CHAT_RECT[1] + CHAT_RECT[3] / 2
        x2, y2 = target
        cx, cy = (x0 + x2) / 2, min(y0, y2) - 170
        start = dict(self.dim)
        trail: list[tuple[float, float]] = []

        def step(t: float) -> None:
            u = smooth(t)
            trail.append(((1 - u) ** 2 * x0 + 2 * (1 - u) * u * cx + u * u * x2,
                          (1 - u) ** 2 * y0 + 2 * (1 - u) * u * cy + u * u * y2))
            self.packet = trail[-7:]
            self.dim["agent"] = start["agent"] + (DIM_LEVEL - start["agent"]) * u
            self.dim["nvim"] = start["nvim"] * (1 - u)
        self.animate(seconds, step)
        self.packet = []

    def key_press(self, key: str, demo: Trailer, after: float) -> None:
        self.focus("nvim")
        self.key = key

        def up(t: float) -> None:
            self.key_fade = t
        self.animate(0.15, up)
        demo.nv.input(key)
        demo.refresh()
        self.hold(after)
        self.v.still("key")

        def down(t: float) -> None:
            self.key_fade = 1 - t
        self.animate(0.2, down)
        self.key = ""


class Trailer(rd.Demo):
    def __init__(self, stage: Stage, fonts: Fonts, setup_lua: str = ""):
        probe = fonts.get(NVIM_FONT)
        self.cw = round(probe.getlength("M"))
        asc, desc = probe.getmetrics()
        self.ch = asc + desc + 2
        _, _, w, h = NVIM_RECT
        cols = (w - 2 * NVIM_PAD - 8) // self.cw
        rows = (h - TITLE_H - 2 * NVIM_PAD - 8) // self.ch
        super().__init__(cols, rows, "vim.o.wrap = false\n" + setup_lua)  # long code lines would wrap
        self.stage, self.fonts = stage, fonts
        iw, ih = cols * self.cw + 2 * NVIM_PAD, rows * self.ch + 2 * NVIM_PAD
        self.paste_at = ((w - iw) // 2, TITLE_H + (h - TITLE_H - ih) // 2)
        self.origin = (NVIM_RECT[0] + self.paste_at[0] + NVIM_PAD, NVIM_RECT[1] + self.paste_at[1] + NVIM_PAD)

    def grid(self) -> list[list]:
        attrs = self.screen.attrs
        return [[(t, tuple(sorted(attrs.get(h, {}).items()))) for t, h in row] for row in self.screen.cells]

    def capture(self) -> tuple[Image.Image, list[tuple]]:
        """Redraw nvim; return the new pane image and the boxes of the cells that changed."""
        before = self.grid()
        screen = self.snapshot()
        boxes = changed_boxes(before, self.grid())
        _, _, w, h = NVIM_RECT
        img = pane(w, h, "nvim — docs/demo/cache.cpp", rd.rgb(screen.bg), self.fonts)
        img.paste(rd.draw(screen, self.fonts.set(NVIM_FONT), NVIM_PAD), self.paste_at)
        return img, boxes

    def refresh(self) -> None:
        self.stage.nvim_pane, _ = self.capture()

    def px(self, r0: int, c0: int, r1: int, c1: int, m: int = 5) -> tuple[float, float, float, float]:
        ox, oy = self.origin
        return ox + c0 * self.cw - m, oy + r0 * self.ch - m, ox + (c1 + 1) * self.cw + m, oy + (r1 + 1) * self.ch + m

    def find(self, needle: str, nth: int = 0, col0: bool = False) -> tuple[float, float] | None:
        """Pixel position (left, middle) of the nth occurrence of `needle` on the screen."""
        for r, row in enumerate(self.screen.cells):
            text, cell = "", []
            for c, (t, _) in enumerate(row):
                text += t
                cell += [c] * len(t)
            i = text.find(needle)
            while i >= 0:
                if nth == 0:
                    ox, oy = self.origin
                    return ox + (1 if col0 else cell[i]) * self.cw, oy + r * self.ch + self.ch / 2
                nth -= 1
                i = text.find(needle, i + 1)
        print(f"warning: callout target {needle!r} not found", file=sys.stderr)
        return None

    def run(self, *args: str, stdin: str | None = None) -> str:
        cmd = [sys.executable, "-m", "nvtour.cli", "--socket", self.sock, *args]
        r = subprocess.run(cmd, env=self.env, input=stdin, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            sys.exit(f"{' '.join(args)}: exit {r.returncode}\n{r.stderr}")
        out = re.sub(r"  socket \S+", "", r.stdout.replace(str(Path.home()), "~"))
        return out.rstrip("\n")

    def cmd(self, *args: str, effect: str, shown: str | None = None, stdin: str | None = None, cps: float = 150,
            after: float = 0.8, outline: str = "changes", callouts: tuple = ()) -> None:
        """Type the command, run it for real, fly a dot to nvim, then outline and label what changed.

        `outline`: "changes" boxes the changed cells, "whole" the whole screen (for commands that change all
        of it), "none" draws no box (use callouts instead).
        """
        st = self.stage
        st.focus("agent")
        item = st.type("cmd", shown or "nvtour " + shlex.join(args), cps)
        item.active = True
        st.hold(0.15)
        out = self.run(*args, stdin=stdin)
        new_pane, boxes = self.capture()

        rows, cols = self.screen.rows - 2, self.screen.cols
        whole = self.px(0, 0, rows - 1, cols - 1, 2)
        big = outline != "changes" or not boxes
        rects = [] if outline == "none" else [whole] if big else [self.px(*b[:4]) for b in boxes[:4]]
        x0, y0, x1, y1 = rects[0] if rects else whole
        st.fly(((x0 + x1) / 2, (y0 + y1) / 2) if not big else (NVIM_RECT[0] + NVIM_RECT[2] / 2, NVIM_RECT[1] + 200))

        st.nvim_pane = new_pane
        item.active = False
        for line in out.splitlines():
            st.add("out", line)
        st.boxes, st.effect = rects, effect
        if big:
            st.effect_at = (x0 + 10, y0 + 12)
        elif y0 - 52 >= self.origin[1]:
            st.effect_at = (x0, y0 - 52)
        else:
            st.effect_at = (x0, y1 + 10)

        def pulse(t: float) -> None:
            st.box_fade, st.box_grow, st.effect_fade = 1.0, 16 * (1 - smooth(t)), smooth(t)
        st.animate(0.35, pulse)
        for label, needle, nth, col0, dx, dy in callouts:
            target = self.find(needle, nth, col0)
            if target is None:
                continue
            c = Callout(label, target, (target[0] + dx, target[1] + dy))
            st.callouts.append(c)

            def grow(t: float, c: Callout = c) -> None:
                c.fade = smooth(t)
            st.animate(0.25, grow)
            st.hold(0.45)
        st.hold(after)
        st.v.still("fx-" + args[0])

        def fade(t: float) -> None:
            st.box_fade = st.effect_fade = 1 - t
            for c in st.callouts:
                c.fade = 1 - t
        st.animate(0.3, fade)
        st.boxes, st.effect, st.callouts = [], "", []


def title_card(f: Fonts) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    center(d, 300, "nvtour", f.get(170, "bold"), ACCENT)
    center(d, 530, "Your agent explains. Your editor shows.", f.get(54, "bold"), TEXT)
    center(d, 615, "Guided, read-only code walkthroughs in your running Neovim.", f.get(30), DIM)
    roles = [("✗ fault", RED), ("   → flow", ACCENT), ("   ✓ fix", GREEN)]
    font = f.get(32, "bold")
    x = (W - sum(d.textlength(t, font=font) for t, _ in roles)) / 2
    for t, c in roles:
        d.text((x, 730), t, font=font, fill=c)
        x += d.textlength(t, font=font)
    return img


def outro_card(f: Fonts) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    center(d, 230, "nvtour", f.get(130, "bold"), ACCENT)
    center(d, 420, "Show, don't just tell.", f.get(48, "bold"), TEXT)
    install = "pip install git+https://" + REPO_URL
    font = f.get(32)
    tw = d.textlength("$ " + install, font=font)
    x = (W - tw) / 2
    d.rounded_rectangle([x - 36, 540, x + tw + 36, 620], radius=14, fill=PANE, outline=BORDER)
    d.text((x, 560), "$ ", font=f.get(32, "bold"), fill=GREEN)
    d.text((x + d.textlength("$ ", font=font), 560), install, font=font, fill=TEXT)
    center(d, 680, "For Claude Code, Codex and any agent that can run a command.", f.get(28), DIM)
    center(d, 800, REPO_URL, f.get(40, "bold"), TEXT)
    return img


def colorscheme_lua(name: str) -> str:
    """Lua that puts the plugin providing colorscheme `name` on the runtimepath and loads it."""
    for root in PLUGIN_DIRS:
        for colors in sorted(root.glob("**/colors")):
            if any((colors / f"{name}.{ext}").exists() for ext in ("vim", "lua")):
                return (f"vim.opt.runtimepath:prepend({str(colors.parent)!r}) "
                        f"vim.o.background = 'dark' vim.cmd.colorscheme({name!r})")
    sys.exit(f"colorscheme {name!r} not found under {', '.join(map(str, PLUGIN_DIRS))}")


def fixed_source() -> str:
    src = (rd.REPO / rd.DEMO).read_text()
    src = src.replace("        refresh(key);\n", "        it->second.expires_at += ttl;\n")
    return re.sub(r"    /// Moves the node.*?\n    }\n\n", "", src, flags=re.S)


def main() -> None:
    p = argparse.ArgumentParser(description="Render the nvtour trailer as an MP4.")
    p.add_argument("--out", default=str(rd.REPO / "docs/nvtour.mp4"), help="output file")
    p.add_argument("--font-dir", action="append", type=Path, help="directory with the .ttf files")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--stills", type=Path, help="also write a PNG at each scene to this directory")
    p.add_argument("--colorscheme", help="load this colorscheme from your nvim plugins (default: built-in)")
    p.add_argument("--poster", type=Path, default=rd.REPO / "docs/trailer-poster.png",
                   help="README image: a frame with a play button")
    p.add_argument("--size", default="1280x720", help="size of the video, WIDTHxHEIGHT (frames are drawn at "
                   f"{W}x{H})")
    a = p.parse_args()
    fonts = Fonts(heavier(rd.find_fonts(a.font_dir or rd.FONT_DIRS)))
    if a.stills:
        a.stills.mkdir(parents=True, exist_ok=True)

    v = Video(a.out, a.fps, a.stills, a.poster, tuple(int(x) for x in a.size.split("x")))  # type: ignore[arg-type]
    s = Stage(v, fonts)
    t = Trailer(s, fonts, colorscheme_lua(a.colorscheme) if a.colorscheme else "")
    try:
        # 1. Title.
        v.emit(title_card(fonts))  # no fade-in: the first frame is the preview image of the video
        v.hold(3.0)
        v.still("title")

        # 2. The usual answer: a wall of line numbers.
        v.fade(s.render(), 0.5)
        s.type("user", "Why does Cache::get sometimes return garbage?", 45)
        s.hold(0.3)
        s.add("gap")
        s.type("agent", ANSWER, 260)
        s.say("Explaining code in chat: a wall of line numbers.")
        s.hold(2.0)
        v.still("chat")

        # 3. The agent builds a tour in nvim.
        t.refresh()
        s.layout, s.items, s.caption = "split", [], ""
        v.fade(s.render(), 0.6)
        s.say("The agent runs commands. Your Neovim shows the result.")
        s.type("user", "Why does Cache::get sometimes return garbage? Show me in nvim.", 50)
        s.add("gap")
        s.type("agent", "I'll walk you through it in your editor.", 120)
        t.cmd("attach", effect="connected to this nvim", outline="whole", after=0.4)
        t.cmd("start", "Dangling iterator in Cache::get", effect="new tour", outline="whole", after=0.2)
        s.say("Each step: a range, a role and a note.")
        t.cmd(*rd.TOUR[0], effect="step 1: jump + highlight", outline="none", after=0.8, callouts=(
            # label, text on screen, occurrence, point at column 0, label dx, dy
            ("range", "return it->second", 0, True, 30, 90),
            ("note", "is used after", 0, False, 330, -120),
            ("role + label", "1 dangling iterator", 0, False, -40, 70),
            ("step list", "dangling iterator", 1, False, 0, 150),
        ))
        s.say("Later steps are added without moving your view.")
        t.cmd(*rd.TOUR[1], effect="step 2 added · view stays", after=0.9)
        t.cmd(*rd.TOUR[2], effect="step 3: label + short note", after=0.9)
        s.say("A side panel lists the steps and a summary.")
        t.cmd("panel", "-", stdin=rd.SUMMARY, shown="nvtour panel - <<'EOF'\n" + rd.SUMMARY + "EOF", cps=220,
              effect="summary", after=1.0)
        s.focus("agent")
        s.add("gap")
        s.type("agent", "The tour is open in nvim.", 120)
        s.hold(0.8)

        # 4. The user steps through.
        s.say("Then you step through it, at your own pace.")
        s.key_press("]w", t, 1.3)
        s.key_press("]w", t, 1.1)
        s.key_press("[W", t, 1.0)

        # 5. Focus.
        s.say("The agent can fold away everything else ...")
        t.cmd("focus", f"{rd.DEMO}:13-21", "50-55", "--context", "1", effect="folded to the tour", outline="whole", after=1.5)
        t.cmd("unfocus", effect="unfolded", outline="whole", cps=200, after=0.2)

        # 6. Diff.
        s.say("... and show the fix as a read-only diff.")
        t.cmd("diff", rd.DEMO, "--stdin", "--title", "proposed fix", stdin=fixed_source(),
              shown=f"nvtour diff {rd.DEMO} --stdin --title 'proposed fix' < fix.cpp", effect="read-only diff tab",
              outline="whole", after=1.8)
        t.cmd("diff-close", effect="diff closed", outline="whole", cps=200, after=0.2)

        # 7. Clear.
        s.say("One command cleans up. Your files are never written.")
        t.cmd("clear", effect="everything removed", outline="whole", cps=200, after=1.4)

        # 8. Outro.
        v.fade(outro_card(fonts), 0.8)
        v.hold(4.0)
        v.still("outro")
        v.fade(Image.new("RGB", (W, H), (0, 0, 0)), 0.8)
    finally:
        t.close()
        v.close()
    print(f"wrote {a.out} ({v.frames / a.fps:.1f} s)")


if __name__ == "__main__":
    main()
