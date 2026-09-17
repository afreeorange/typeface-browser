# Typeface Browser

For my collection of typefaces. Entirely vibe-coded with Claude Opus 5. Uses Preact for the web UI (which is just five files.)

<p align="center">
  <img src="https://public.nikhil.io/project-screenshots/typeface-browser-light.png?" width="45.5%">
  <img src="https://public.nikhil.io/project-screenshots/typeface-browser-dark.png?" width="45.5%">
</p>

## Usage

```bash
# Build manifest
# pip install -U fonttools
python scripts/build-manifest.py

# Organize fonts
python scripts/organize.py
```

Once manifest is generated, I use this `Caddyfile`. You can run `python scripts/serve.py` as well.

```
:8080 {
      encode gzip

      @fontfile path *.otf *.ttf *.ttc *.otc *.woff *.woff2 *.eot *.svg
      handle @fontfile {
          root * /path/to/fonts
          header Cache-Control "public, max-age=86400"

          @collection path *.ttc *.otc
          header @collection Content-Type "font/collection"

          file_server
      }

      handle {
          root * /path/to/fonts/_typeface-browser/web
          header Cache-Control "no-store"
          file_server
      }
  }
```

## TODO

- [x] Allow downloads
- [ ] Link to any auxiliary media (e.g. specimen PDFs)
- [ ] Maybe JSX? -- Build step must be simple
- [ ] Maybe Tailwind? -- Build step must be simple

---

## Claude-Generated Notes

Searchable browser for the collection. Two commands:

```sh
python3 scripts/build-manifest.py     # scan fonts -> web/manifest.json
python3 scripts/serve.py              # serve the collection, opens the app
```

`serve.py` roots at the collection folder — the folder holding this repo — so the app sits at `/typeface-browser/web/` and every font loads from its manifest path. Re-runs of the scanner are incremental (`.manifest-cache.json`, keyed on size+mtime); `--no-cache` forces a full rescan, needed after changing any heuristic.

Useful flags: `--limit N`, `--dry-run`, `--jobs N`, `--report-unclassified FILE` (lists families the heuristics were unsure about, so `category-overrides.json` can be seeded from real evidence).

### Why categories are measured, not read

The metadata that should classify these fonts mostly isn't there — across a 400-font sample, PANOSE serif-style is `0` ("any") in 83%, `sFamilyClass` is unset in 98%, and `usWeightClass` is simply wrong in places (`Agenda-Black.otf` reports 400). So the scanner measures the outlines:

| signal       | how                                              | used for                                                                                                                                                                                |
| ------------ | ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `serifRatio` | width of `I` near its top/bottom vs its middle   | serif vs sans (`>= 1.08` is serif). Sans faces score exactly 1.00, Times 1.25, Baskerville 1.85                                                                                         |
| `monoRatio`  | share of glyphs on the most common advance width | monospaced (`>= 0.90`); an exact "one width" test misses real mono faces                                                                                                                |
| `stemRatio`  | stem thickness of `I` ÷ its height               | true weight, and the picture test — text stems stay under ~0.39, so `> 0.42` means the glyph is a drawing, not a letter (one-width dingbat sets would otherwise all read as monospaced) |
| `widthRatio` | advance of `H` ÷ its cap height                  | condensed / expanded                                                                                                                                                                    |

Weight comes from style-name tokens first and `usWeightClass` only as a fallback; `weightSource` records which was used. Categories carry `categoryConfidence` and `categorySource`, and `category-overrides.json` overrides any of it by family or folder.

### Manifest shape

One entry per family (keyed on folder + typographic family), styles collapsed so `.otf`/`.ttf`/`.woff2` of the same face are one style with several `files`.

Kits fight this in three ways, so the scanner undoes each:

- **A family split across format folders** — `Averta/TTF`, `Averta/WOFF`, `Averta/WOFF2` used to be three families. Trailing format-named segments are folded off the grouping directory, repeatedly, because they nest (FontAwesome ships `web/webfonts` and `desktop/otfs`). File paths are never rewritten, only the grouping.
- **Names that don't line up across formats.** Averta's desktop file is `Averta Black Italic.ttf`, its web kit's is `averta-blackitalic-webfont.woff2`; psName is no better, reporting `\x7f` in Averta's WOFF2 and `.` in every Diatype file. Records are unioned when *either* the normalised filename or the normalised psName matches, since each rescues cases the other loses. A psName carrying only the style ("Thin", "Light Italic") names no family and is ignored.
- **A mangled name table splintering a few faces** — Averta's WOFF build reports family `Averta Extra`, subfamily `☞`. When every face in a smaller group already exists under another name in the same folder, it is folded into it.

`.svg` and `.eot` are never parsed. They are attached to the style whose filename they share, and anything matching nothing is dropped — which is what keeps FontAwesome Pro's 14,808 icon SVGs from inventing styles. Each one lands on exactly one style: the group owning the most files for that face.

**Values identical across all of a family's styles are hoisted to the family and removed from the styles** — that alone cuts the file from 26 MB to 16 MB. So every style read goes through the fallback the app calls `sf()`:

```js
const sf = (style, family, key, fallback) =>
  key in style ? style[key] : (key in family ? family[key] : fallback);
```

Two compact arrays: `signals` is `[serifRatio, stemRatio, widthRatio, monoRatio]` and `metrics` is `[capHeight, xHeight, ascender, descender, lineGap]`, `null`/`0` where unmeasurable.

`preview` is the file to render; it is **omitted** when it is just `files[0].path`, and explicitly `null` for `.ttc`/`.otc`/`.eot`/`.svg`, which `@font-face` cannot load. Byte-identical copies elsewhere in the collection get `duplicateOf` pointing at the canonical path.

### App

`index.html` + `app.js` + `styles.css` + `zip.js`, `Preact` and `htm` vendored in `vendor/standalone.module.js` — no build step, no `node_modules`.

The folder tree is the navigation: `#/A/Agenda`, `#/Old OS X Collections/Agfa MonoType FontFolio`. Search and facets ride in the hash query string (`#/A?q=condensed&cat=sans&wmin=700`), so every view is a link. Search matches across all metadata — family, style, PostScript name, designer, foundry, copyright, version, file paths.

Specimens are the real fonts, loaded as rows scroll into view and released on unmount; the loader caps the live set at 400 faces (`MAX_FACES`) and calls `document.fonts.delete()` beyond that, without which scrolling 19k styles exhausts memory.

### Downloads

Single files come down through an anchor's `download` attribute — one `↓` next to every style in an expanded row, and one next to every file in the detail panel. Anything bigger is zipped, because browsers refuse a burst of programmatic downloads: **download all** takes the whole family, and the pill bar pinned to the bottom of the detail panel takes one format of it.

The pills are always `TTF OTF WOFF WOFF2 EOT SVG` in that order, each showing its file count and greyed out when the family has none — a kit's gaps should be as visible as what it ships. A format holding a single file skips the zip and hands the URL straight over.

`zip.js` writes the archive itself: store-only, no compression, ~160 lines, no dependency. Fonts barely deflate anyway.
