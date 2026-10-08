"""
Google Fonts metadata helpers shared by the catalog tools (ADR 125 Am. 2).

Sources (all public, no API key):
  * a sparse checkout of github.com/google/fonts — METADATA.pb, the licence
    texts (OFL.txt / LICENSE.txt / UFL.txt) and tags/all/families.csv;
  * https://fonts.google.com/metadata/fonts — the site's family list
    (popularity / trending ranks, classifications, per-style variants).

The licence id is DERIVED from the family's METADATA.pb `license` field (and
cross-checked against the licence file that exists in the family directory) —
never defaulted. The Reserved Font Name(s) come from the copyright header of
the licence text (the lines above "This Font Software is licensed under").
"""

import csv
import json
import pathlib
import re

ALLOWED_LICENSES = {"OFL", "APACHE2", "UFL"}           # METADATA.pb spelling
LICENSE_IDS = {"OFL": "OFL-1.1", "APACHE2": "Apache-2.0", "UFL": "UFL-1.0"}  # SPDX
LICENSE_DIRS = {"ofl": ("OFL.txt", "OFL"), "apache": ("LICENSE.txt", "APACHE2"),
                "ufl": ("UFL.txt", "UFL")}


# ── text-proto (METADATA.pb) ────────────────────────────────────────────────
_TOKEN = re.compile(r'\s*(?:(#[^\n]*)|("(?:[^"\\]|\\.)*")|([{}:])|([^\s{}:"#]+))', re.S)


def _tokens(text):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        comment, string, punct, atom = m.groups()
        if comment:
            continue
        if string is not None:
            yield ("str", bytes(string[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace"))
        elif punct:
            yield ("p", punct)
        elif atom:
            yield ("atom", atom)


def parse_textproto(text):
    """Minimal text-proto reader → dict of lists (every field may repeat)."""
    toks = list(_tokens(text))
    i = 0

    def block():
        nonlocal i
        out = {}
        while i < len(toks):
            kind, val = toks[i]
            if kind == "p" and val == "}":
                i += 1
                return out
            key = val
            i += 1
            if toks[i] == ("p", ":"):
                i += 1
            if toks[i] == ("p", "{"):
                i += 1
                value = block()
            else:
                kind, raw = toks[i]
                i += 1
                if kind == "str":
                    value = raw
                    while i < len(toks) and toks[i][0] == "str":  # adjacent string concat
                        value += toks[i][1]
                        i += 1
                else:
                    try:
                        value = float(raw) if "." in raw else int(raw)
                    except ValueError:
                        value = {"true": True, "false": False}.get(raw, raw)
            out.setdefault(key, []).append(value)
        return out

    return block()


def first(d, key, default=None):
    v = d.get(key)
    return v[0] if v else default


# ── licence text ────────────────────────────────────────────────────────────
_RFN = re.compile(r'Reserved\s+Font\s+Names?\s*(.*)', re.I | re.S)


def licence_header(text):
    """The copyright block above the licence body (OFL) — where RFNs live."""
    cut = re.search(r"This Font Software is licensed under", text)
    return text[: cut.start()] if cut else ""


def reserved_font_names(licence_text):
    """Reserved Font Names declared in an OFL copyright header (may be [])."""
    names = []
    for line in re.split(r"(?<=[.\n])\s*(?=Copyright)", licence_header(licence_text)):
        m = _RFN.search(line)
        if not m:
            continue
        tail = m.group(1)
        quoted = re.findall(r'["“”\'‘’]([^"“”\'‘’]+)["“”\'‘’]', tail)
        if quoted:
            names.extend(q.strip() for q in quoted if q.strip())
        else:  # unquoted: up to the end of the sentence
            names.append(re.split(r"[.\n]", tail.strip())[0].strip(" ,"))
    seen, out = set(), []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def licence_kind_from_text(text):
    """The licence a licence TEXT is (OFL / APACHE2 / UFL), or None."""
    head = text[:4000]
    if re.search(r"SIL\s+OPEN\s+FONT\s+LICEN[CS]E", head, re.I):
        return "OFL"
    if re.search(r"Apache\s+License", head, re.I):
        return "APACHE2"
    if re.search(r"UBUNTU\s+FONT\s+LICEN[CS]E", head, re.I):
        return "UFL"
    return None


# Words too generic to carry a reserved name on their own.
_GENERIC = {"sans", "serif", "mono", "code", "pro", "text", "display", "one", "two", "slab", "sc",
            "condensed", "expanded", "semiexpanded", "the", "of", "and", "font", "fonts", "family",
            "math", "tm", "std", "neue", "new", "unicase", "rounded", "variable", "vf"}


def _words(s):
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


def rfn_binds(family, reserved):
    """True when a declared Reserved Font Name reaches THIS family's name, i.e.
    a modified derivative of it could not keep the name. Conservative: any
    distinctive (non-generic) word of a reserved name appearing in the family
    name binds ("Bitter Pro" binds "Bitter"; "RevReading Lexend" binds
    "Lexend Deca"), while a borrowed upstream's RFN ("Josefin Sans" inside
    Reem Kufi's header, "Source" inside Assistant's) does not."""
    fam = set(_words(family))
    for name in reserved:
        if set(_words(name)) & fam - _GENERIC:
            return True
    return False


def must_rename_if_modified(family, license_kind, reserved):
    """The policy flag the catalog ships as `rfn`: a modified derivative of
    this family (a subset, an instance, overlap removal) may not keep its
    name. OFL → a binding Reserved Font Name; UFL → always (UFL §2: Modified
    Versions must be renamed); Apache-2.0 → never."""
    if license_kind == "UFL":
        return True
    if license_kind == "OFL":
        return rfn_binds(family, reserved)
    return False


# ── the checkout ────────────────────────────────────────────────────────────
class GoogleFontsRepo:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self._by_name = None

    def families(self):
        """{family name → record} for every METADATA.pb under ofl/apache/ufl."""
        if self._by_name is None:
            self._by_name = {}
            for d, (lic_file, lic_kind) in LICENSE_DIRS.items():
                for meta_path in sorted((self.root / d).glob("*/METADATA.pb")):
                    fam_dir = meta_path.parent
                    meta = parse_textproto(meta_path.read_text("utf-8"))
                    name = first(meta, "name")
                    if not name:
                        continue
                    lic_path = fam_dir / lic_file
                    lic_text = lic_path.read_text("utf-8", "replace") if lic_path.exists() else None
                    rec = {
                        "name": name,
                        "dir": f"{d}/{fam_dir.name}",
                        "meta": meta,
                        "license": first(meta, "license"),      # OFL / APACHE2 / UFL
                        "licenseDirKind": lic_kind,             # what the directory says
                        "licensePath": str(lic_path) if lic_text is not None else None,
                        "licenseText": lic_text,
                        "rfn": reserved_font_names(lic_text) if (lic_text and d == "ofl") else [],
                    }
                    # A family can appear twice only by repo error; keep the first.
                    self._by_name.setdefault(name, rec)
        return self._by_name


def load_site_metadata(path):
    raw = pathlib.Path(path).read_text("utf-8").lstrip(")]}'\n")
    meta = json.loads(raw)
    return {f["family"]: f for f in meta["familyMetadataList"]}


def load_tags(path):
    """families.csv → {family: {axisSpec: {tag: score}}} ('' = whole family)."""
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) < 4 or not row[0] or row[0].startswith("#"):
                continue
            family, spec, tag, score = row[0], row[1], row[2], row[3]
            try:
                score = float(score)
            except ValueError:
                continue
            out.setdefault(family, {}).setdefault(spec, {})[tag] = score
    return out


def quality_score(tags_for_family):
    """Mean of the four /Quality/* tags (family-level rows), or None."""
    vals = [v for spec in tags_for_family.values() for t, v in spec.items() if t.startswith("/Quality/")]
    return sum(vals) / len(vals) if vals else None
