#!/usr/bin/env python3
"""Render the README screenshot and GIF of a real nvtour session, without a terminal or display.

A private `nvim --embed` is attached as a UI (ext_linegrid). The demo tour runs through the real
CLI on its socket. The screen grid is drawn with Pillow after each step.

    python3 scripts/render_demo.py            # writes docs/screenshot.png and docs/tour.gif
    python3 scripts/render_demo.py --font-dir /usr/share/fonts/truetype/dejavu

Needs pynvim and Pillow. Fonts: JetBrains Mono (Nerd Font Mono) when found, else DejaVu Sans Mono.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pynvim
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
DEMO = "docs/demo/cache.cpp"

FONT_SETS = [
    ("JetBrainsMonoNerdFontMono-{}.ttf", {"regular": "Regular", "bold": "Bold", "italic": "Italic",
                                          "bold_italic": "BoldItalic"}),
    ("JetBrainsMono-{}.ttf", {"regular": "Regular", "bold": "Bold", "italic": "Italic", "bold_italic": "BoldItalic"}),
    ("DejaVuSansMono{}.ttf", {"regular": "", "bold": "-Bold", "italic": "-Oblique", "bold_italic": "-BoldOblique"}),
]
FONT_DIRS = [Path.home() / ".local/share/fonts", Path("/usr/share/fonts/truetype/jetbrains-mono"),
             Path("/usr/share/fonts/truetype/dejavu")]


def find_fonts(dirs: list[Path]) -> dict[str, Path]:
    for pattern, styles in FONT_SETS:
        for d in dirs:
            paths = {k: d / pattern.format(v) for k, v in styles.items()}
            if all(p.exists() for p in paths.values()):
                return paths
    sys.exit("no usable monospace font found; pass --font-dir")


class Screen:
    """The UI grid, kept up to date from ext_linegrid redraw events."""

    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        self.cells = [[(" ", 0)] * cols for _ in range(rows)]
        self.attrs: dict[int, dict] = {0: {}}
        self.fg, self.bg, self.sp = 0xFFFFFF, 0x000000, 0xFF0000

    def apply(self, events: list) -> None:
        for name, *calls in events:
            for args in calls:
                getattr(self, "ev_" + name, lambda *a: None)(*args)

    def ev_grid_resize(self, grid, cols, rows):
        self.cols, self.rows = cols, rows
        self.cells = [[(" ", 0)] * cols for _ in range(rows)]

    def ev_default_colors_set(self, fg, bg, sp, *_):
        self.fg, self.bg, self.sp = fg, bg, sp

    def ev_hl_attr_define(self, hl_id, rgb, *_):
        self.attrs[hl_id] = rgb

    def ev_grid_clear(self, grid):
        self.cells = [[(" ", 0)] * self.cols for _ in range(self.rows)]

    def ev_grid_line(self, grid, row, col, cells, *_):
        hl = 0
        for cell in cells:
            text = cell[0]
            if len(cell) > 1:
                hl = cell[1]
            for _ in range(cell[2] if len(cell) > 2 else 1):
                if col < self.cols:
                    self.cells[row][col] = (text, hl)
                col += 1

    def ev_grid_scroll(self, grid, top, bot, left, right, rows, _cols):
        src = [r[:] for r in self.cells]
        for r in range(top, bot):
            s = r + rows
            if top <= s < bot:
                self.cells[r][left:right] = src[s][left:right]


def rgb(c: int) -> tuple[int, int, int]:
    return (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF


def draw(screen: Screen, fonts: dict[str, ImageFont.FreeTypeFont], pad: int) -> Image.Image:
    font = fonts["regular"]
    cw = round(font.getlength("M"))
    asc, desc = font.getmetrics()
    ch = asc + desc + 2
    img = Image.new("RGB", (screen.cols * cw + 2 * pad, screen.rows * ch + 2 * pad), rgb(screen.bg))
    d = ImageDraw.Draw(img)
    for r, line in enumerate(screen.cells):
        for c, (text, hl) in enumerate(line):
            a = screen.attrs.get(hl, {})
            fg, bg = a.get("foreground", screen.fg), a.get("background", screen.bg)
            if a.get("reverse"):
                fg, bg = bg, fg
            x, y = pad + c * cw, pad + r * ch
            wide = text != "" and c + 1 < screen.cols and line[c + 1][0] == ""
            if bg != screen.bg:
                d.rectangle([x, y, x + cw * (2 if wide else 1) - 1, y + ch - 1], fill=rgb(bg))
            if text.strip():
                style = "bold_italic" if a.get("bold") and a.get("italic") else (
                    "bold" if a.get("bold") else ("italic" if a.get("italic") else "regular"))
                d.text((x, y + 1), text, font=fonts[style], fill=rgb(fg))
            if a.get("underline") or a.get("undercurl"):
                uy = y + asc + 3
                d.line([x, uy, x + cw - 1, uy], fill=rgb(a.get("special", fg)), width=max(1, cw // 9))
    return img


class Demo:
    def __init__(self, cols: int, rows: int, setup_lua: str = "", file: str = DEMO):
        self.tmp = tempfile.TemporaryDirectory(prefix="nvtour-demo-")
        self.sock = os.path.join(self.tmp.name, "nvim.sock")
        env = {**os.environ, "XDG_RUNTIME_DIR": self.tmp.name}
        env.pop("NVIM", None)
        self.env = {**env, "NVTOUR_RUNTIME_ONLY": "1", "PYTHONPATH": str(REPO)}
        os.chdir(REPO)
        self.nv = pynvim.attach("child", argv=["nvim", "--embed", "--headless", "--clean", "-n",
                                               "--listen", self.sock])
        self.screen = Screen(cols, rows)
        self.nv.ui_attach(cols, rows, rgb=True, ext_linegrid=True)
        self.nv.exec_lua("""
            vim.o.number = true
            vim.o.signcolumn = "yes:1"
            vim.o.showmode = false
            vim.o.shortmess = vim.o.shortmess .. "I"
            vim.g.nvtour_flash = 0
        """)
        if setup_lua:
            self.nv.exec_lua(setup_lua)
        self.nv.command("edit " + file)

    def cli(self, *args: str, stdin: str | None = None) -> None:
        cmd = [sys.executable, "-m", "nvtour.cli", "--socket", self.sock, *args]
        r = subprocess.run(cmd, env=self.env, input=stdin, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            sys.exit(f"{' '.join(args)}: exit {r.returncode}\n{r.stderr}")

    def snapshot(self) -> Screen:
        """Redraw and apply every pending redraw event; a notification marks the end."""
        self.nv.command("redraw!")
        self.nv.exec_lua("vim.rpcnotify(...,'demo_done')", self.nv.channel_id)
        while True:
            kind, method, args = self.nv.next_message()
            if kind == "notification" and method == "redraw":
                self.screen.apply(args)
            elif kind == "notification" and method == "demo_done":
                return self.screen

    def close(self) -> None:
        self.nv.close()
        self.tmp.cleanup()


TOUR = [
    ("step", f"{DEMO}:20", "--role", "fault", "--label", "dangling iterator", "--expect", "it->second",
     "--note", "`it` is used after `refresh()` moved the node out of `entries` and back in. "
               "The iterator is invalid, so this reads freed memory."),
    ("step", f"{DEMO}:52-54", "--role", "flow", "--label", "extract + insert",
     "--via", "`get()` calls `refresh()` while it holds `it`", "--note", "`extract()` unlinks the node. Iterators to it are invalidated, "
               "and `insert()` does **not** make them valid again."),
    ("step", f"{DEMO}:19", "--role", "fix", "--label", "update in place",
     "--via", "back in `get()`, where `refresh()` is called", "--note", "Change the expiry in place: `it->second.expires_at += ttl;`. "
               "Then `it` stays valid and `refresh()` can go."),
]
SUMMARY = """**Cause**: `get()` keeps `it` across `refresh()`, which re-inserts the node.

**Fix**: update `expires_at` through `it` instead of `extract()` + `insert()`.
"""


def main() -> None:
    p = argparse.ArgumentParser(description="Render the README screenshot and GIF of a real nvtour session.")
    p.add_argument("--out", default=str(REPO / "docs"), help="output directory")
    p.add_argument("--font-dir", action="append", type=Path, help="directory with the .ttf files")
    p.add_argument("--cols", type=int, default=124)
    p.add_argument("--rows", type=int, default=36)
    p.add_argument("--scale", type=int, default=2, help="pixel scale of the PNG (the GIF uses 1)")
    a = p.parse_args()
    paths = find_fonts(a.font_dir or FONT_DIRS)

    def load(size: int) -> dict[str, ImageFont.FreeTypeFont]:
        return {k: ImageFont.truetype(str(v), size) for k, v in paths.items()}

    demo = Demo(a.cols, a.rows)
    try:
        demo.cli("start", "Dangling iterator in Cache::get")
        for step in TOUR:
            demo.cli(*step)
        demo.cli("panel", "-", stdin=SUMMARY)
        frames = []
        for n in range(1, len(TOUR) + 1):
            demo.cli("goto", str(n))
            screen = demo.snapshot()
            if n == 1:
                draw(screen, load(15 * a.scale), 12 * a.scale).save(Path(a.out) / "screenshot.png", optimize=True)
            frames.append(draw(screen, load(15), 12))
        frames = [f.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
        frames[0].save(Path(a.out) / "tour.gif", save_all=True, append_images=frames[1:], duration=3000, loop=0,
                       optimize=True)
    finally:
        demo.close()
    print(f"wrote {a.out}/screenshot.png and {a.out}/tour.gif")


if __name__ == "__main__":
    main()
