#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import argparse
from pathlib import Path

try:
    from fontTools.ttLib import TTFont
except ImportError:
    print("Error: fontTools not found. Install with: pip install fonttools")
    sys.exit(1)


# Paths are passed as script arguments, not interpolated, to avoid quoting bugs.
# $2 is an AFM/PFM metrics file to merge kerning from, or "" to skip.
FF_SCRIPT = """\
Open($1)
if ($2 != "")
  MergeKern($2)
endif
Generate($3)
Quit(0)
"""


def convert_pfb_to_otf(pfb_path, output_path, kern_path=None):
    """Convert PFB to OTF using fontforge script, merging kerning if given.

    OTF keeps the PFB's original cubic outlines; TTF would approximate them
    as quadratics.
    """
    fd, script_path = tempfile.mkstemp(suffix=".pe")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(FF_SCRIPT)

        subprocess.run(
            [
                "fontforge",
                "-quiet",
                "-lang=ff",
                "-script",
                script_path,
                str(pfb_path),
                str(kern_path) if kern_path else "",
                str(output_path),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        if not os.path.exists(output_path):
            print("  Error converting: no output produced", file=sys.stderr)
            return False
        return True
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace").strip() or str(e)
        print(f"  Error converting: {err}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("  Error converting: fontforge timed out", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(
            "Error: fontforge not found. Install with: brew install fontforge",
            file=sys.stderr,
        )
        return False
    finally:
        if os.path.exists(script_path):
            os.remove(script_path)


def extract_font_names(font_path):
    """Extract family name and full font name from a generated font."""
    try:
        font = TTFont(font_path)
        name_table = font["name"]

        family_name = None
        font_name = None

        # nameID 1 = Family name, nameID 4 = Full font name
        for record in name_table.names:
            if record.nameID == 1 and not family_name:
                family_name = record.toUnicode().strip()
            elif record.nameID == 4 and not font_name:
                font_name = record.toUnicode().strip()

            if family_name and font_name:
                break

        # Fallback to family name if full name not found
        if not font_name:
            font_name = family_name

        return family_name or "Unknown", font_name or "Unknown"
    except Exception as e:
        print(
            f"  Warning: Could not extract names from {font_path}: {e}", file=sys.stderr
        )
        return None, None


def find_kern_file(pfb_path):
    """Find a metrics file to merge kerning from, AFM preferred.

    AFM carries kern pairs by glyph name; PFM is the cut-down binary Windows
    used. Either works with MergeKern, AFM is the fuller source. Matched by
    scanning siblings so any extension casing is picked up.
    """
    stem = pfb_path.stem.lower()
    found = {}
    for sibling in pfb_path.parent.iterdir():
        ext = sibling.suffix.lower()
        if ext in (".afm", ".pfm") and sibling.stem.lower() == stem:
            found.setdefault(ext, sibling)
    return found.get(".afm") or found.get(".pfm")


def sanitize_name(name):
    """Sanitize name for filesystem use."""
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)


def main():
    parser = argparse.ArgumentParser(description="Convert PFB fonts to OTF")
    parser.add_argument(
        "-i", "--input", required=True, help="Input directory containing PFB files"
    )
    parser.add_argument(
        "-o", "--output", required=True, help="Output directory for OTF files"
    )
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)

    if not input_dir.exists():
        print(f"Error: Input directory not found: {input_dir}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Char classes rather than "*.PFB": pathlib globs case-sensitively on
    # POSIX, and this collection mixes .pfb and .PFB.
    pfb_files = sorted(input_dir.glob("**/*.[pP][fF][bB]"))

    if not pfb_files:
        print("No PFB files found.")
        return

    print(f"Found {len(pfb_files)} PFB files. Starting conversion...\n")

    successful = 0
    failed = 0
    temp_otf = output_dir / "_temp.otf"

    for pfb_path in pfb_files:
        print(f"Processing {pfb_path.name}...", end=" ", flush=True)

        kern_path = find_kern_file(pfb_path)

        # Convert to temp OTF first
        if not convert_pfb_to_otf(str(pfb_path), str(temp_otf), kern_path):
            print(f"✗ Failed")
            failed += 1
            continue

        # Extract font names
        family_name, full_font_name = extract_font_names(str(temp_otf))

        if not family_name:
            print(f"✗ Failed to extract font names")
            failed += 1
            continue

        # Create folder for font family
        safe_family_name = sanitize_name(family_name)
        family_dir = output_dir / safe_family_name
        family_dir.mkdir(parents=True, exist_ok=True)

        # Name the OTF with full font name
        safe_font_name = sanitize_name(full_font_name)
        final_path = family_dir / f"{safe_font_name}.otf"

        # Handle duplicates
        counter = 1
        original_final = final_path
        while final_path.exists():
            stem = original_final.stem
            final_path = family_dir / f"{stem}_{counter}.otf"
            counter += 1

        # Move temp OTF to final location
        os.rename(str(temp_otf), str(final_path))

        kern_note = (
            f" (kern: {kern_path.suffix.lstrip('.').upper()})" if kern_path else ""
        )
        print(f"✓ {final_path.name}{kern_note}")
        successful += 1

    print(f"\n✓ Converted: {successful}")
    print(f"✗ Failed: {failed}")


if __name__ == "__main__":
    main()
