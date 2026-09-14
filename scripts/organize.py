#!/usr/bin/env python3
"""Organize font files into per-family subfolders.

Reads each font's internal `name` table (typographic family, else family name)
and copies the file into a folder named after that family under the output
directory. Falls back to a filename heuristic for fonts whose name table can't
be read. Every other file found is left untouched and listed in
non-ttf-otf.txt in the output directory.

Usage:
    python3 organize-ttf-otf.py -i INPUT -o OUTPUT [-n] [-y] [-m]
"""

import argparse
import os
import re
import shutil
import struct
import sys
from collections import defaultdict

FONT_EXTS = {".otf", ".ttf", ".ttc", ".otc"}

# name table IDs
NAME_FAMILY = 1
NAME_TYPO_FAMILY = 16

# ---------------------------------------------------------------- name table


def _decode(data, platform_id, encoding_id):
    if platform_id == 3 or (platform_id == 0):
        # Windows / Unicode -> UTF-16BE
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


def _read_names_pure(path):
    """Minimal sfnt parser -> {nameID: string}. Returns {} on failure."""
    names = {}
    with open(path, "rb") as fh:
        head = fh.read(4)
        if head == b"ttcf":
            fh.seek(12)
            (off,) = struct.unpack(">I", fh.read(4))  # first font in collection
            fh.seek(off)
            head = fh.read(4)
        if head not in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
            return names
        (num_tables,) = struct.unpack(">H", fh.read(2))
        fh.read(6)
        name_off = name_len = None
        for _ in range(num_tables):
            rec = fh.read(16)
            if len(rec) < 16:
                return names
            tag, _csum, off, length = struct.unpack(">4sIII", rec)
            if tag == b"name":
                name_off, name_len = off, length
                break
        if name_off is None:
            return names
        fh.seek(name_off)
        fmt, count, string_off = struct.unpack(">HHH", fh.read(6))
        records = [struct.unpack(">HHHHHH", fh.read(12)) for _ in range(count)]
        for plat, enc, _lang, name_id, length, offset in records:
            if name_id not in (NAME_FAMILY, NAME_TYPO_FAMILY):
                continue
            fh.seek(name_off + string_off + offset)
            text = _decode(fh.read(length), plat, enc)
            if not text:
                continue
            # prefer Windows records over Mac ones
            key = (name_id, plat == 3)
            prev = names.get(name_id)
            if prev is None or (plat == 3 and not prev[1]):
                names[name_id] = (text, plat == 3)
    return {k: v[0] for k, v in names.items()}


def _read_names_fonttools(path):
    from fontTools.ttLib import TTFont, TTCollection

    if path.lower().endswith((".ttc", ".otc")):
        coll = TTCollection(path, lazy=True)
        try:
            font = coll.fonts[0]
            return _names_from_ttfont(font)
        finally:
            coll.close()
    font = TTFont(path, lazy=True, fontNumber=0)
    try:
        return _names_from_ttfont(font)
    finally:
        font.close()


def _names_from_ttfont(font):
    out = {}
    table = font.get("name")
    if table is None:
        return out
    for name_id in (NAME_TYPO_FAMILY, NAME_FAMILY):
        rec = table.getDebugName(name_id)
        if rec:
            out[name_id] = rec
    return out


def read_family(path, use_fonttools=True):
    names = {}
    if use_fonttools:
        try:
            names = _read_names_fonttools(path)
        except Exception:
            names = {}
    if not names:
        try:
            names = _read_names_pure(path)
        except Exception:
            names = {}
    for name_id in (NAME_TYPO_FAMILY, NAME_FAMILY):
        value = clean(names.get(name_id, ""))
        if value:
            return value
    return None


# ------------------------------------------------------------------ helpers

STYLE_WORDS = (
    r"thin|ultralight|ultlt|extralight|light|lt|book|regular|roman|normal|medium|md|"
    r"demi|semibold|semi|bold|bd|heavy|hv|black|blk|extrablack|xblk|ultrabold|ultra|"
    r"extrabold|xbold|italic|ital|it|oblique|obl|o|condensed|cond|cn|narrow|"
    r"extended|ext|ex|wide|smallcaps|sc|caps|alt|frac|display|titling|poster|num"
)


def clean(text):
    text = re.sub(r"[\x00-\x1f]", "", text or "").strip()
    # strip characters that are illegal or awkward in folder names
    text = re.sub(r'[/\\:*?"<>|]', "-", text)
    return re.sub(r"\s+", " ", text).strip(" .-")


def family_from_filename(path):
    """Heuristic fallback: strip trailing style tokens from the stem."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[_\s]+$", "", stem)
    # split on the first hyphen/underscore: "GillSansMTPro-BdCn" -> "GillSansMTPro"
    base = re.split(r"[-_]", stem, 1)[0]
    if not base:
        base = stem
    # split camelCase into words
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", base)
    # drop trailing style words
    while len(words) > 1 and re.fullmatch(STYLE_WORDS, words[-1], re.I):
        words.pop()
    return clean(" ".join(words)) or clean(stem)


NON_FONT_LOG = "non-ttf-otf.txt"


def is_font(name):
    return os.path.splitext(name)[1].lower() in FONT_EXTS


def iter_files(root, recursive):
    """Yield every file under root; non-font files are yielded too."""
    if recursive:
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                yield os.path.join(dirpath, name)
    else:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isfile(full):
                yield full


def unique_dest(dest):
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(dest)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def write_non_font_log(output_dir, input_dir, paths):
    """List non-font files (untouched, still in the input dir) to NON_FONT_LOG."""
    log_path = os.path.join(output_dir, NON_FONT_LOG)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {len(paths)} non-font file(s) left untouched in {input_dir}\n")
        for path in sorted(paths, key=str.lower):
            fh.write(os.path.abspath(path) + "\n")
    return log_path


# --------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "-i", "--input", required=True, help="input directory containing font files"
    )
    ap.add_argument(
        "-o", "--output", required=True, help="output directory for the family folders"
    )
    ap.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would happen only"
    )
    ap.add_argument(
        "-y", "--yes", action="store_true", help="don't ask for confirmation"
    )
    ap.add_argument(
        "-r", "--recursive", action="store_true", help="also descend into subfolders"
    )
    ap.add_argument(
        "-m", "--move", action="store_true", help="move instead of the default copy"
    )
    ap.add_argument(
        "--by-filename",
        action="store_true",
        help="group by filename heuristic, ignore the font's name table",
    )
    args = ap.parse_args()

    input_dir = os.path.abspath(args.input)
    output_dir = os.path.abspath(args.output)
    if not os.path.isdir(input_dir):
        sys.exit(f"not a directory: {input_dir}")

    use_fonttools = True
    if not args.by_filename:
        try:
            import fontTools  # noqa: F401
        except ImportError:
            use_fonttools = False

    plan = defaultdict(list)  # family -> [paths]
    guessed = []
    non_fonts = []
    skip_prefix = output_dir + os.sep
    for path in iter_files(input_dir, args.recursive):
        # don't re-scan a previous run's output when it sits inside the input
        if path.startswith(skip_prefix):
            continue
        if not is_font(path):
            non_fonts.append(path)
            continue
        family = None
        if not args.by_filename:
            family = read_family(path, use_fonttools)
        if not family:
            family = family_from_filename(path)
            guessed.append(path)
        plan[family].append(path)

    if not plan:
        print(f"no font files ({', '.join(sorted(FONT_EXTS))}) found in {input_dir}")
        if non_fonts and not args.dry_run:
            os.makedirs(output_dir, exist_ok=True)
            write_non_font_log(output_dir, input_dir, non_fonts)
            print(
                f"{len(non_fonts)} non-font file(s) listed in "
                f"{os.path.join(output_dir, NON_FONT_LOG)}, left in place"
            )
        return

    total = sum(len(v) for v in plan.values())
    verb = "move" if args.move else "copy"
    verb_past = "moved" if args.move else "copied"
    print(f"{total} font(s) -> {len(plan)} family folder(s) in {output_dir}\n")
    for family in sorted(plan, key=str.lower):
        files = plan[family]
        print(f"  {family}/  ({len(files)})")
        for path in files:
            print(f"      {os.path.relpath(path, input_dir)}")
    if guessed:
        print(
            f"\n  note: {len(guessed)} file(s) had no readable family name; "
            "grouped by filename."
        )
    if non_fonts:
        print(
            f"  note: {len(non_fonts)} non-font file(s) will be left in place "
            f"and listed in {NON_FONT_LOG}."
        )

    if args.dry_run:
        print("\ndry run - nothing changed.")
        return

    if not args.yes and sys.stdin.isatty():
        if input(f"\n{verb} these files? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted.")
            return

    os.makedirs(output_dir, exist_ok=True)

    done = 0
    skipped = []  # (path, reason) - already in place
    failed = []  # (path, reason)
    for family in sorted(plan, key=str.lower):
        folder = os.path.join(output_dir, family)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            for path in plan[family]:
                failed.append((path, str(exc)))
            continue
        for path in plan[family]:
            dest = os.path.join(folder, os.path.basename(path))
            if os.path.abspath(path) == os.path.abspath(dest):
                skipped.append((path, "already in place"))
                continue
            dest = unique_dest(dest)
            try:
                if args.move:
                    shutil.move(path, dest)
                else:
                    shutil.copy2(path, dest)
                done += 1
            except OSError as exc:
                failed.append((path, str(exc)))

    log_path = None
    if non_fonts:
        log_path = write_non_font_log(output_dir, input_dir, non_fonts)

    print(f"\nscanned:   {total} font(s)")
    print(f"  name table: {total - len(guessed)}")
    print(f"  filename:   {len(guessed)}")
    print(f"{verb_past}:    {done} file(s) into {len(plan)} family folder(s)")
    if skipped:
        print(f"skipped:   {len(skipped)} (already in place)")
    print(f"failed:    {len(failed)}")
    for path, reason in failed:
        print(f"  {os.path.relpath(path, input_dir)}: {reason}", file=sys.stderr)
    if non_fonts:
        print(
            f"non-font:  {len(non_fonts)} file(s) left in place, listed in {log_path}"
        )


if __name__ == "__main__":
    main()
