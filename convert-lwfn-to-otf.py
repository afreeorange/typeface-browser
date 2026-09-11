#!/usr/bin/env python3
"""Convert classic Mac LWFN PostScript Type 1 fonts to OTF.

A classic Mac Type 1 family is two kinds of file, both of which keep
everything in the resource fork and leave the data fork empty:

  LWFN  the outlines, as Type 1 charstrings in POST resources
  FFIL  a "suitcase" holding FOND resources (metrics + kerning) and the
        NFNT bitmap screen fonts, which are not needed here

FontForge opens an LWFN directly, so the outlines need no special handling.
Kerning does: FontForge's MergeKern applies one FOND to every face when a
suitcase holds several families, so a six-face suitcase converts with one
face's kern table copied onto all six. The FOND kern tables are therefore
parsed here, matched to a face by PostScript name, and applied pair by pair.

OTF is the output because it keeps the source's cubic outlines; TTF would
approximate them as quadratics.
"""

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: fontTools not found. Install with: pip install fonttools")
    sys.exit(1)


# Runs under FontForge's own Python. The job is handed over as a JSON file
# rather than argv so paths with quotes or spaces cannot be misread.
WORKER = r"""
import json, sys
import fontforge

job = json.load(open(sys.argv[1]))
font = fontforge.open(job["src"])

pairs = job["kern"]
if pairs:
    # First glyph wins a duplicated codepoint: FontForge lists glyphs in
    # font order, so that is the encoded one rather than an alternate.
    by_uni = {}
    for glyph in font.glyphs():
        if glyph.unicode and glyph.unicode > 0:
            by_uni.setdefault(glyph.unicode, glyph.glyphname)

    font.addLookup("fondkern", "gpos_pair", (),
                   (("kern", (("DFLT", ("dflt",)), ("latn", ("dflt",)))),))
    font.addLookupSubtable("fondkern", "fondkern-1")

    applied = 0
    for uni1, uni2, fixed in pairs:
        left = by_uni.get(uni1)
        right = by_uni.get(uni2)
        if not left or not right:
            continue
        # FOND kern values are 4.12 fixed point fractions of an em.
        value = int(round(fixed / 4096.0 * font.em))
        if value:
            font[left].addPosSub("fondkern-1", right, 0, 0, value, 0, 0, 0, 0, 0)
            applied += 1
    print("KERN_APPLIED %d %d" % (applied, len(pairs)))

font.generate(job["dst"])
"""

APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


def read_resource_fork(path):
    """Return the raw resource fork of a classic Mac file, or None.

    Tried in order: the real fork, an AppleDouble sidecar left by a copy to
    a foreign filesystem, then the data fork itself for files that have been
    flattened at some point. Anything that does not parse as a resource map
    is rejected by the caller.
    """
    candidates = [
        Path(str(path) + "/..namedfork/rsrc"),
        path.parent / ("._" + path.name),
    ]
    for candidate in candidates:
        try:
            blob = candidate.read_bytes()
        except OSError:
            continue
        if not blob:
            continue
        if blob.startswith(APPLEDOUBLE_MAGIC):
            blob = _appledouble_resource(blob)
            if blob is None:
                continue
        return blob
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    return blob or None


def _appledouble_resource(blob):
    """Pull entry ID 2 (the resource fork) out of an AppleDouble file."""
    if len(blob) < 26:
        return None
    count = struct.unpack(">H", blob[24:26])[0]
    for i in range(count):
        base = 26 + i * 12
        if base + 12 > len(blob):
            break
        entry_id, offset, length = struct.unpack(">III", blob[base : base + 12])
        if entry_id == 2:
            return blob[offset : offset + length]
    return None


def iter_resources(blob):
    """Yield (type, id, name, data) for every resource in a resource fork."""
    if len(blob) < 16:
        return
    data_off, map_off, data_len, map_len = struct.unpack(">IIII", blob[:16])
    if map_off + 30 > len(blob):
        return
    type_list = struct.unpack(">H", blob[map_off + 24 : map_off + 26])[0] + map_off
    name_list = struct.unpack(">H", blob[map_off + 26 : map_off + 28])[0] + map_off
    if type_list + 2 > len(blob):
        return
    num_types = struct.unpack(">h", blob[type_list : type_list + 2])[0] + 1

    for i in range(num_types):
        entry = type_list + 2 + i * 8
        if entry + 8 > len(blob):
            return
        res_type = blob[entry : entry + 4].decode("mac-roman")
        count = struct.unpack(">h", blob[entry + 4 : entry + 6])[0] + 1
        ref_list = struct.unpack(">H", blob[entry + 6 : entry + 8])[0] + type_list

        for j in range(count):
            ref = ref_list + j * 12
            if ref + 12 > len(blob):
                return
            res_id = struct.unpack(">h", blob[ref : ref + 2])[0]
            name_off = struct.unpack(">h", blob[ref + 2 : ref + 4])[0]
            name = ""
            if name_off != -1 and name_list + name_off < len(blob):
                length = blob[name_list + name_off]
                start = name_list + name_off + 1
                name = blob[start : start + length].decode("mac-roman")
            # The data offset is a 3-byte field behind a 1-byte attribute.
            body = struct.unpack(">I", b"\x00" + blob[ref + 5 : ref + 8])[0] + data_off
            if body + 4 > len(blob):
                continue
            length = struct.unpack(">I", blob[body : body + 4])[0]
            yield res_type, res_id, name, blob[body + 4 : body + 4 + length]


def parse_fond_kern(data):
    """Return [(unicode1, unicode2, fixed)] from a FOND's kerning table.

    Pair members are Mac character codes, which is what the printer driver
    fed the font, so they are read as MacRoman rather than through the
    font's own /Encoding. Values stay in 4.12 fixed point; the worker
    scales them once it knows the em.
    """
    if len(data) < 52:
        return []
    kern_off = struct.unpack(">i", data[20:24])[0]
    if kern_off <= 0 or kern_off + 2 > len(data):
        return []

    pairs = []
    num_entries = struct.unpack(">h", data[kern_off : kern_off + 2])[0] + 1
    pos = kern_off + 2
    for _ in range(num_entries):
        if pos + 4 > len(data):
            break
        _style, num_pairs = struct.unpack(">hh", data[pos : pos + 4])
        pos += 4
        for _ in range(num_pairs):
            if pos + 4 > len(data):
                break
            code1, code2, value = struct.unpack(">BBh", data[pos : pos + 4])
            pos += 4
            try:
                uni1 = ord(bytes([code1]).decode("mac-roman"))
                uni2 = ord(bytes([code2]).decode("mac-roman"))
            except (UnicodeDecodeError, TypeError):
                continue
            pairs.append((uni1, uni2, value))
    return pairs


def type1_fontname(resources):
    """Read /FontName out of the cleartext header of an LWFN's POST data.

    POST type 1 resources are the ASCII part of the Type 1 font, so the
    name is readable without decrypting anything.
    """
    ascii_parts = [d for t, _, _, d in resources if t == "POST" and d[:1] == b"\x01"]
    if not ascii_parts:
        return None
    header = b"".join(ascii_parts)[:4096].decode("latin-1")
    match = re.search(r"/FontName\s*/([^\s/(){}\[\]]+)", header)
    return match.group(1) if match else None


def normalize(name):
    """Loose key for matching a FOND to a face across naming styles."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def scan(input_dir):
    """Sort every file under input_dir into outline fonts and suitcases."""
    faces, suitcases = [], []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store" or path.name.startswith("._"):
            continue
        blob = read_resource_fork(path)
        if not blob:
            continue
        resources = list(iter_resources(blob))
        if not resources:
            continue
        types = {t for t, _, _, _ in resources}

        if "POST" in types:
            name = type1_fontname(resources)
            if name:
                faces.append((path, name))
        elif "FOND" in types:
            fonds = [(n, parse_fond_kern(d)) for t, _, n, d in resources if t == "FOND"]
            suitcases.append((path, fonds, "sfnt" in types))
    return faces, suitcases


def build_kern_index(suitcases):
    """Map FOND name -> kern pairs, plus a per-directory fallback.

    The fallback covers families whose FOND is named for the family rather
    than the face (the FOND "9600" against face "NineSixNilNil", say) and is
    only safe when that directory holds exactly one FOND.
    """
    by_name, by_dir = {}, {}
    for path, fonds, _ in suitcases:
        for name, pairs in fonds:
            if name:
                by_name.setdefault(normalize(name), pairs)
            by_dir.setdefault(path.parent, []).append(pairs)
    lone = {d: p[0] for d, p in by_dir.items() if len(p) == 1}
    return by_name, lone


def convert(src, dst, kern_pairs, timeout):
    """Run FontForge to open src, apply kerning, and write dst."""
    job_fd, job_path = tempfile.mkstemp(suffix=".json")
    worker_fd, worker_path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(job_fd, "w") as handle:
            json.dump({"src": str(src), "dst": str(dst), "kern": kern_pairs}, handle)
        with os.fdopen(worker_fd, "w") as handle:
            handle.write(WORKER)

        result = subprocess.run(
            ["fontforge", "-quiet", "-lang=py", "-script", worker_path, job_path],
            capture_output=True,
            check=True,
            timeout=timeout,
        )
        if not os.path.exists(dst):
            print("  Error converting: no output produced", file=sys.stderr)
            return None

        applied = 0
        for line in result.stdout.decode(errors="replace").splitlines():
            if line.startswith("KERN_APPLIED"):
                applied = int(line.split()[1])
        return applied
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode(errors="replace").strip() or str(exc)
        print(
            f"  Error converting: {err.splitlines()[-1] if err else exc}",
            file=sys.stderr,
        )
        return None
    except subprocess.TimeoutExpired:
        print("  Error converting: fontforge timed out", file=sys.stderr)
        return None
    except FileNotFoundError:
        print(
            "Error: fontforge not found. Install with: brew install fontforge",
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        for temp in (job_path, worker_path):
            if os.path.exists(temp):
                os.remove(temp)


def extract_font_names(font_path):
    """Extract family name and full font name from a generated font."""
    try:
        font = TTFont(font_path)
        family_name = font_name = None
        # nameID 1 = Family name, nameID 4 = Full font name
        for record in font["name"].names:
            if record.nameID == 1 and not family_name:
                family_name = record.toUnicode().strip()
            elif record.nameID == 4 and not font_name:
                font_name = record.toUnicode().strip()
            if family_name and font_name:
                break
        return family_name or "Unknown", font_name or family_name or "Unknown"
    except Exception as exc:
        print(
            f"  Warning: could not read names from {font_path}: {exc}", file=sys.stderr
        )
        return None, None


def sanitize_name(name):
    """Sanitize name for filesystem use."""
    return (
        "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()
        or "Unnamed"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Convert classic Mac LWFN Type 1 fonts to OTF, with FOND kerning."
    )
    parser.add_argument(
        "-i", "--input", required=True, help="Input directory, searched recursively"
    )
    parser.add_argument(
        "-o", "--output", required=True, help="Output directory for OTF files"
    )
    parser.add_argument(
        "--by-family",
        action="store_true",
        help="Group output by the font's family name instead of "
        "mirroring the input folder layout",
    )
    parser.add_argument("--no-kern", action="store_true", help="Skip FOND kerning")
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would be converted and exit"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-font fontforge timeout in seconds (default: 120)",
    )
    args = parser.parse_args()

    input_dir = Path(args.input).expanduser()
    output_dir = Path(args.output).expanduser()

    if not input_dir.is_dir():
        print(f"Error: input directory not found: {input_dir}")
        sys.exit(1)

    faces, suitcases = scan(input_dir)
    if not faces:
        print("No LWFN Type 1 fonts found.")
        if suitcases:
            print(
                f"({len(suitcases)} suitcase(s) found, but suitcases hold only "
                f"bitmaps and metrics — the outlines live in the LWFN files.)"
            )
        return

    for path, _, has_sfnt in suitcases:
        if has_sfnt:
            print(f"Note: {path.name} also holds TrueType (sfnt) resources, skipped.")

    by_name, lone_in_dir = build_kern_index(suitcases)

    print(f"Found {len(faces)} Type 1 face(s) in {len(suitcases)} suitcase(s).\n")

    if args.dry_run:
        for path, ps_name in faces:
            pairs = (
                []
                if args.no_kern
                else (
                    by_name.get(normalize(ps_name))
                    or lone_in_dir.get(path.parent)
                    or []
                )
            )
            source = "FOND" if pairs else "none"
            print(
                f"  {path.relative_to(input_dir)}  ->  {ps_name}  (kern: {source}, "
                f"{len(pairs)} pairs)"
            )
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    temp_otf = output_dir / "_temp.otf"
    successful = failed = 0

    for path, ps_name in faces:
        print(f"Processing {path.name} ({ps_name})...", end=" ", flush=True)

        pairs = (
            []
            if args.no_kern
            else (by_name.get(normalize(ps_name)) or lone_in_dir.get(path.parent) or [])
        )

        if temp_otf.exists():
            temp_otf.unlink()
        applied = convert(path, temp_otf, pairs, args.timeout)
        if applied is None:
            print("✗ Failed")
            failed += 1
            continue

        family_name, full_name = extract_font_names(str(temp_otf))
        if not family_name:
            print("✗ Failed to read font names")
            failed += 1
            continue

        if args.by_family:
            target_dir = output_dir / sanitize_name(family_name)
        else:
            target_dir = output_dir / path.parent.relative_to(input_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        final_path = target_dir / f"{sanitize_name(full_name)}.otf"
        counter = 1
        stem = final_path.stem
        while final_path.exists():
            final_path = target_dir / f"{stem}_{counter}.otf"
            counter += 1

        os.rename(str(temp_otf), str(final_path))

        note = f" (kern: {applied} pairs)" if applied else " (no kerning)"
        print(f"✓ {final_path.name}{note}")
        successful += 1

    if temp_otf.exists():
        temp_otf.unlink()

    print(f"\n✓ Converted: {successful}")
    if failed:
        print(f"✗ Failed: {failed}")


if __name__ == "__main__":
    main()
