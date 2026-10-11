"""Draw the memo's business diagrams as PNGs, locally with matplotlib.

Nothing leaves the machine: deal data is never sent to a hosted renderer. Which diagrams
exist (and their thresholds) is decided here; render.py embeds the files and slack_app.py
posts them so they show inline in the thread."""

from __future__ import annotations

import io
import textwrap
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

from .models import MemoState  # noqa: E402

# Reference palette (dataviz skill): ink for text, slot 1 for the company, slot 2 for others.
SURFACE, INK, INK_2, MUTED, GRID = "#ffffff", "#0b0b0b", "#52514e", "#8a8984", "#e4e3df"
COMPANY, OTHER = "#2a78d6", "#eb6834"

plt.rcParams.update({
    "text.parse_math": False,          # "$12M ... $4M" must not become mathtext
    "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK_2,
    "xtick.color": INK_2, "ytick.color": INK_2, "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
})


@dataclass(frozen=True)
class Chart:
    section: str      # memo section the chart is drawn in
    filename: str
    title: str
    caption: str


CHARTS = [
    Chart("snapshot", "funding.png", "Funding history", "Funding rounds"),
    Chart("product", "business_model.png", "Business model", "Business model: who pays or supplies whom"),
    Chart("competitors", "competitors.png", "Competitive positioning",
          "Competitive positioning, inferred from the claims above"),
    Chart("financials", "revenue.png", "Revenue trend", "Revenue by period"),
]


def available(memo: MemoState) -> list[Chart]:
    """Charts with enough cited data whose section is in the memo."""
    d = memo.diagrams
    enough = {"snapshot": len(d.funding) >= 1, "product": len(d.business_model) >= 2,
              "competitors": len(d.quadrant.points) >= 2, "financials": len(d.revenue.points) >= 2}
    return [c for c in CHARTS if enough[c.section] and c.section in memo.sections]


def evidence_ids(memo: MemoState, section: str) -> list[str]:
    d = memo.diagrams
    items = {"snapshot": d.funding, "product": d.business_model,
             "competitors": d.quadrant.points, "financials": d.revenue.points}[section]
    return [e for i in items for e in i.evidence_ids]


def render_all(memo: MemoState) -> dict[str, bytes]:
    """filename -> PNG bytes for every available chart."""
    draw = {"snapshot": _funding, "product": _business_model,
            "competitors": _quadrant, "financials": _revenue}
    out = {}
    for c in available(memo):
        fig = draw[c.section](memo)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
        plt.close(fig)
        out[c.filename] = buf.getvalue()
    return out


_PLACEHOLDERS = {"", "-", "n/a", "na", "none", "unknown", "not disclosed", "undisclosed",
                 "not available", "not specified", "not stated", "tbd"}


def _known(text: str) -> str:
    return "" if text.strip().strip(".").lower() in _PLACEHOLDERS else text.strip()


def _short(text: str, width: int = 22, lines: int = 2) -> str:
    return "\n".join(textwrap.wrap(text, width, max_lines=lines, placeholder="…")) or "?"


def _money(value: float, unit: str) -> str:
    """4.8 with unit "$M" -> "$4.8M"; with unit "%" -> "4.8%"; with "customers" -> "4.8 customers"."""
    unit = unit.strip()
    if unit[:1] in "$€£¥" and unit:
        return f"{unit[0]}{value:g}{unit[1:].strip()}"
    return f"{value:g}{unit}" if unit in ("%", "x") else f"{value:g} {unit}".strip()


def _title(ax, text: str) -> None:
    ax.set_title(text, loc="left", color=INK, fontsize=12, fontweight="bold", pad=12)


def _quadrant(memo: MemoState):
    q = memo.diagrams.quadrant
    fig, ax = plt.subplots(figsize=(7, 5.6))
    ax.set_xlim(0, 1), ax.set_ylim(0, 1)
    ax.axhline(0.5, color=GRID, lw=1, zorder=0), ax.axvline(0.5, color=GRID, lw=1, zorder=0)
    ax.set_xticks([]), ax.set_yticks([])
    for side in ax.spines.values():
        side.set_color(GRID)
    company = memo.company.lower()
    mine = [p.name.lower() in company or company in p.name.lower() for p in q.points]
    for p, m in zip(q.points, mine):
        ax.scatter(p.x, p.y, s=110 if m else 70, color=COMPANY if m else OTHER,
                   edgecolor=SURFACE, linewidth=2, zorder=3)
    _place_labels(fig, ax, [(p.name, p.x, p.y, m) for p, m in zip(q.points, mine)])
    end = {"transform": ax.transAxes, "color": INK_2, "fontsize": 9}
    ax.text(0, -0.035, f"← {_short(q.x_low or 'Low', 34, 1)}", ha="left", va="top", **end)
    ax.text(1, -0.035, f"{_short(q.x_high or 'High', 34, 1)} →", ha="right", va="top", **end)
    ax.text(-0.035, 0, f"← {_short(q.y_low or 'Low', 34, 1)}", ha="right", va="bottom", rotation=90, **end)
    ax.text(-0.035, 1, f"{_short(q.y_high or 'High', 34, 1)} →", ha="right", va="top", rotation=90, **end)
    _title(ax, f"{memo.company} vs competitors (inferred)")
    return fig


def _place_labels(fig, ax, points: list[tuple[str, float, float, bool]]) -> None:
    """Put each label beside its dot at the first spot that overlaps no other label or dot."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    to_px = ax.transData.transform
    dots = [to_px((x, y)) for _, x, y, _ in points]
    frame = ax.get_window_extent(renderer)
    taken = []
    for i, (name, x, y, bold) in enumerate(points):
        near = 1 if x <= 0.7 else -1                     # prefer the side with room
        tries = [(near * dx, dy) for dx in (9, 0) for dy in (7, -7)] + \
                [(-near * 9, dy) for dy in (7, -7)] + [(near * 9, dy) for dy in (20, -20, 33, -33)]
        label = ax.annotate(_short(name, 18), (x, y), xytext=(0, 0), textcoords="offset points", color=INK, fontsize=9,
                            fontweight="bold" if bold else "normal", zorder=4)
        best = None
        for dx, dy in tries:
            label.set_position((dx, dy))
            label.set_ha("left" if dx > 0 else "right" if dx < 0 else "center")
            label.set_va("bottom" if dy > 0 else "top")
            box = label.get_window_extent(renderer).expanded(1.06, 1.15)
            clash = any(box.overlaps(t) for t in taken) or not (
                frame.x0 <= box.x0 and box.x1 <= frame.x1 and frame.y0 <= box.y0 and box.y1 <= frame.y1) or any(
                box.x0 - 5 <= ox <= box.x1 + 5 and box.y0 - 5 <= oy <= box.y1 + 5
                for j, (ox, oy) in enumerate(dots) if j != i)
            if best is None:
                best = (dx, dy, box)
            if not clash:
                best = (dx, dy, box)
                break
        dx, dy, box = best
        label.set_position((dx, dy))
        label.set_ha("left" if dx > 0 else "right" if dx < 0 else "center")
        label.set_va("bottom" if dy > 0 else "top")
        taken.append(box)


def _revenue(memo: MemoState):
    r = memo.diagrams.revenue
    fig, ax = plt.subplots(figsize=(7, 4))
    xs = [p.period for p in r.points]
    vals = [p.value for p in r.points]
    bars = ax.bar(xs, vals, color=COMPANY, width=0.55, zorder=2)
    for b, v in zip(bars, vals):
        ax.annotate(_money(v, r.unit), (b.get_x() + b.get_width() / 2, v), xytext=(0, 4), textcoords="offset points",
                    ha="center", color=INK, fontsize=9)
    ax.grid(axis="y", color=GRID, lw=0.8, zorder=0)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.set_ylabel(r.unit or r.metric)
    ax.set_ylim(0, max(vals) * 1.15 if max(vals) > 0 else 1)
    _title(ax, f"{r.metric} by period" + (f" ({r.unit})" if r.unit else ""))
    return fig


def _funding(memo: MemoState):
    rounds = memo.diagrams.funding
    fig, ax = plt.subplots(figsize=(max(5, 2.3 * len(rounds) + 1), 2.1))
    xs = list(range(len(rounds)))
    ax.plot([-0.5, len(rounds) - 0.5], [0, 0], color=GRID, lw=2, zorder=1)
    ax.scatter(xs, [0] * len(xs), s=80, color=COMPANY, edgecolor=SURFACE, linewidth=2, zorder=3)
    for x, f in zip(xs, rounds):
        ax.annotate(f.date, (x, 0), xytext=(0, -14), textcoords="offset points",
                    ha="center", va="top", color=MUTED, fontsize=9)
        lines = [(f"led by {_short(_known(f.lead), 24, 1)}" if _known(f.lead) else "", INK_2, 8.5, "normal"),
                 (_short(_known(f.amount), 24, 1), INK, 9.5, "normal"),
                 (_short(f.round, 22, 1), INK, 10, "bold")]
        y_off = 12
        for text, color, size, weight in lines:  # stacked upward from the line
            if text:
                ax.annotate(text, (x, 0), xytext=(0, y_off), textcoords="offset points", ha="center",
                            va="bottom", color=color, fontsize=size, fontweight=weight)
                y_off += 14
    ax.set_xlim(-0.6, len(rounds) - 0.4), ax.set_ylim(-0.3, 0.55)
    ax.axis("off")
    _title(ax, "Funding history")
    return fig


def _business_model(memo: MemoState):
    """Hub layout around the company: who pays or supplies it on the left, who it pays or
    serves on the right. Two-way flows get a pair of offset arrows."""
    edges = [(e.source.strip(), e.target.strip(), e.label) for e in memo.diagrams.business_model]
    nodes = list(dict.fromkeys(n for a, b, _ in edges for n in (a, b)))
    company = memo.company.lower()
    mine = [n for n in nodes if n.lower() in company or company in n.lower()]
    hub = mine[0] if mine else max(nodes, key=lambda n: sum(n in (a, b) for a, b, _ in edges))
    into = {a for a, b, _ in edges if b == hub}
    out = {b for a, b, _ in edges if a == hub}
    left = [n for n in nodes if n != hub and n in into and n not in out]
    right = [n for n in nodes if n != hub and n not in left]
    gap, step = 5.0, 1.5
    pos = {hub: (gap, 0.0)}
    for x, col in ((0.0, left), (2 * gap, right)):
        pos.update({n: (x, ((len(col) - 1) / 2 - i) * step) for i, n in enumerate(col)})

    bw, bh = 2.5, 0.8
    rows = max(len(left), len(right), 1)
    x_min = (0.0 if left else gap) - bw / 2 - 0.4
    x_max = (2 * gap if right else gap) + bw / 2 + 0.4
    fig, ax = plt.subplots(figsize=(0.78 * (x_max - x_min), max(2.4, 1.35 * rows + 1.0)))
    pairs = {(a, b) for a, b, _ in edges}

    # Spread the hub's attachment points along each side so arrows don't meet in one spot.
    def lane(a: str, b: str) -> float:
        two_way = (b, a) in pairs
        return (0.12 if (a, b) < (b, a) else -0.12) if two_way else 0.0

    anchor: dict[tuple[str, str], float] = {}
    for side in (left, right):
        spokes = sorted(((a, b) for a, b, _ in edges if hub in (a, b) and ({a, b} - {hub}) <= set(side)),
                        key=lambda e: -(pos[e[0] if e[1] == hub else e[1]][1] + lane(*e)))
        for i, e in enumerate(spokes):
            anchor[e] = (bh * 0.36) * (1 - 2 * i / (len(spokes) - 1)) if len(spokes) > 1 else 0.0

    floor = min(p[1] for p in pos.values()) - 0.75
    for a, b, label in edges:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        dy = lane(a, b)
        arrow = {"arrowstyle": "-|>", "mutation_scale": 14, "color": MUTED, "lw": 1.4, "zorder": 1}
        if hub not in (a, b) and abs(x1 - x2) > gap:
            # Partner to partner across the company: arc underneath it instead of through it.
            start, end = (x1, y1 - bh / 2), (x2, y2 - bh / 2)
            rad = 0.32 if x2 > x1 else -0.32
            ax.add_patch(FancyArrowPatch(start, end, connectionstyle=f"arc3,rad={rad}", **arrow))
            mx = (start[0] + end[0]) / 2
            my = (start[1] + end[1]) / 2 - abs(rad) * abs(x2 - x1) / 2
            floor = min(floor, my - 0.35)
            dy = -1  # label below the arc
        elif x1 != x2:
            d = 1 if x2 > x1 else -1
            ya = anchor.get((a, b), dy) if a == hub else y1 + dy
            yb = anchor.get((a, b), dy) if b == hub else y2 + dy
            start, end = (x1 + d * bw / 2, ya), (x2 - d * bw / 2, yb)
            ax.add_patch(FancyArrowPatch(start, end, **arrow))
            mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
        else:  # same column: arc out to the side
            d = -1 if x1 < gap else 1
            start, end = (x1 + d * bw / 2, y1), (x2 + d * bw / 2, y2)
            ax.add_patch(FancyArrowPatch(start, end, connectionstyle=f"arc3,rad={0.5 * d * (1 if y2 < y1 else -1)}",
                                         **arrow))
            mx, my = (start[0] + end[0]) / 2 + d * 0.55, (start[1] + end[1]) / 2
        if label:
            below = dy < 0
            ax.text(mx, my + (-0.06 if below else 0.06), _short(label, 20, 2), ha="center",
                    va="top" if below else "bottom", fontsize=8, color=INK_2,
                    bbox={"boxstyle": "round,pad=0.15", "fc": SURFACE, "ec": "none", "alpha": 0.9}, zorder=2)
    for n, (x, y) in pos.items():
        is_hub = n == hub
        ax.add_patch(FancyBboxPatch((x - bw / 2, y - bh / 2), bw, bh, boxstyle="round,pad=0.02,rounding_size=0.1",
                                    fc=COMPANY if is_hub else SURFACE, ec=COMPANY if is_hub else MUTED,
                                    lw=1.2, zorder=3))
        ax.text(x, y, _short(n, 20), ha="center", va="center", fontsize=8.5, zorder=4,
                color=SURFACE if is_hub else INK, fontweight="bold" if is_hub else "normal")
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(x_min, x_max)
    ax.set_ylim(floor, max(ys) + 0.75)
    ax.set_aspect("equal"), ax.axis("off")
    _title(ax, "Business model")
    return fig
