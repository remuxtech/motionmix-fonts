"""
Outline metrics for the font index (ADR 125 Am. 2) — measured from glyph
OUTLINES with fontTools, never from OS/2 header fields (11 of the first 150
families had a missing or >0.08-off sxHeight / sCapHeight).

All lengths are in em (font units / unitsPerEm) unless noted. The fill rule
is non-zero winding, so overlapping contours (common in static instances of
variable fonts) measure as one shape — and are counted (`overlap`).

A variable font is measured at a design location (a named instance's
coordinates, ADR 125 Am. 3) through fontTools' location-aware glyph set — in
memory only; advances come from the varied glyphs (phantom points).
"""

import unicodedata

import numpy as np
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont

SPECIMEN = "Hamburgefonstiv"
OVERLAP_SAMPLE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# Representative code points per Google Fonts subset (assigned ones only),
# for the measured coverage fraction. Ranges are inclusive.
SUBSET_RANGES = {
    "latin": [(0x20, 0x7E), (0xC0, 0xFF)],
    "latin-ext": [(0x100, 0x17F)],
    "vietnamese": [(0x1EA0, 0x1EF9), (0x102, 0x103), (0x110, 0x111), (0x1A0, 0x1A1), (0x1AF, 0x1B0)],
    "cyrillic": [(0x410, 0x44F), (0x401, 0x401), (0x451, 0x451)],
    "cyrillic-ext": [(0x460, 0x52F)],
    "greek": [(0x391, 0x3A9), (0x3B1, 0x3C9)],
    "greek-ext": [(0x1F00, 0x1FFE)],
    "arabic": [(0x621, 0x64A)],
    "hebrew": [(0x5D0, 0x5EA)],
    "devanagari": [(0x900, 0x97F)],
    "bengali": [(0x980, 0x9FF)],
    "gurmukhi": [(0xA00, 0xA7F)],
    "gujarati": [(0xA80, 0xAFF)],
    "oriya": [(0xB00, 0xB7F)],
    "tamil": [(0xB80, 0xBFF)],
    "telugu": [(0xC00, 0xC7F)],
    "kannada": [(0xC80, 0xCFF)],
    "malayalam": [(0xD00, 0xD7F)],
    "sinhala": [(0xD80, 0xDFF)],
    "thai": [(0xE01, 0xE5B)],
    "lao": [(0xE80, 0xEFF)],
    "tibetan": [(0xF00, 0xFFF)],
    "myanmar": [(0x1000, 0x109F)],
    "georgian": [(0x10A0, 0x10FF)],
    "armenian": [(0x531, 0x58F)],
    "khmer": [(0x1780, 0x17FF)],
    "ethiopic": [(0x1200, 0x137F)],
    "korean": [(0xAC00, 0xD7A3)],
    "japanese": [(0x3041, 0x3096), (0x30A1, 0x30FA)],
    "chinese-simplified": [(0x4E00, 0x9FA5)],
    "chinese-traditional": [(0x4E00, 0x9FA5)],
    "chinese-hongkong": [(0x4E00, 0x9FA5)],
}
_SUBSET_CPS = {}


def _subset_cps(name):
    if name not in _SUBSET_CPS:
        cps = [cp for lo, hi in SUBSET_RANGES[name] for cp in range(lo, hi + 1)
               if unicodedata.category(chr(cp)) != "Cn"]
        _SUBSET_CPS[name] = cps
    return _SUBSET_CPS[name]


class _FlattenPen(BasePen):
    """Outline → closed polygons (curves sampled)."""

    def __init__(self, glyph_set, steps=10):
        super().__init__(glyph_set)
        self.steps = steps
        self.contours = []
        self._cur = []

    def _moveTo(self, p):
        self._flush()
        self._cur = [p]

    def _lineTo(self, p):
        self._cur.append(p)

    def _curveToOne(self, p1, p2, p3):
        (x0, y0) = self._cur[-1]
        for k in range(1, self.steps + 1):
            t = k / self.steps
            mt = 1 - t
            self._cur.append((mt ** 3 * x0 + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t ** 3 * p3[0],
                              mt ** 3 * y0 + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t ** 3 * p3[1]))

    def _qCurveToOne(self, p1, p2):
        (x0, y0) = self._cur[-1]
        for k in range(1, self.steps + 1):
            t = k / self.steps
            mt = 1 - t
            self._cur.append((mt * mt * x0 + 2 * mt * t * p1[0] + t * t * p2[0],
                              mt * mt * y0 + 2 * mt * t * p1[1] + t * t * p2[1]))

    def _closePath(self):
        self._flush()

    def _endPath(self):
        self._flush()

    def _flush(self):
        if len(self._cur) > 2:
            self.contours.append(self._cur)
        self._cur = []


class Glyph:
    def __init__(self, contours, advance):
        self.advance = advance
        segs = []
        for c in contours:
            pts = np.asarray(c, dtype=float)
            nxt = np.roll(pts, -1, axis=0)
            segs.append(np.hstack([pts, nxt]))
        self.edges = np.vstack(segs) if segs else np.zeros((0, 4))
        if len(self.edges):
            self.xmin, self.xmax = self.edges[:, [0, 2]].min(), self.edges[:, [0, 2]].max()
            self.ymin, self.ymax = self.edges[:, [1, 3]].min(), self.edges[:, [1, 3]].max()
        else:
            self.xmin = self.xmax = self.ymin = self.ymax = 0.0

    @property
    def empty(self):
        return not len(self.edges)

    def _scan(self, c, vertical):
        e = self.edges
        if vertical:   # a vertical line x = c: swap axes
            a0, b0, a1, b1 = e[:, 1], e[:, 0], e[:, 3], e[:, 2]
        else:
            a0, b0, a1, b1 = e[:, 0], e[:, 1], e[:, 2], e[:, 3]
        up = (b0 <= c) & (c < b1)
        down = (b1 <= c) & (c < b0)
        hit = up | down
        if not hit.any():
            return [], 0
        t = (c - b0[hit]) / (b1[hit] - b0[hit])
        pos = a0[hit] + t * (a1[hit] - a0[hit])
        d = np.where(up[hit], 1, -1)
        order = np.argsort(pos, kind="stable")
        pos, d = pos[order], d[order]
        wind = np.cumsum(d)
        runs, start, peak = [], None, int(np.abs(wind).max())
        prev = 0
        for p, w in zip(pos, wind):
            if prev == 0 and w != 0:
                start = p
            elif prev != 0 and w == 0 and start is not None:
                runs.append((start, p))
                start = None
            prev = w
        return runs, peak

    def runs_h(self, y):
        """Ink intervals [(x0, x1)] along y, and the peak |winding|."""
        return self._scan(y, False)

    def runs_v(self, x):
        return self._scan(x, True)


class Face:
    def __init__(self, path, location=None):
        self.font = TTFont(path, lazy=True)
        self.upm = self.font["head"].unitsPerEm
        self.cmap = self.font.getBestCmap() or {}
        self.location = None
        if location and "fvar" in self.font:
            self.location = {a.axisTag: a.defaultValue for a in self.font["fvar"].axes}
            self.location.update({k: float(v) for k, v in location.items()})
        self.glyph_set = self.font.getGlyphSet(location=self.location)
        self.hmtx = self.font["hmtx"]
        self._cache = {}

    def advance(self, name):
        if self.location is None:
            return self.hmtx[name][0]
        return self.glyph_set[name].width

    def glyph(self, ch):
        name = self.cmap.get(ord(ch))
        if name is None:
            return None
        if name not in self._cache:
            pen = _FlattenPen(self.glyph_set)
            try:
                self.glyph_set[name].draw(pen)
            except Exception:  # noqa: BLE001 — a broken glyph measures as absent
                return None
            self._cache[name] = Glyph(pen.contours, self.advance(name))
        return self._cache[name]

    def first(self, chars):
        for ch in chars:
            g = self.glyph(ch)
            if g is not None and not g.empty:
                return g
        return None

    def close(self):
        self.font.close()


def _r(x, nd=3):
    return None if x is None else round(float(x), nd)


def measure(path, subsets=(), location=None):
    """→ dict of index metrics for one font file (see INDEX.md), at `location`
    ({axis: value}) for a variable font."""
    f = Face(path, location)
    upm = float(f.upm)
    out = {"upm": f.upm, "glyphs": f.font["maxp"].numGlyphs}

    gx = f.first("xzvw")
    gH = f.first("HIEL")
    cap = gH.ymax / upm if gH else None
    xh = gx.ymax / upm if gx else None
    asc_g = [g for g in (f.glyph(c) for c in "dhklb") if g is not None and not g.empty]
    desc_g = [g for g in (f.glyph(c) for c in "pqgyj") if g is not None and not g.empty]
    out["cap"] = _r(cap)
    out["xh"] = _r(xh)
    out["xr"] = _r(xh / cap) if xh and cap else None
    out["asc"] = _r(max(g.ymax for g in asc_g) / upm) if asc_g else None
    out["desc"] = _r(min(g.ymin for g in desc_g) / upm) if desc_g else None

    # caseless: no true lowercase (caps-only / small caps) — 'p' has no
    # descender, or the x-height reaches the cap height.
    gp = f.glyph("p")
    no_desc = gp is not None and not gp.empty and gp.ymin > -0.05 * upm
    out["caseless"] = bool(no_desc or (out["xr"] is not None and out["xr"] >= 0.9))

    # width: mean advance of the specimen, in em and relative to cap height
    adv = [f.glyph(c).advance for c in SPECIMEN if f.glyph(c) is not None]
    if len(adv) == len(SPECIMEN):
        wd = sum(adv) / len(adv) / upm
        out["wd"] = _r(wd)
        out["wdc"] = _r(wd / cap) if cap else None
    else:
        out["wd"] = out["wdc"] = None

    # stem: the vertical stems of H (cut at 30% cap height), else I / l
    stem = None
    if gH is not None:
        runs, _ = gH.runs_h(gH.ymax * 0.3)
        if len(runs) >= 2:
            stem = (runs[0][1] - runs[0][0] + runs[-1][1] - runs[-1][0]) / 2
        elif len(runs) == 1:
            stem = runs[0][1] - runs[0][0]
    if stem is None:
        g = f.first("Il")
        if g is not None:
            runs, _ = g.runs_h((g.ymin + g.ymax) / 2)
            if runs:
                stem = max(b - a for a, b in runs)
    out["stem"] = _r(stem / upm) if stem else None
    out["stemc"] = _r(stem / upm / cap) if stem and cap else None

    # contrast: thinnest horizontal stroke / thickest vertical stroke of 'o'
    # (1 = monoline; a Didone is ~0.1–0.3)
    go = f.first("oO")
    contrast = None
    if go is not None:
        cy = (go.ymin + go.ymax) / 2
        cx = (go.xmin + go.xmax) / 2
        hr, _ = go.runs_h(cy)
        vr, _ = go.runs_v(cx)
        if len(hr) >= 2 and len(vr) >= 2:
            thick = max(b - a for a, b in (hr[0], hr[-1]))
            thin = min(b - a for a, b in (vr[0], vr[-1]))
            if thick > 0:
                contrast = min(1.0, thin / thick)
    out["contrast"] = _r(contrast)

    # ink: inked area of the specimen / (its advance × cap height) — the
    # typographic colour (weight) of a line of text at a fixed cap height
    if out["wd"] is not None and cap:
        area = 0.0
        for ch in SPECIMEN:
            g = f.glyph(ch)
            if g is None or g.empty:
                continue
            ys = np.linspace(g.ymin, g.ymax, 34)[1:-1]
            dy = (g.ymax - g.ymin) / 33
            area += sum(sum(b - a for a, b in g.runs_h(y)[0]) for y in ys) * dy
        out["ink"] = _r(area / (sum(adv) * cap * upm))
    else:
        out["ink"] = None

    # overlap: share of A–Z a–z 0–9 with overlapping contours (|winding| ≥ 2)
    hits = total = 0
    for ch in OVERLAP_SAMPLE:
        g = f.glyph(ch)
        if g is None or g.empty:
            continue
        total += 1
        for y in np.linspace(g.ymin, g.ymax, 9)[1:-1]:
            if g.runs_h(y)[1] >= 2:
                hits += 1
                break
    out["overlap"] = _r(hits / total, 2) if total else None

    cov = {}
    for s in subsets:
        if s in SUBSET_RANGES:
            cps = _subset_cps(s)
            cov[s] = _r(sum(1 for cp in cps if cp in f.cmap) / len(cps), 2)
    out["cov"] = cov
    f.close()
    return out
