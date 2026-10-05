#!/usr/bin/env python3
"""Render every role at several range lengths, on a dark and a light background.

Each cell of the grid is a crop of a real nvim screen with that step as the current step. The steps
are in nvtour/cli.py.

    python3 scripts/render_roles.py       # writes docs/roles-dark.png and docs/roles-light.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from render_demo import FONT_DIRS, REPO, Demo, Screen, draw, find_fonts

FILE = "nvtour/cli.py"
ROLES = {"fault": "DiagnosticError", "flow": "DiagnosticInfo", "fix": "DiagnosticOk", "context": "DiagnosticWarn",
         "info": "DiagnosticHint"}
LENGTHS = [1, 3, 12]
CROP_COLS = 70


def plan_steps() -> list[tuple[str, int, int, int]]:
    """(role, length, l1, l2) for each cell; each range starts on a non-blank line, far from the others."""
    lines = (REPO / FILE).read_text().splitlines()
    steps, l1 = [], 20
    for role in ROLES:
        for length in LENGTHS:
            while not lines[l1 - 1].strip():
                l1 += 1
            steps.append((role, length, l1, l1 + length - 1))
            l1 += 36
    assert l1 < len(lines), "the file is too short for the grid"
    return steps


def crop(screen: Screen, top: int, bottom: int, cols: int) -> Screen:
    out = Screen(cols, bottom - top)
    out.attrs, out.fg, out.bg, out.sp = screen.attrs, screen.fg, screen.bg, screen.sp
    out.cells = [row[:cols] for row in screen.cells[top:bottom]]
    return out


def render(background: str, fonts: dict, out: Path) -> None:
    steps = plan_steps()
    demo = Demo(110, 40, setup_lua=f"""
        vim.o.background = "{background}"
        vim.o.wrap = false
        vim.g.nvtour_auto_panel = false
    """, file=FILE)
    cells: dict[tuple[str, int], Image.Image] = {}
    try:
        demo.cli("start", "Roles")
        for role, length, l1, l2 in steps:
            label = f"{role} · {length} line{'s' if length > 1 else ''}"
            demo.cli("step", f"{FILE}:{l1}-{l2}", "--role", role, "--label", label, "--no-jump",
                     "--note", f"The accent comes from `{ROLES[role]}`.")
        for n, (role, length, l1, l2) in enumerate(steps, 1):
            demo.cli("goto", str(n))
            screen = demo.snapshot()
            # Screen rows (0-based) of the note above l1 and of l2; one line of context on each side.
            row1, row2, fill = demo.nv.exec_lua("""
                local win, l1, l2 = vim.api.nvim_get_current_win(), ...
                local fill = vim.api.nvim_win_text_height(win, { start_row = l1 - 1, end_row = l1 - 1 }).fill
                return { vim.fn.screenpos(win, l1, 1).row - 1, vim.fn.screenpos(win, l2, 1).row - 1, fill }
            """, l1, l2)
            top, bottom = max(1, row1 - fill - 1), min(screen.rows - 2, row2 + 2)
            cells[(role, length)] = draw(crop(screen, top, bottom, CROP_COLS), fonts, 8)
    finally:
        demo.close()

    font = fonts["bold"]
    bg = cells[("fault", 1)].getpixel((0, 0))
    fg = (230, 230, 230) if background == "dark" else (40, 40, 40)
    head_w, head_h, gap = 110, 34, 10
    col_w = max(im.width for im in cells.values())
    row_h = {role: max(cells[(role, n)].height for n in LENGTHS) for role in ROLES}
    img = Image.new("RGB", (head_w + len(LENGTHS) * (col_w + gap), head_h + sum(row_h.values()) + gap * len(ROLES)), bg)
    d = ImageDraw.Draw(img)
    for i, n in enumerate(LENGTHS):
        d.text((head_w + i * (col_w + gap) + 8, 8), f"{n} line{'s' if n > 1 else ''}", font=font, fill=fg)
    y = head_h
    for role in ROLES:
        d.text((10, y + 10), role, font=font, fill=fg)
        for i, n in enumerate(LENGTHS):
            img.paste(cells[(role, n)], (head_w + i * (col_w + gap), y))
        y += row_h[role] + gap
        d.line([(0, y - gap // 2), (img.width, y - gap // 2)], fill=tuple(int(c * 0.5 + b * 0.5) for c, b in zip(fg, bg)))
    img.save(out, optimize=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Render every role at several range lengths.")
    p.add_argument("--out", default=str(REPO / "docs"), help="output directory")
    p.add_argument("--font-dir", action="append", type=Path, help="directory with the .ttf files")
    a = p.parse_args()
    paths = find_fonts(a.font_dir or FONT_DIRS)
    fonts = {k: ImageFont.truetype(str(v), 15) for k, v in paths.items()}
    for background in ("dark", "light"):
        render(background, fonts, Path(a.out) / f"roles-{background}.png")
        print(f"wrote {a.out}/roles-{background}.png")


if __name__ == "__main__":
    main()
