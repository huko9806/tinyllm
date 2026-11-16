#!/usr/bin/env python3
"""Draws the charts for the README as SVG files next to this script.

bench.svg comes from the /bench results in the README (BENCH below), thermal.svg from thermal-run.csv.
The style follows logo.svg and screenshot.svg: a dark terminal panel that looks the same in both GitHub themes.
"""
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent

# /bench on 2026-11-09, Raspberry Pi 3B, 4 cores, heatsink, closed case: name, load s, tokens/s, RAM MB
BENCH = [
    ("TinyStories 28.9M int4", 0, 203, 18),
    ("SmolLM2 135M Q4_K_M", 6, 15.7, 179),
    ("granite 4.0 H 350M Q4_K_M", 12, 5.6, 323),
    ("LFM2 700M Q4_K_M", 23, 4.1, 508),
    ("Qwen3 0.6B Q4_K_M", 20, 4.5, 664),
]
PI_RAM = 904          # MB available to the system
# thermal-run.csv: LFM2 700M Q4_K_M on 2026-11-16, prompt "Write a very long and detailed story about a robot.",
# temperature 0.7, top-k 40, sampled every 5 s. The answer itself: 1024 tokens (max_tokens) in 336 s.
THERMAL_TOKENS, THERMAL_SECONDS = 1024, 336
READING = 5           # tokens/s: a reader at 250 words per minute, about 1.3 tokens per word

SURFACE, BORDER, GRID = "#11151c", "#2a3140", "#1b2130"
INK, INK2, MUTED = "#d0d0d0", "#a0a4ab", "#7d838d"
BAR = "#3987e5"       # one series, one hue; passes the dark-surface checks
LINE2 = "#d95926"     # clock in the thermal chart; each panel holds one series under its own title
SPEED = "#199e70"     # tokens per second in the thermal chart
FONT = "font-family=\"system-ui, -apple-system, 'Segoe UI', sans-serif\""
MONO = "font-family=\"ui-monospace, 'DejaVu Sans Mono', Menlo, Consolas, monospace\""


def panel(width, height, title):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{title}">',
        f"<title>{title}</title>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="{SURFACE}" stroke="{BORDER}"/>',
        f'<path d="M0.5 34H{width - 0.5}" stroke="{BORDER}"/>',
        '<circle cx="22" cy="17" r="6" fill="#ff5f57"/><circle cx="42" cy="17" r="6" fill="#febc2e"/>'
        '<circle cx="62" cy="17" r="6" fill="#28c840"/>',
        f'<text x="84" y="21.5" {MONO} font-size="13" fill="{MUTED}">{title}</text>',
    ]


def text(x, y, s, size=12, fill=INK, anchor="start", weight="normal", mono=False):
    return (f'<text x="{x}" y="{y}" {MONO if mono else FONT} font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>')


def bar(x, y, w, h, fill=BAR, r=4):
    """A horizontal bar, square at the baseline (left) and rounded at the data end (right)."""
    if w <= r:
        return f'<rect x="{x}" y="{y}" width="{w:.1f}" height="{h}" fill="{fill}"/>'
    return (f'<path d="M{x} {y}H{x + w - r:.1f}a{r} {r} 0 0 1 {r} {r}V{y + h - r}a{r} {r} 0 0 1 -{r} {r}'
            f'H{x}Z" fill="{fill}"/>')


def bench_chart():
    width, height = 880, 300
    out = panel(width, height, "/bench · Raspberry Pi 3B · 2026-11-09")
    left, row0, step, thick = 200, 78, 36, 18
    panels = [  # title, column index in BENCH, axis max, unit, reference line
        ("Speed, tokens per second", 2, 16, "", (READING, "reading pace")),
        ("RAM while answering, MB", 3, PI_RAM, "", (PI_RAM, "all of the Pi's 904 MB")),
        ("Load time, seconds", 1, 25, "s", None),
    ]
    pw, gap = 190, 40
    for p, (title, col, vmax, unit, ref) in enumerate(panels):
        x0 = left + p * (pw + gap)
        out.append(text(x0, 58, title, 12, INK2))
        scale = pw / vmax
        bottom = row0 + len(BENCH) * step - 10
        out.append(f'<path d="M{x0} {row0 - 8}V{bottom}" stroke="{BORDER}" stroke-width="1"/>')
        if ref:
            value, label = ref
            rx = x0 + value * scale
            out.append(f'<path d="M{rx:.1f} {row0 - 8}V{bottom}" stroke="{MUTED}" stroke-width="1"/>')
            anchor = "end" if value == vmax else "middle"
            out.append(text(rx if anchor == "middle" else rx + 2, bottom + 16, label, 10.5, MUTED, anchor))
        for i, row in enumerate(BENCH):
            y = row0 + i * step
            value = row[col]
            if p == 0:
                out.append(text(left - 12, y + 13, row[0], 12, INK, "end"))
            w = min(value, vmax) * scale
            out.append(bar(x0, y, w, thick))
            if value > vmax:  # off the scale: cut the bar and say so
                cut = x0 + w - 14
                out.append(f'<path d="M{cut} {y - 2}l-5 {thick / 2 + 2}l5 {thick / 2 + 2}" stroke="{SURFACE}" '
                           f'stroke-width="3" fill="none"/>')
                out.append(f'<path d="M{cut + 6} {y - 2}l-5 {thick / 2 + 2}l5 {thick / 2 + 2}" stroke="{SURFACE}" '
                           f'stroke-width="3" fill="none"/>')
            label = f"{value:g}{unit}" if value else "0"
            out.append(text(x0 + w + 6, y + 13, label, 11.5, INK2, mono=True))
    out.append(text(20, height - 16, "Q4_K_M files through llama-server, 4 threads, 2048 context. "
                    "TinyStories runs in its own C program. Peak CPU temperatures 69 to 73 °C.", 11, MUTED))
    out.append("</svg>")
    (HERE / "bench.svg").write_text("\n".join(out) + "\n")


def thermal_chart():
    rows = list(csv.DictReader((HERE / "thermal-run.csv").open()))
    rows = [r for r in rows if not r["seconds"].startswith("#")]
    t = [float(r["seconds"]) for r in rows]
    temp = [float(r["temp_c"]) for r in rows]
    mhz = [int(r["mhz"]) for r in rows]
    tokens = [int(r["tokens"]) for r in rows]
    # Speed per 20-second window, so the line shows what the clock does to the answer
    window = 4  # samples of 5 s
    speed_t, speed = [], []
    for i in range(window, len(t)):
        speed_t.append(t[i])
        speed.append((tokens[i] - tokens[i - window]) / (t[i] - t[i - window]))
    width, height = 880, 440
    out = panel(width, height, "LFM2 700M, one answer of 1024 tokens · Raspberry Pi 3B · 2026-11-16")
    x0, x1 = 70, width - 30
    tmax = max(t)
    sx = (x1 - x0) / tmax

    def plot(y0, y1, times, values, vmin, vmax, ticks, colour, title, unit):
        sy = (y1 - y0) / (vmax - vmin)
        out.append(text(x0, y0 - 10, title, 12, INK2))
        for v in ticks:
            y = y1 - (v - vmin) * sy
            out.append(f'<path d="M{x0} {y:.1f}H{x1}" stroke="{GRID}" stroke-width="1"/>')
            out.append(text(x0 - 8, y + 4, f"{v}{unit}", 10.5, MUTED, "end", mono=True))
        points = " ".join(f"{x0 + ti * sx:.1f},{y1 - (v - vmin) * sy:.1f}" for ti, v in zip(times, values))
        out.append(f'<polyline points="{points}" fill="none" stroke="{colour}" stroke-width="2" '
                   'stroke-linejoin="round" stroke-linecap="round"/>')
        last_x, last_y = x0 + times[-1] * sx, y1 - (values[-1] - vmin) * sy
        out.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="{colour}" stroke="{SURFACE}" stroke-width="2"/>')
        return sy

    # Shaded while the firmware caps the clock (bit 0x2) or throttles (bit 0x4)
    ty0, ty1 = 70, 170
    cy0, cy1 = 215, 275
    vy0, vy1 = 320, 380
    capped = [ti for ti, r in zip(t, rows) if int(r["throttled"], 16) & 0x6]
    if capped:
        a, b = x0 + capped[0] * sx, x0 + capped[-1] * sx
        out.append(f'<rect x="{a:.1f}" y="{ty0 - 14}" width="{max(b - a, 1):.1f}" height="{vy1 - ty0 + 14}" '
                   f'fill="{LINE2}" fill-opacity="0.08"/>')
        out.append(text(x1, ty0 - 10, "shaded: clock capped by the firmware", 10.5, MUTED, "end"))
    # Temperature, top, with the 80 °C line the firmware acts on
    sy = plot(ty0, ty1, t, temp, 50, 90, [50, 70, 80], BAR, "CPU temperature", " °C")
    y80 = ty1 - (80 - 50) * sy
    out.append(f'<path d="M{x0} {y80:.1f}H{x1}" stroke="{MUTED}" stroke-width="1"/>')
    peak = max(temp)
    px = x0 + t[temp.index(peak)] * sx
    out.append(text(min(px, x1 - 40), ty1 - (peak - 50) * sy - 8, f"peak {peak:g} °C", 11, INK2, "middle", mono=True))
    # Clock
    plot(cy0, cy1, t, mhz, 600, 1300, [700, 1000, 1200], LINE2, "CPU clock", " MHz")
    # Speed
    plot(vy0, vy1, speed_t, speed, 0, 5, [0, 2.5, 5], SPEED, "Tokens per second", "")
    # Time axis
    for s in range(0, int(tmax) + 1, 60):
        x = x0 + s * sx
        out.append(f'<path d="M{x:.1f} {vy1}v4" stroke="{MUTED}"/>')
        out.append(text(x, vy1 + 16, f"{s // 60} min", 10.5, MUTED, "middle", mono=True))
    out.append(text(20, height - 16, f"{THERMAL_TOKENS} tokens in {THERMAL_SECONDS / 60:.1f} minutes, "
                    f"{THERMAL_TOKENS / THERMAL_SECONDS:.1f} tokens per second on average. Started at {temp[0]:g} °C "
                    "after a few minutes of idling. 4 threads, heatsink, closed case, no fan.", 11, MUTED))
    out.append("</svg>")
    (HERE / "thermal.svg").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    bench_chart()
    if (HERE / "thermal-run.csv").exists():
        thermal_chart()
