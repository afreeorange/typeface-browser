#!/usr/bin/env python3
"""Scan a font collection and build the manifest.json the web browser reads.

Walks the collection, reads every readable piece of metadata out of each font
(name table, OS/2, head, post, cmap, GSUB/GPOS, fvar) and *measures* the
outlines for the things the metadata lies about or omits: serif-ness, real
stroke weight, monospacing and width. Fonts are grouped into families, styles
that differ only by file format are collapsed, byte-identical duplicates are
flagged, and the result is written as a single JSON file.

Usage:
    python3 build-manifest.py [-i INPUT] [-o OUTPUT] [-j JOBS] [--limit N]
                              [--no-cache] [--dry-run] [--report-unclassified F]
"""

import argparse
import hashlib
import json
import logging
import multiprocessing
import os
import re
import struct
import sys
import time
from collections import Counter, defaultdict

FONT_EXTS = {".otf", ".ttf", ".ttc", ".otc", ".woff", ".woff2"}
COLLECTION_EXTS = {".ttc", ".otc"}

# downloadable but unreadable: .svg fonts and .eot are never parsed, only
# attached to the style their filename matches
AUX_EXTS = {".svg", ".eot"}

FORMAT_RANK = {
    "woff2": 0, "woff": 1, "otf": 2, "ttf": 3,
    "ttc": 9, "otc": 9, "eot": 9, "svg": 9,
}

# formats @font-face cannot use, so never a preview
UNRENDERABLE = {"ttc", "otc", "eot", "svg"}

# a kit splits one family across Family/TTF, Family/WOFF2, Family/web/webfonts
FORMAT_DIRS = re.compile(
    r"^(ttf|otf|woff|woff2|eot|svg|ttc|otc|web|webfont|webfonts|desktop|app|"
    r"mobile|print|opentype|truetype|postscript|type1)s?$",
    re.I,
)

SKIP_DIRS = {"_scripts", "_software"}

CACHE_NAME = ".manifest-cache.json"
OVERRIDES_NAME = "category-overrides.json"

# scripts/ lives in the repo, the repo lives in the collection
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
COLLECTION_DIR = os.path.dirname(REPO_DIR)

# name table IDs we keep
NAME_IDS = {
    0: "copyright",
    1: "family",
    2: "subfamily",
    3: "uniqueID",
    4: "fullName",
    5: "version",
    6: "psName",
    7: "trademark",
    8: "manufacturer",
    9: "designer",
    10: "description",
    11: "vendorURL",
    12: "designerURL",
    13: "license",
    14: "licenseURL",
    16: "typoFamily",
    17: "typoSubfamily",
    21: "wwsFamily",
    22: "wwsSubfamily",
}

# ---------------------------------------------------------------- name table


def _decode(data, platform_id, encoding_id):
    if platform_id == 3 or platform_id == 0:
        try:
            return data.decode("utf-16-be")
        except UnicodeDecodeError:
            return None
    if platform_id == 1 and encoding_id == 0:
        try:
            return data.decode("mac-roman")
        except (UnicodeDecodeError, LookupError):
            return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_names_pure(path):
    """Minimal sfnt parser -> {field: string}. Returns {} on failure."""
    best = {}
    with open(path, "rb") as fh:
        head = fh.read(4)
        if head == b"ttcf":
            fh.seek(12)
            (off,) = struct.unpack(">I", fh.read(4))
            fh.seek(off)
            head = fh.read(4)
        if head not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
            return {}
        (num_tables,) = struct.unpack(">H", fh.read(2))
        fh.read(6)
        name_off = None
        for _ in range(num_tables):
            rec = fh.read(16)
            if len(rec) < 16:
                return {}
            tag, _csum, off, _length = struct.unpack(">4sIII", rec)
            if tag == b"name":
                name_off = off
                break
        if name_off is None:
            return {}
        fh.seek(name_off)
        _fmt, count, string_off = struct.unpack(">HHH", fh.read(6))
        records = [struct.unpack(">HHHHHH", fh.read(12)) for _ in range(count)]
        for plat, enc, _lang, name_id, length, offset in records:
            if name_id not in NAME_IDS:
                continue
            fh.seek(name_off + string_off + offset)
            text = _decode(fh.read(length), plat, enc)
            if not text:
                continue
            prev = best.get(name_id)
            # prefer Windows (platform 3) records over Mac ones
            if prev is None or (plat == 3 and not prev[1]):
                best[name_id] = (text, plat == 3)
    return {NAME_IDS[k]: clean(v[0]) for k, v in best.items() if clean(v[0])}


def names_from_ttfont(font):
    table = font.get("name")
    if table is None:
        return {}
    out = {}
    for name_id, field in NAME_IDS.items():
        try:
            value = clean(table.getDebugName(name_id) or "")
        except Exception:
            continue
        if value:
            out[field] = value
    return out


# ------------------------------------------------------------------ helpers

STYLE_WORDS = (
    r"thin|ultralight|ultlt|extralight|light|lt|book|regular|roman|normal|medium|md|"
    r"demi|semibold|semi|bold|bd|heavy|hv|black|blk|extrablack|xblk|ultrabold|ultra|"
    r"extrabold|xbold|italic|ital|it|oblique|obl|o|condensed|cond|cn|narrow|"
    r"extended|ext|ex|wide|smallcaps|sc|caps|alt|frac|display|titling|poster|num"
)


def clean(text):
    text = re.sub(r"[\x00-\x1f]", " ", text or "").strip()
    return re.sub(r"\s+", " ", text).strip(" .-")


def family_from_filename(path):
    """Heuristic fallback: strip trailing style tokens from the stem."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[_\s]+$", "", stem)
    base = re.split(r"[-_]", stem, 1)[0] or stem
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", base)
    while len(words) > 1 and re.fullmatch(STYLE_WORDS, words[-1], re.I):
        words.pop()
    return clean(" ".join(words)) or clean(stem)


def is_font(name):
    return os.path.splitext(name)[1].lower() in FONT_EXTS


def family_dir(path):
    """Directory a family groups under, with format subfolders folded away.

    Iterative because kits nest them -- FontAwesome ships web/webfonts and
    desktop/otfs. The file's own path is never rewritten, only the grouping.
    """
    parts = os.path.dirname(path).replace(os.sep, "/").split("/")
    while len(parts) > 1 and FORMAT_DIRS.match(parts[-1]):
        parts.pop()
    return "/".join(parts)


# trailing noise a web kit adds but a desktop file doesn't. Stripped
# repeatedly: "averta-regular-webfont" has to reach "averta" to meet
# "Averta Regular" coming the other way.
NOISE_TOKEN = re.compile(r"[-_ ]?(webfont|web|regular)$", re.I)


def norm_key(text):
    """Filename or PostScript name reduced to something two kits agree on."""
    text = (text or "").lower()
    while True:
        stripped = NOISE_TOKEN.sub("", text)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"[^a-z0-9]", "", text)


def style_keys(rec):
    """Every name under which this file might meet the same face in another
    format.

    Neither the filename nor the PostScript name is enough alone. Web kits
    rename the file -- Averta ships "Averta Black Italic.ttf" beside
    "averta-blackitalic-webfont.woff2" -- while also shipping a junk psName
    ("\x7f" in Averta's WOFF2, "." in every Diatype file). Other builds do the
    reverse and keep a good psName under a renamed file: "CalibreWeb-Bold.woff"
    is psName "Calibre-Bold".
    """
    keys = {norm_key(rec["stem"])}
    ps = norm_key(rec.get("psName"))
    # a psName carrying only the style ("Thin", "Light Italic" -- five of
    # Averta's TTFs) names no family and would merge unrelated faces
    style_only = norm_key(rec.get("typoSubfamily") or rec.get("subfamily"))
    if len(ps) >= 6 and ps != style_only:
        keys.add(ps)
    return {k for k in keys if k}


def iter_font_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(files):
            if name.startswith("._") or name.startswith("."):
                continue
            if is_font(name):
                yield os.path.join(dirpath, name)


def iter_aux_files(root):
    """.svg/.eot siblings: path and size only, no parsing, no cache."""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(files):
            if name.startswith("._") or name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in AUX_EXTS:
                yield os.path.join(dirpath, name)


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------- geometry
#
# fontTools gives us contours, not pixels. Flatten every curve into a polyline,
# then measure the glyph's horizontal extent at a given height by intersecting
# the polygon edges with that scanline. Comparing the extent near the top and
# bottom of an "I" against its extent at the middle separates serif from sans
# cleanly: sans faces score 1.00, Times 1.25, Baskerville 1.85, Courier 4.05.

CURVE_STEPS = 8


def _cubic(p0, p1, p2, p3):
    out = []
    for i in range(1, CURVE_STEPS + 1):
        t = i / CURVE_STEPS
        m = 1 - t
        out.append(
            (
                m**3 * p0[0]
                + 3 * m * m * t * p1[0]
                + 3 * m * t * t * p2[0]
                + t**3 * p3[0],
                m**3 * p0[1]
                + 3 * m * m * t * p1[1]
                + 3 * m * t * t * p2[1]
                + t**3 * p3[1],
            )
        )
    return out


def _quad(p0, p1, p2):
    out = []
    for i in range(1, CURVE_STEPS + 1):
        t = i / CURVE_STEPS
        m = 1 - t
        out.append(
            (
                m * m * p0[0] + 2 * m * t * p1[0] + t * t * p2[0],
                m * m * p0[1] + 2 * m * t * p1[1] + t * t * p2[1],
            )
        )
    return out


def flatten_contours(recording):
    """RecordingPen -> [[(x, y), ...], ...], one closed polyline per contour."""
    contours = []
    current = []
    cur = (0, 0)
    for op, args in recording.value:
        if op == "moveTo":
            if len(current) > 2:
                contours.append(current)
            cur = args[0]
            current = [cur]
        elif op == "lineTo":
            cur = args[0]
            current.append(cur)
        elif op == "curveTo":
            current += _cubic(cur, args[0], args[1], args[2])
            cur = args[2]
        elif op == "qCurveTo":
            pts = list(args)
            if pts[-1] is None:  # closed all-offcurve contour
                pts[-1] = pts[0]
            for i in range(len(pts) - 1):
                if i + 1 == len(pts) - 1:
                    end = pts[i + 1]
                else:  # implied on-curve point between controls
                    end = (
                        (pts[i][0] + pts[i + 1][0]) / 2,
                        (pts[i][1] + pts[i + 1][1]) / 2,
                    )
                current += _quad(cur, pts[i], end)
                cur = end
        elif op == "closePath":
            if len(current) > 2:
                contours.append(current)
            current = []
    if len(current) > 2:
        contours.append(current)
    return contours


def width_at(contours, y):
    xs = []
    for contour in contours:
        for i in range(len(contour)):
            x0, y0 = contour[i]
            x1, y1 = contour[(i + 1) % len(contour)]
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append(x0 + (x1 - x0) * (y - y0) / (y1 - y0))
    return (max(xs) - min(xs)) if len(xs) > 1 else None


def _band(contours, y0, height, lo, hi, samples=7):
    values = []
    for i in range(samples):
        t = lo + (hi - lo) * i / (samples - 1)
        w = width_at(contours, y0 + t * height)
        if w:
            values.append(w)
    return sum(values) / len(values) if values else None


def measure_glyph(glyph_set, cmap, chars):
    from fontTools.pens.recordingPen import RecordingPen

    for ch in chars:
        name = cmap.get(ord(ch))
        if not name or name not in glyph_set:
            continue
        pen = RecordingPen()
        glyph_set[name].draw(pen)
        contours = flatten_contours(pen)
        if contours:
            return ch, contours
    return None, None


def measure(font, cmap, upm):
    """-> {serifRatio, stemRatio, widthRatio} (any key may be absent)."""
    out = {}
    try:
        glyph_set = font.getGlyphSet()
    except Exception:
        return out

    ch, contours = measure_glyph(glyph_set, cmap, "IlH")
    if contours:
        ys = [p[1] for c in contours for p in c]
        y0, y1 = min(ys), max(ys)
        height = y1 - y0
        if height > 0:
            mid = _band(contours, y0, height, 0.40, 0.60)
            ends = [
                _band(contours, y0, height, 0.02, 0.10),
                _band(contours, y0, height, 0.90, 0.98),
            ]
            ends = [e for e in ends if e]
            if mid and ends:
                out["serifRatio"] = round(max(ends) / mid, 3)
                out["stemRatio"] = round(mid / height, 4)
            out["serifGlyph"] = ch

    # width: advance of H against its own cap height
    name = cmap.get(ord("H")) or cmap.get(ord("I"))
    if name and name in glyph_set:
        try:
            adv = glyph_set[name].width
            _c, contours = measure_glyph(glyph_set, cmap, "H")
            if adv and contours:
                ys = [p[1] for c in contours for p in c]
                cap = max(ys) - min(ys)
                if cap > 0:
                    out["widthRatio"] = round(adv / cap, 3)
        except Exception:
            pass
    return out


# ------------------------------------------------------------ cmap coverage

COVERAGE_RANGES = [
    ("latin", 0x0041, 0x007A, 20),
    ("latin-ext", 0x00C0, 0x024F, 20),
    ("greek", 0x0370, 0x03FF, 15),
    ("cyrillic", 0x0400, 0x04FF, 20),
    ("hebrew", 0x0590, 0x05FF, 15),
    ("arabic", 0x0600, 0x06FF, 20),
    ("thai", 0x0E00, 0x0E7F, 20),
    ("kana", 0x3040, 0x30FF, 40),
    ("cjk", 0x4E00, 0x9FFF, 100),
    ("symbols", 0x2000, 0x26FF, 20),
    ("dingbats", 0x2700, 0x27BF, 10),
]


def coverage_of(codepoints):
    out = []
    for name, lo, hi, need in COVERAGE_RANGES:
        n = sum(1 for cp in codepoints if lo <= cp <= hi)
        if n >= need:
            out.append(name)
    return out


# --------------------------------------------------------------- extraction


def feature_tags(font):
    tags = set()
    for tag in ("GSUB", "GPOS"):
        try:
            table = font.get(tag)
            if table is None:
                continue
            feature_list = table.table.FeatureList
            if feature_list is None:
                continue
            for record in feature_list.FeatureRecord:
                tags.add(record.FeatureTag)
        except Exception:
            continue
    return sorted(tags)


def extract(path_and_root):
    """Worker: one font file -> flat record dict. Never raises."""
    path, root = path_and_root
    rel = os.path.relpath(path, root)
    try:
        stat = os.stat(path)
    except OSError as exc:
        return {"path": rel, "error": str(exc)}

    rec = {
        "path": rel,
        "format": os.path.splitext(path)[1].lower().lstrip("."),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "stem": os.path.splitext(os.path.basename(path))[0],
        "error": None,
    }
    try:
        rec["sha1"] = sha1_of(path)
    except OSError as exc:
        return {**rec, "error": str(exc)}

    from fontTools.ttLib import TTFont

    font = None
    try:
        font = TTFont(path, lazy=True, fontNumber=0)
        rec.update(names_from_ttfont(font))

        os2 = font.get("OS/2")
        if os2 is not None:
            rec["weightClass"] = int(getattr(os2, "usWeightClass", 0) or 0)
            rec["widthClass"] = int(getattr(os2, "usWidthClass", 0) or 0)
            rec["fsSelection"] = int(getattr(os2, "fsSelection", 0) or 0)
            rec["fsType"] = int(getattr(os2, "fsType", 0) or 0)
            rec["vendorID"] = clean(str(getattr(os2, "achVendID", "") or ""))
            rec["familyClass"] = int(getattr(os2, "sFamilyClass", 0) or 0) >> 8
            panose = getattr(os2, "panose", None)
            if panose is not None:
                rec["panose"] = [
                    int(getattr(panose, attr, 0) or 0)
                    for attr in (
                        "bFamilyType",
                        "bSerifStyle",
                        "bWeight",
                        "bProportion",
                        "bContrast",
                        "bStrokeVariation",
                        "bArmStyle",
                        "bLetterForm",
                        "bMidline",
                        "bXHeight",
                    )
                ]
            if getattr(os2, "version", 0) >= 2:
                rec["capHeight"] = int(getattr(os2, "sCapHeight", 0) or 0)
                rec["xHeight"] = int(getattr(os2, "sxHeight", 0) or 0)

        head = font.get("head")
        if head is not None:
            rec["upm"] = int(head.unitsPerEm)
            rec["macStyle"] = int(head.macStyle)
            for field in ("created", "modified"):
                value = getattr(head, field, None)
                if isinstance(value, (int, float)) and value > 0:
                    # sfnt epoch is 1904-01-01
                    rec[field] = int(value) - 2082844800

        post = font.get("post")
        if post is not None:
            rec["italicAngle"] = float(getattr(post, "italicAngle", 0) or 0)
            rec["isFixedPitch"] = bool(getattr(post, "isFixedPitch", 0))

        hhea = font.get("hhea")
        if hhea is not None:
            rec["ascender"] = int(hhea.ascender)
            rec["descender"] = int(hhea.descender)
            rec["lineGap"] = int(hhea.lineGap)

        maxp = font.get("maxp")
        if maxp is not None:
            rec["glyphs"] = int(maxp.numGlyphs)

        fvar = font.get("fvar")
        if fvar is not None:
            rec["variable"] = [
                {
                    "tag": a.axisTag,
                    "min": a.minValue,
                    "default": a.defaultValue,
                    "max": a.maxValue,
                }
                for a in fvar.axes
            ]

        rec["features"] = feature_tags(font)

        cmap = {}
        try:
            cmap = font.getBestCmap() or {}
        except Exception:
            pass
        rec["cmapCount"] = len(cmap)
        rec["coverage"] = coverage_of(cmap.keys())

        # monospace: share of glyphs on the most common advance width.
        # post.isFixedPitch is set on only 8 of 400 sampled fonts, and an exact
        # "one width" test misses real mono faces -- ABC Diatype Mono has five
        # widths with 91.6% on the dominant one. Proportional faces sit far
        # below 0.5, so the gap is wide.
        try:
            hmtx = font["hmtx"]
            widths = Counter(hmtx[g][0] for g in font.getGlyphOrder() if hmtx[g][0] > 0)
            total = sum(widths.values())
            if total:
                rec["distinctWidths"] = len(widths)
                rec["monoRatio"] = round(widths.most_common(1)[0][1] / total, 3)
                rec["mono"] = rec["monoRatio"] >= 0.90
        except Exception:
            pass

        if rec["format"] not in COLLECTION_EXTS and cmap:
            rec.update(measure(font, cmap, rec.get("upm", 1000)))

    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
        if not rec.get("family"):
            try:
                rec.update(read_names_pure(path))
            except Exception:
                pass
    finally:
        if font is not None:
            try:
                font.close()
            except Exception:
                pass

    if not rec.get("family"):
        rec["family"] = family_from_filename(path)
        rec["familyGuessed"] = True
    return rec


# --------------------------------------------------------------- derivation

# ordered longest/most-specific first so "extralight" never matches "light"
WEIGHT_TOKENS = [
    (r"extra ?black|ultra ?black|x-?black|xblk", 950),
    (r"extra ?bold|ultra ?bold|x-?bold|xbold|ultrabd", 800),
    (r"extra ?light|ultra ?light|ultlt|ultra ?thin", 200),
    (r"semi ?bold|demi ?bold|semibd|demibd", 600),
    (r"hairline|thin", 100),
    (r"black|heavy|fat|poster|ultra|blk", 900),
    (r"bold|bd", 700),
    (r"demi", 600),
    (r"semi ?light|semilt", 350),
    (r"medium|med|md", 500),
    (r"light|lt", 300),
    (r"book", 400),
    (r"regular|roman|normal|plain|text", 400),
]

WIDTH_TOKENS = [
    (r"ultra ?condensed|ultra ?cond", "ultra-condensed"),
    (r"extra ?condensed|extra ?cond|x-?cond", "extra-condensed"),
    (r"semi ?condensed|semi ?cond", "semi-condensed"),
    (r"compressed|compact", "extra-condensed"),
    (r"condensed|cond|narrow|cn", "condensed"),
    (r"semi ?extended|semi ?ext", "semi-expanded"),
    (r"extra ?extended|ultra ?extended|extra ?expanded", "extra-expanded"),
    (r"extended|expanded|wide|ext", "expanded"),
]

WIDTH_CLASS_NAMES = {
    1: "ultra-condensed",
    2: "extra-condensed",
    3: "condensed",
    4: "semi-condensed",
    5: "normal",
    6: "semi-expanded",
    7: "expanded",
    8: "extra-expanded",
    9: "ultra-expanded",
}

ITALIC_RE = re.compile(r"\b(italic|ital|oblique|obl|kursiv|slanted|it)\b", re.I)

BLACKLETTER_RE = re.compile(
    r"fraktur|textura?\b|schwabacher|black ?letter|old ?english|gebrochene|"
    r"cloister ?black|goudy ?text|rotunda|gotisch|deutsche ?schrift|"
    r"canterbury|engraver'?s? ?old",
    re.I,
)

SCRIPT_RE = re.compile(
    r"\bscript\b|handwrit|\bhand\b|brush|calligraph|signature|casual|"
    r"cursive|swash|chancery|\bpen\b|marker|sharpie|graffiti|scrawl|"
    r"scribble|autograph|\bwedding\b",
    re.I,
)

DISPLAY_RE = re.compile(
    r"\bdisplay\b|\bposter\b|titling|\bdeco\b|art ?deco|inline|shadow|stencil|"
    r"outline|\bcaps\b|engraved|western|circus|\bfunky\b|\bcomic\b|\bhorror\b|"
    r"\bgrunge\b|distress|\bretro\b|\bneon\b|\bpixel\b|\bbitmap\b|3-?d\b",
    re.I,
)

MONO_RE = re.compile(r"\bmono\b|monospac|\bcode\b|typewriter|courier|\bconsole\b", re.I)

SLAB_RE = re.compile(r"\bslab\b|egyptian|clarendon|rockwell|memphis|serifa", re.I)

SANS_RE = re.compile(r"\bsans\b|grotesk|grotesque|\bgothic\b|neue haas", re.I)

SERIF_RE = re.compile(
    r"\bserif\b|antiqua|garamond|caslon|baskerville|bodoni|"
    r"\btimes\b|\bgaramond\b|didot|jenson|centaur",
    re.I,
)

SYMBOL_RE = re.compile(
    r"dingbat|\bsymbol\b|\bicons?\b|ornament|fleuron|bullets|"
    r"\bwingding|\bwebding|\bpi\b|\bglyphs?\b|arrows",
    re.I,
)

# vendor IDs that name the tool rather than the foundry, so they make useless
# facet values: FontForge, unknown, and the empty placeholders
JUNK_VENDOR = {
    "pfed",
    "ukwn",
    "unkn",
    "unknown",
    "none",
    "----",
    "????",
    "",
    "n/a",
    "macr",
    "pyrs",
    "fog ",
    "ftgh",
    "altsys",
    "alts",
}

SERIF_THRESHOLD = 1.08

# stem width / glyph height of the "I". Text faces stay under ~0.39 even at
# Black; anything heavier is a picture glyph or a poster face.
PICTURE_STEM = 0.42


def style_string(rec):
    """The part of the naming that describes the style, not the family."""
    parts = [rec.get("typoSubfamily") or "", rec.get("subfamily") or ""]
    full = rec.get("fullName") or ""
    family = rec.get("typoFamily") or rec.get("family") or ""
    if full and family and full.lower().startswith(family.lower()):
        parts.append(full[len(family) :])
    stem = rec.get("stem") or ""
    if "-" in stem:
        parts.append(stem.rsplit("-", 1)[1])
    elif family and stem.lower().startswith(re.sub(r"\s+", "", family).lower()):
        parts.append(stem[len(re.sub(r"\s+", "", family)) :])
    text = " ".join(p for p in parts if p)
    # split camelCase so "BoldCondensed" tokenises
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)


def derive_weight(rec, style):
    for pattern, value in WEIGHT_TOKENS:
        if re.search(r"(?<![a-z])(" + pattern + r")(?![a-z])", style, re.I):
            return value, "name"
    cls = rec.get("weightClass") or 0
    if 1 <= cls <= 9:  # some old fonts store 1-9 instead of 100-900
        return cls * 100, "os2"
    if 100 <= cls <= 1000:
        return int(cls), "os2"
    return 400, "default"


def derive_width(rec, style):
    for pattern, value in WIDTH_TOKENS:
        if re.search(r"(?<![a-z])(" + pattern + r")(?![a-z])", style, re.I):
            return value, "name"
    cls = rec.get("widthClass") or 0
    if cls in WIDTH_CLASS_NAMES and cls != 5:
        return WIDTH_CLASS_NAMES[cls], "os2"
    ratio = rec.get("widthRatio")
    if ratio:
        if ratio < 0.78:
            return "condensed", "measured"
        if ratio > 1.20:
            return "expanded", "measured"
    return "normal", "default"


def derive_italic(rec, style):
    if rec.get("fsSelection", 0) & 0x01:
        return True
    if rec.get("macStyle", 0) & 0x02:
        return True
    if abs(rec.get("italicAngle", 0) or 0) >= 4:
        return True
    return bool(ITALIC_RE.search(style))


def derive_category(rec, style, context):
    """-> (category, confidence, source, tags). `context` is family + folders."""
    tags = []
    panose = rec.get("panose") or [0] * 10
    serif_ratio = rec.get("serifRatio")
    text = f"{context} {style}"

    if DISPLAY_RE.search(text):
        tags.append("display")
    if SCRIPT_RE.search(text):
        tags.append("script")
    if (rec.get("stemRatio") or 0) > PICTURE_STEM and "display" not in tags:
        tags.append("display")

    # 1. symbol / dingbat fonts. Ahead of the monospace test because
    #    ornament and pi fonts put every glyph on one advance width and
    #    would otherwise all read as monospaced.
    if panose[0] == 5 or SYMBOL_RE.search(text):
        latin = "latin" in (rec.get("coverage") or [])
        if not latin or panose[0] == 5:
            return (
                "symbol",
                0.7 if panose[0] == 5 else 0.55,
                "panose" if panose[0] == 5 else "name",
                tags,
            )
    if not (rec.get("coverage") or []) and rec.get("cmapCount", 0) > 0:
        return "symbol", 0.5, "geometry", tags

    # 2. blackletter -- no reliable metadata or geometry, names only
    if BLACKLETTER_RE.search(text):
        return "blackletter", 0.75, "name", tags

    # 2. monospace -- measured, essentially never wrong once dingbats are out.
    #    A one-width font whose "I" is a picture rather than a stem is an
    #    ornament set mapped onto the Latin keyboard, not a monospaced face:
    #    real text stems top out near 0.39 even at Black weights, while
    #    picture glyphs run 0.5-1.0.
    stem = rec.get("stemRatio")
    picture = stem is not None and stem > PICTURE_STEM
    if rec.get("mono"):
        if picture:
            return "symbol", 0.7, "geometry", tags
        return "mono", 1.0, "geometry", tags
    if MONO_RE.search(text) and (rec.get("monoRatio") or 0) >= 0.70 and not picture:
        return "mono", 0.8, "name", tags

    # 4. script / handwriting
    if panose[0] == 3:
        return "script", 0.8, "panose", tags
    if SCRIPT_RE.search(text):
        return "script", 0.7, "name", tags
    if (
        (rec.get("italicAngle") or 0)
        and abs(rec["italicAngle"]) > 15
        and serif_ratio is not None
        and serif_ratio < SERIF_THRESHOLD
    ):
        return "script", 0.4, "geometry", tags

    # 5. decorative / display
    if panose[0] == 4:
        return "display", 0.7, "panose", tags
    if DISPLAY_RE.search(text):
        return "display", 0.55, "name", tags

    # 6. serif vs sans -- the measurement, with names as a tiebreak
    if serif_ratio is not None:
        if serif_ratio >= SERIF_THRESHOLD:
            if SLAB_RE.search(text) or panose[1] in (5, 6, 7, 8):
                return "slab", 0.75, "geometry", tags
            return "serif", min(0.95, 0.6 + (serif_ratio - 1.0)), "geometry", tags
        if SERIF_RE.search(text):
            return "serif", 0.5, "name", tags
        return "sans", 0.9, "geometry", tags

    # 7. nothing measurable (expert sets, symbol-only cmaps)
    if SLAB_RE.search(text):
        return "slab", 0.5, "name", tags
    if SERIF_RE.search(text):
        return "serif", 0.5, "name", tags
    if SANS_RE.search(text):
        return "sans", 0.5, "name", tags
    if panose[1] in (2, 3, 4, 5, 6, 7, 8, 9, 10):
        return "serif", 0.45, "panose", tags
    if panose[1] in (11, 12, 13, 14, 15):
        return "sans", 0.45, "panose", tags
    return "unknown", 0.0, "guess", tags


# -------------------------------------------------------------- aggregation


def derive_foundry(rec, family_name, segments):
    """Manufacturer, then designer, then vendor ID, then the folder naming.

    Most of this collection predates anyone filling in name IDs 8 and 9, and
    what is there is often a copyright sentence rather than a foundry, so long
    values are cut at the first comma. The folder fallbacks catch the two
    naming conventions actually used here: "H&F - Acropolis" style family
    folders, and Old OS X Collections/<foundry>/...
    """
    for field in ("manufacturer", "designer"):
        value = clean(rec.get(field) or "")
        if not value or value.lower() in JUNK_VENDOR:
            continue
        if len(value) > 60:
            value = value.split(",")[0].strip()
        if 1 < len(value) <= 60:
            return value

    vendor = clean(rec.get("vendorID") or "")
    if vendor and vendor.lower().strip() not in JUNK_VENDOR:
        return vendor

    for candidate in [family_name] + list(reversed(segments)):
        match = re.match(r"^(.{2,20}?)\s+-\s+\S", candidate or "")
        if match:
            return match.group(1).strip()

    if len(segments) > 1 and segments[0] == "Old OS X Collections":
        return segments[1]
    return ""


def slug(text):
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text or "untitled"


def load_overrides(repo_dir):
    path = os.path.join(repo_dir, OVERRIDES_NAME)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"warning: could not read {OVERRIDES_NAME}: {exc}", file=sys.stderr)
        return {}
    return {
        k.lower(): v
        for k, v in data.items()
        if isinstance(v, str) and not k.startswith("_")
    }


def apply_override(overrides, family_name, directory):
    if not overrides:
        return None
    for key in (
        f"{directory}/{family_name}".lower(),
        family_name.lower(),
        directory.lower(),
    ):
        if key in overrides:
            return overrides[key]
    return None


# `name` identifies the style and `files` always differ, so they stay put;
# every other field a family's styles agree on is hoisted to the family and
# dropped from the styles. The UI reads `style[f] ?? family[f]`.
NEVER_HOIST = {"name", "files", "preview"}


def build_families(records, overrides, aux_paths=()):
    """Group records into families, collapse format duplicates, hoist strings."""
    seen_hashes = {}
    groups = defaultdict(list)

    # .svg/.eot indexed the way styles are, so they can be handed to the family
    # they belong to and matched against its style keys
    aux_by_dir = defaultdict(list)
    for path, size in aux_paths:
        aux_by_dir[family_dir(path)].append((path, size))

    def canonical_rank(rec):
        name = os.path.basename(rec["path"])
        return (1 if re.search(r"\(\d+\)", name) else 0, len(rec["path"]), rec["path"])

    for rec in sorted(records, key=canonical_rank):
        digest = rec.get("sha1")
        if digest:
            first = seen_hashes.setdefault(digest, rec["path"])
            rec["duplicateOf"] = None if first == rec["path"] else first

    for rec in records:
        directory = family_dir(rec["path"])
        family = rec.get("typoFamily") or rec.get("family") or "Unknown"
        groups[(directory, family)].append(rec)

    # Fold naming artefacts. A kit's WOFF build sometimes mangles the name
    # table -- Averta's ships family "Averta Extra", subfamily "☞" -- which
    # splinters a few faces into a family of their own beside the real one.
    # When every face in the smaller group already exists under another name in
    # the same folder, it is that mangling, not a separate family.
    group_keys = {
        gk: {k for rec in recs for k in style_keys(rec)} for gk, recs in groups.items()
    }
    by_directory = defaultdict(list)
    for gk in groups:
        by_directory[gk[0]].append(gk)
    for siblings in by_directory.values():
        if len(siblings) < 2:
            continue
        siblings.sort(key=lambda gk: (-len(groups[gk]), gk[1].lower()))
        for i, small in enumerate(siblings):
            if not groups[small]:
                continue
            for big in siblings[:i]:
                if groups[big] and group_keys[small] <= group_keys[big]:
                    groups[big].extend(groups[small])
                    groups[small] = []
                    break
    groups = {gk: recs for gk, recs in groups.items() if recs}

    # every aux file goes to one style only: the group that owns the most
    # files for that face, so a leftover artefact can't claim a copy too
    aux_owner = {}
    for directory, paths in aux_by_dir.items():
        for path, _size in paths:
            key = norm_key(os.path.splitext(os.path.basename(path))[0])
            best = max(
                (gk for gk in by_directory.get(directory, ()) if gk in groups
                 and key in group_keys[gk]),
                key=lambda gk: (len(groups[gk]), gk[1].lower()),
                default=None,
            )
            if best is not None:
                aux_owner[path] = best

    families = []
    for (directory, family_name), recs in sorted(
        groups.items(), key=lambda kv: kv[0][1].lower()
    ):
        segments = [s for s in directory.split("/") if s and s != "."]
        context = f"{family_name} {' '.join(segments)}"

        # collapse files that are the same style in a different format: union
        # the records that share any key, so a chain of half-matching names
        # still lands in one style
        owner = {}

        def find(i):
            while owner[i] != i:
                owner[i] = owner[owner[i]]
                i = owner[i]
            return i

        first_seen = {}
        for i, rec in enumerate(recs):
            owner[i] = i
            for key in style_keys(rec):
                other = first_seen.setdefault(key, i)
                if other != i:
                    owner[find(i)] = find(other)

        by_style = defaultdict(list)
        for i, rec in enumerate(recs):
            by_style[find(i)].append(rec)
        keys_of = defaultdict(set)
        for key, i in first_seen.items():
            keys_of[find(i)].add(key)

        styles = []
        styles_by_key = {}
        for key, group in by_style.items():
            group.sort(key=lambda r: FORMAT_RANK.get(r["format"], 5))
            head = group[0]
            style = style_string(head)
            weight, weight_source = derive_weight(head, style)
            width, width_source = derive_width(head, style)
            italic = derive_italic(head, style)
            category, confidence, source, tags = derive_category(head, style, context)

            display = clean(
                head.get("typoSubfamily") or head.get("subfamily") or style or "Regular"
            )
            files = []
            for rec in group:
                entry = {
                    "path": rec["path"],
                    "format": rec["format"],
                    "size": rec["size"],
                }
                if rec.get("duplicateOf"):
                    entry["duplicateOf"] = rec["duplicateOf"]
                files.append(entry)

            preview = next(
                (f["path"] for f in files if f["format"] not in UNRENDERABLE), None
            )

            entry = {
                "name": display,
                "weight": weight,
                "weightSource": weight_source,
                "weightClass": head.get("weightClass"),
                "width": width,
                "widthSource": width_source,
                "italic": italic,
                "slant": round(head.get("italicAngle", 0) or 0, 1),
                "mono": bool(head.get("mono")),
                "category": category,
                "categoryConfidence": round(confidence, 2),
                "categorySource": source,
                "files": files,
                "glyphs": head.get("glyphs"),
                "upm": head.get("upm"),
                "features": head.get("features") or [],
                "coverage": head.get("coverage") or [],
                "cmapCount": head.get("cmapCount"),
                # [serifRatio, stemRatio, widthRatio, monoRatio];
                # null where unmeasurable
                "signals": [
                    head.get("serifRatio"),
                    head.get("stemRatio"),
                    head.get("widthRatio"),
                    head.get("monoRatio"),
                ],
                "panose": head.get("panose"),
                "fsType": head.get("fsType"),
                "variable": head.get("variable"),
                "created": head.get("created"),
            }
            if tags:
                entry["tags"] = tags
            # omitted when it is simply files[0]; explicit null when the only
            # files are .ttc/.otc, which @font-face cannot use
            if preview is None:
                entry["preview"] = None
            elif not files or preview != files[0]["path"]:
                entry["preview"] = preview
            # [cap, x, ascender, descender, lineGap]
            entry["metrics"] = [
                head.get(k) or 0
                for k in ("capHeight", "xHeight", "ascender", "descender", "lineGap")
            ]
            for field in (
                "psName",
                "fullName",
                "version",
                "copyright",
                "trademark",
                "designer",
                "manufacturer",
                "description",
                "license",
                "licenseURL",
                "vendorURL",
                "designerURL",
                "vendorID",
            ):
                if head.get(field):
                    entry[field] = head[field]
            entry["foundry"] = derive_foundry(head, family_name, segments)
            if head.get("error"):
                entry["error"] = head["error"]
            styles.append(entry)
            for name in keys_of[key]:
                styles_by_key[name] = entry

        # hang .svg/.eot on the style whose filename they share. Anything that
        # matches nothing is dropped -- that is what keeps FontAwesome's 14,808
        # icon SVGs from inventing styles.
        for path, size in aux_by_dir.get(directory, ()):
            if aux_owner.get(path) != (directory, family_name):
                continue
            name = os.path.basename(path)
            style = styles_by_key.get(norm_key(os.path.splitext(name)[0]))
            if style is None:
                continue
            style["files"].append(
                {
                    "path": path,
                    "format": os.path.splitext(name)[1].lower().lstrip("."),
                    "size": size,
                }
            )

        styles.sort(key=lambda s: (s["italic"], s["weight"], s["name"].lower()))

        # family category: the most confident majority verdict among its styles
        votes = Counter()
        for style in styles:
            if style["category"] != "unknown":
                votes[style["category"]] += style["categoryConfidence"]
        category = votes.most_common(1)[0][0] if votes else "unknown"
        confidence = round(
            max(
                (s["categoryConfidence"] for s in styles if s["category"] == category),
                default=0.0,
            ),
            2,
        )
        source = next(
            (s["categorySource"] for s in styles if s["category"] == category), "guess"
        )
        forced = apply_override(overrides, family_name, directory)
        if forced:
            category, confidence, source = forced, 1.0, "override"
            for style in styles:
                style["category"] = forced
                style["categoryConfidence"] = 1.0
                style["categorySource"] = "override"

        family = {
            "id": slug(f"{directory}-{family_name}"),
            "name": family_name,
            "dir": directory,
            "path": segments,
            "category": category,
            "categoryConfidence": confidence,
            "categorySource": source,
            "styles": styles,
            "styleCount": len(styles),
            "fileCount": sum(len(s["files"]) for s in styles),
            "weights": sorted({s["weight"] for s in styles}),
            "widths": sorted({s["width"] for s in styles}),
            "hasItalic": any(s["italic"] for s in styles),
            "hasMono": any(s["mono"] for s in styles),
            "formats": sorted({f["format"] for s in styles for f in s["files"]}),
            "coverage": sorted({c for s in styles for c in s["coverage"]}),
            "features": sorted({f for s in styles for f in s["features"]}),
            "tags": sorted({t for s in styles for t in s.get("tags", [])}),
            "duplicateCount": sum(
                1 for s in styles for f in s["files"] if f.get("duplicateOf")
            ),
        }

        # hoist anything every style agrees on up to the family
        fields = {k for s in styles for k in s} - NEVER_HOIST
        for field in sorted(fields):
            if any(field not in s for s in styles):
                continue
            values = {json.dumps(s[field], sort_keys=True) for s in styles}
            if len(values) != 1:
                continue
            value = styles[0][field]
            if value not in (None, "", [], {}):
                family[field] = value
            for style in styles:
                style.pop(field, None)

        families.append(family)

    return families


def build_tree(families):
    root = {"count": 0, "families": 0, "children": {}}
    for family in families:
        node = root
        node["count"] += family["fileCount"]
        node["families"] += 1
        for segment in family["path"]:
            node = node["children"].setdefault(
                segment, {"count": 0, "families": 0, "children": {}}
            )
            node["count"] += family["fileCount"]
            node["families"] += 1
    return root


def style_field(style, family, field, default=None):
    """Styles drop values identical across the family; fall back to the family."""
    if field in style:
        return style[field]
    return family.get(field, default)


def build_facets(families):
    categories, foundries, features = Counter(), Counter(), Counter()
    coverage, formats, widths, weights = Counter(), Counter(), Counter(), Counter()
    for family in families:
        categories[family["category"]] += 1
        if family.get("foundry"):
            foundries[family["foundry"]] += 1
        for key in family["features"]:
            features[key] += 1
        for key in family["coverage"]:
            coverage[key] += 1
        for key in family["formats"]:
            formats[key] += 1
        for style in family["styles"]:
            widths[style_field(style, family, "width", "normal")] += 1
            weights[style_field(style, family, "weight", 400)] += 1
    return {
        "categories": dict(categories.most_common()),
        "foundries": dict(foundries.most_common()),
        "features": dict(features.most_common()),
        "coverage": dict(coverage.most_common()),
        "formats": dict(formats.most_common()),
        "widths": dict(widths.most_common()),
        "weights": {str(k): v for k, v in sorted(weights.items())},
    }


# --------------------------------------------------------------------- main


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "-i",
        "--input",
        default=COLLECTION_DIR,
        help="font collection root (default: the folder holding this repo)",
    )
    ap.add_argument(
        "-o",
        "--output",
        default=os.path.join(REPO_DIR, "web", "manifest.json"),
        help="manifest path (default: web/manifest.json)",
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        help="worker processes (default: cpu count)",
    )
    ap.add_argument("--limit", type=int, default=0, help="scan only the first N files")
    ap.add_argument("--no-cache", action="store_true", help="ignore the scan cache")
    ap.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="scan and report, but write nothing",
    )
    ap.add_argument(
        "--report-unclassified",
        metavar="FILE",
        help="write low-confidence families to FILE",
    )
    args = ap.parse_args()

    logging.getLogger("fontTools").setLevel(logging.ERROR)

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        sys.exit(f"not a directory: {root}")

    print(f"scanning {root} ...")
    # don't walk this repo when it sits inside the collection
    SKIP_DIRS.add(os.path.basename(REPO_DIR))
    paths = list(iter_font_files(root))
    if args.limit:
        paths = paths[: args.limit]
    print(f"{len(paths)} font file(s) found")

    cache_path = os.path.join(REPO_DIR, CACHE_NAME)
    cache = {} if args.no_cache else load_cache(cache_path)

    fresh, stale = [], []
    for path in paths:
        rel = os.path.relpath(path, root)
        entry = cache.get(rel)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if (
            entry
            and entry.get("size") == stat.st_size
            and entry.get("mtime") == int(stat.st_mtime)
        ):
            fresh.append(entry)
        else:
            stale.append(path)

    print(f"{len(fresh)} from cache, {len(stale)} to parse")

    records = list(fresh)
    if stale:
        jobs = args.jobs or (os.cpu_count() or 4)
        started = time.time()
        payload = [(p, root) for p in stale]
        if jobs > 1 and len(stale) > 50:
            with multiprocessing.Pool(jobs) as pool:
                for i, rec in enumerate(
                    pool.imap_unordered(extract, payload, chunksize=32), 1
                ):
                    records.append(rec)
                    if i % 1000 == 0:
                        print(f"  {i}/{len(stale)} ...", flush=True)
        else:
            for i, item in enumerate(payload, 1):
                records.append(extract(item))
                if i % 1000 == 0:
                    print(f"  {i}/{len(stale)} ...", flush=True)
        print(f"parsed {len(stale)} in {time.time() - started:.1f}s")

    records.sort(key=lambda r: r["path"])

    aux = []
    for path in iter_aux_files(root):
        try:
            aux.append((os.path.relpath(path, root), os.path.getsize(path)))
        except OSError:
            continue

    overrides = load_overrides(REPO_DIR)
    families = build_families(records, overrides, aux)
    tree = build_tree(families)
    facets = build_facets(families)

    attached = sum(
        1
        for f in families
        for style in f["styles"]
        for file in style["files"]
        if file["format"] in ("svg", "eot")
    )

    failed = [r for r in records if r.get("error")]
    guessed = [r for r in records if r.get("familyGuessed")]
    duplicates = sum(1 for r in records if r.get("duplicateOf"))
    low = [f for f in families if f["categoryConfidence"] < 0.5]

    manifest = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": os.path.relpath(root, os.path.dirname(os.path.abspath(args.output))),
        "counts": {
            "families": len(families),
            "styles": sum(f["styleCount"] for f in families),
            "files": len(records) + attached,
            "duplicates": duplicates,
            "failed": len(failed),
        },
        "facets": facets,
        "tree": tree,
        "families": families,
    }

    print(f"\nfiles:      {len(records)}")
    print(f"svg/eot:    {attached} attached of {len(aux)} found")
    print(f"families:   {len(families)}")
    print(f"styles:     {manifest['counts']['styles']}")
    print(f"duplicates: {duplicates}")
    print(f"failed:     {len(failed)}")
    print(f"guessed family name: {len(guessed)}")
    print(
        "categories: " + ", ".join(f"{k}={v}" for k, v in facets["categories"].items())
    )
    print(f"low confidence (<0.5): {len(low)} families")
    for rec in failed[:10]:
        print(f"  ! {rec['path']}: {rec['error']}", file=sys.stderr)

    if args.report_unclassified:
        with open(args.report_unclassified, "w", encoding="utf-8") as fh:
            for family in sorted(low, key=lambda f: f["dir"]):
                fh.write(
                    f"{family['dir']}/{family['name']}\t"
                    f"{family['category']}\t{family['categoryConfidence']}\n"
                )
        print(f"unclassified list -> {args.report_unclassified}")

    if args.dry_run:
        print("\ndry run - nothing written.")
        return

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(args.output) / 1e6
    print(f"\nwrote {args.output} ({size:.1f} MB)")

    if not args.no_cache:
        new_cache = {r["path"]: r for r in records if not r.get("error")}
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(new_cache, fh, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
