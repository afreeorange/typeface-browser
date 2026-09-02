import {
  html,
  render,
  useState,
  useEffect,
  useMemo,
  useRef,
  useCallback,
} from "./vendor/standalone.module.js";

// ---------------------------------------------------------------- manifest
//
// The scanner hoists any value a family's styles all agree on up to the family
// and drops it from the styles, so every style read goes through sf().

const sf = (style, family, key, fallback) =>
  key in style ? style[key] : key in family ? family[key] : fallback;

const CATEGORY_ORDER = [
  "serif",
  "sans",
  "slab",
  "display",
  "script",
  "blackletter",
  "mono",
  "symbol",
  "unknown",
];
const WIDTH_ORDER = [
  "ultra-condensed",
  "extra-condensed",
  "condensed",
  "semi-condensed",
  "normal",
  "semi-expanded",
  "expanded",
  "extra-expanded",
  "ultra-expanded",
];
const WEIGHT_NAMES = {
  100: "Thin",
  200: "ExtraLight",
  300: "Light",
  350: "SemiLight",
  400: "Regular",
  500: "Medium",
  600: "SemiBold",
  700: "Bold",
  800: "ExtraBold",
  900: "Black",
  950: "ExtraBlack",
};

// --------------------------------------------------------------- font loader
//
// Fonts are loaded on demand as rows scroll into view and released when the
// row unmounts. Without the eviction pass, scrolling the full 19k styles piles
// up every face the browser ever parsed.

const MAX_FACES = 400;
let fontBase = new URL(".", location.href);
let faceSeq = 0;
const faces = new Map(); // path -> {id, state, refs, face, subs}
const released = []; // paths with refs === 0, oldest first

const encodePath = (p) => p.split("/").map(encodeURIComponent).join("/");

function notify(entry) {
  entry.subs.forEach((cb) => cb());
}

function evict() {
  while (faces.size > MAX_FACES && released.length) {
    const path = released.shift();
    const entry = faces.get(path);
    if (!entry || entry.refs > 0) continue;
    if (entry.face && entry.state === "ok") {
      try {
        document.fonts.delete(entry.face);
      } catch (e) {
        /* ignore */
      }
    }
    faces.delete(path);
  }
}

function retain(path) {
  let entry = faces.get(path);
  if (!entry) {
    entry = {
      id: `fnt${(faceSeq += 1)}`,
      state: "loading",
      refs: 0,
      subs: new Set(),
    };
    faces.set(path, entry);
    const url = new URL(encodePath(path), fontBase).href;
    const face = new FontFace(entry.id, `url("${url}")`);
    entry.face = face;
    face
      .load()
      .then(() => {
        document.fonts.add(face);
        entry.state = "ok";
        notify(entry);
      })
      .catch(() => {
        entry.state = "failed";
        notify(entry);
      });
  }
  entry.refs += 1;
  return entry;
}

function release(path) {
  const entry = faces.get(path);
  if (!entry) return;
  entry.refs -= 1;
  if (entry.refs <= 0) {
    released.push(path);
    evict();
  }
}

/** Loads `path` while mounted; returns [cssFamily, state]. */
function useFont(path) {
  const [, bump] = useState(0);
  useEffect(() => {
    if (!path) return undefined;
    const entry = retain(path);
    const cb = () => bump((n) => n + 1);
    entry.subs.add(cb);
    cb();
    return () => {
      entry.subs.delete(cb);
      release(path);
    };
  }, [path]);
  if (!path) return [null, "none"];
  const entry = faces.get(path);
  return entry ? [entry.id, entry.state] : [null, "loading"];
}

function Specimen({ path, text, size, italic, className }) {
  const [family, state] = useFont(path);
  if (!path) {
    return html`
      <div class="specimen failed">no previewable file (.ttc/.otc)</div>
    `;
  }
  if (state === "failed") {
    return html`
      <div class="specimen failed">could not load ${path}</div>
    `;
  }
  const style =
    state === "ok"
      ? `font-family:'${family}',serif;font-size:${size}px;${italic ? "font-style:italic;" : ""}`
      : `font-size:${size}px`;
  return html`
    <div
      class="specimen ${state === "ok" ? "" : "pending"} ${className || ""}"
      style=${style}
    >
      ${text}
    </div>
  `;
}

// -------------------------------------------------------------------- routing
//
// #/<folder>/<sub>?q=…&cat=…  -- the folder path scopes the list, the query
// string carries search and facets, so any view is a link.

const LIST_KEYS = ["cat", "width", "fmt", "cov", "foundry", "feat"];

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [pathPart, queryPart = ""] = raw.split("?");
  const path = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(queryPart);
  const state = { path, q: params.get("q") || "" };
  LIST_KEYS.forEach((key) => {
    state[key] = (params.get(key) || "").split(",").filter(Boolean);
  });
  state.wmin = Number(params.get("wmin") || 100);
  state.wmax = Number(params.get("wmax") || 950);
  state.italic = params.get("italic") || "any";
  state.mono = params.get("mono") || "any";
  state.sort = params.get("sort") || "name";
  return state;
}

function writeHash(state, push) {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  LIST_KEYS.forEach((key) => {
    if (state[key] && state[key].length) params.set(key, state[key].join(","));
  });
  if (state.wmin !== 100) params.set("wmin", state.wmin);
  if (state.wmax !== 950) params.set("wmax", state.wmax);
  if (state.italic !== "any") params.set("italic", state.italic);
  if (state.mono !== "any") params.set("mono", state.mono);
  if (state.sort !== "name") params.set("sort", state.sort);
  const query = params.toString();
  const hash = `#/${state.path.map(encodeURIComponent).join("/")}${query ? `?${query}` : ""}`;
  if (hash === location.hash) return;
  if (push) location.hash = hash;
  else history.replaceState(null, "", hash);
}

// ------------------------------------------------------------------ indexing

function buildIndex(manifest) {
  manifest.families.forEach((family) => {
    const parts = [
      family.name,
      family.dir,
      family.category,
      (family.tags || []).join(" "),
    ];
    [
      "foundry",
      "designer",
      "manufacturer",
      "copyright",
      "trademark",
      "description",
      "version",
      "vendorID",
      "license",
    ].forEach((key) => {
      if (family[key]) parts.push(family[key]);
    });
    parts.push((family.features || []).join(" "));
    parts.push((family.coverage || []).join(" "));
    family.styles.forEach((style) => {
      parts.push(style.name);
      [
        "psName",
        "fullName",
        "version",
        "copyright",
        "designer",
        "manufacturer",
        "description",
        "trademark",
      ].forEach((key) => {
        if (style[key]) parts.push(style[key]);
      });
      style.files.forEach((file) => parts.push(file.path));
    });
    family.hay = parts.join(" ").toLowerCase();
    family.weightMin = Math.min(
      ...family.styles.map((s) => sf(s, family, "weight", 400)),
    );
    family.weightMax = Math.max(
      ...family.styles.map((s) => sf(s, family, "weight", 400)),
    );
    family.widthSet = [
      ...new Set(family.styles.map((s) => sf(s, family, "width", "normal"))),
    ];
    family.newest = Math.max(
      0,
      ...family.styles.map((s) => sf(s, family, "created", 0) || 0),
    );
    family.glyphMax = Math.max(
      0,
      ...family.styles.map((s) => sf(s, family, "glyphs", 0) || 0),
    );
  });
  return manifest;
}

// ----------------------------------------------------------------- filtering

function matchesPath(family, path) {
  for (let i = 0; i < path.length; i += 1) {
    if (family.path[i] !== path[i]) return false;
  }
  return true;
}

function filterFamilies(families, state, skip) {
  const tokens = state.q.toLowerCase().split(/\s+/).filter(Boolean);
  return families.filter((family) => {
    if (!matchesPath(family, state.path)) return false;
    if (tokens.length && !tokens.every((t) => family.hay.includes(t)))
      return false;
    if (
      skip !== "cat" &&
      state.cat.length &&
      !state.cat.includes(family.category)
    )
      return false;
    if (
      skip !== "width" &&
      state.width.length &&
      !state.width.some((w) => family.widthSet.includes(w))
    )
      return false;
    if (
      skip !== "fmt" &&
      state.fmt.length &&
      !state.fmt.some((f) => family.formats.includes(f))
    )
      return false;
    if (
      skip !== "cov" &&
      state.cov.length &&
      !state.cov.every((c) => family.coverage.includes(c))
    )
      return false;
    if (
      skip !== "feat" &&
      state.feat.length &&
      !state.feat.every((f) => family.features.includes(f))
    )
      return false;
    if (
      skip !== "foundry" &&
      state.foundry.length &&
      !state.foundry.includes(family.foundry || "")
    )
      return false;
    if (state.wmin > 100 || state.wmax < 950) {
      if (family.weightMax < state.wmin || family.weightMin > state.wmax)
        return false;
    }
    if (state.italic === "yes" && !family.hasItalic) return false;
    if (
      state.italic === "no" &&
      family.hasItalic &&
      family.styles.every((s) => sf(s, family, "italic", false))
    )
      return false;
    if (state.mono === "yes" && !family.hasMono) return false;
    if (state.mono === "no" && family.category === "mono") return false;
    return true;
  });
}

const SORTS = {
  name: (a, b) =>
    a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
  "name-desc": (a, b) =>
    b.name.localeCompare(a.name, undefined, { sensitivity: "base" }),
  styles: (a, b) => b.styleCount - a.styleCount || a.name.localeCompare(b.name),
  glyphs: (a, b) => b.glyphMax - a.glyphMax || a.name.localeCompare(b.name),
  newest: (a, b) => b.newest - a.newest || a.name.localeCompare(b.name),
  folder: (a, b) => a.dir.localeCompare(b.dir) || a.name.localeCompare(b.name),
};

function tally(families, pick) {
  const counts = new Map();
  families.forEach((family) => {
    const values = pick(family);
    (Array.isArray(values) ? values : [values]).forEach((value) => {
      if (value === undefined || value === null || value === "") return;
      counts.set(value, (counts.get(value) || 0) + 1);
    });
  });
  return counts;
}

// ------------------------------------------------------------------ sidebar

function Group({ title, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  return html`
    <div class="group">
      <h3 onClick=${() => setOpen(!open)}>
        ${title}
        <span>${open ? "−" : "+"}</span>
      </h3>
      ${open &&
      html`
        <div class="body">${children}</div>
      `}
    </div>
  `;
}

function CheckList({
  counts,
  selected,
  onToggle,
  order,
  limit = 12,
  searchable,
}) {
  const [expanded, setExpanded] = useState(false);
  const [needle, setNeedle] = useState("");
  let entries = [...counts.entries()];
  if (order) {
    entries.sort((a, b) => {
      const ia = order.indexOf(a[0]);
      const ib = order.indexOf(b[0]);
      return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
    });
  } else {
    entries.sort(
      (a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])),
    );
  }
  if (needle) {
    const n = needle.toLowerCase();
    entries = entries.filter(([key]) => String(key).toLowerCase().includes(n));
  }
  selected.forEach((key) => {
    if (!entries.some(([k]) => k === key)) entries.unshift([key, 0]);
  });
  const shown = expanded ? entries : entries.slice(0, limit);
  return html`
    <div>
      ${searchable &&
      entries.length > limit &&
      html`
        <input
          class="facet-search"
          type="text"
          placeholder="filter…"
          value=${needle}
          onInput=${(e) => setNeedle(e.target.value)}
        />
      `}
      ${shown.map(
        ([key, n]) => html`
          <label class="opt ${selected.includes(key) ? "sel" : ""}" key=${key}>
            <input
              type="checkbox"
              checked=${selected.includes(key)}
              onChange=${() => onToggle(key)}
            />
            <span>${key}</span>
            <span class="n">${n}</span>
          </label>
        `,
      )}
      ${entries.length > limit &&
      html`
        <div class="more" onClick=${() => setExpanded(!expanded)}>
          ${expanded ? "show fewer" : `show all ${entries.length}`}
        </div>
      `}
    </div>
  `;
}

function TreeNode({ name, node, prefix, current, go, depth }) {
  const path = [...prefix, name];
  const isCurrent =
    current.length === path.length &&
    path.every((seg, i) => current[i] === seg);
  const onPath = path.every((seg, i) => current[i] === seg);
  const [open, setOpen] = useState(depth === 0 ? false : onPath);
  const children = Object.entries(node.children || {});
  useEffect(() => {
    if (onPath) setOpen(true);
  }, [onPath]);
  return html`
    <div>
      <div
        class="tnode ${isCurrent ? "sel" : ""}"
        style=${`padding-left:${6 + depth * 11}px`}
        onClick=${() => {
          if (children.length) setOpen(!open);
          go(path);
        }}
      >
        <span
          class="tw"
          onClick=${(e) => {
            e.stopPropagation();
            setOpen(!open);
          }}
        >
          ${children.length ? (open ? "▾" : "▸") : ""}
        </span>
        <span class="tl" title=${name}>${name}</span>
        <span class="n">${node.families}</span>
      </div>
      ${open &&
      children
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(
          ([childName, child]) => html`
            <${TreeNode}
              key=${childName}
              name=${childName}
              node=${child}
              prefix=${path}
              current=${current}
              go=${go}
              depth=${depth + 1}
            />
          `,
        )}
    </div>
  `;
}

// ------------------------------------------------------------------- detail

function fmtBytes(n) {
  if (!n) return "—";
  return n < 1024
    ? `${n} B`
    : n < 1048576
      ? `${(n / 1024).toFixed(0)} KB`
      : `${(n / 1048576).toFixed(1)} MB`;
}

function fmtDate(seconds) {
  if (!seconds) return null;
  const d = new Date(seconds * 1000);
  return Number.isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10);
}

function Row({ label, value }) {
  if (value === undefined || value === null || value === "") return null;
  return html`
    <dt>${label}</dt>
    <dd>${value}</dd>
  `;
}

function Detail({ family, text, size, onClose }) {
  const [copied, setCopied] = useState("");
  const copy = (path) => {
    navigator.clipboard?.writeText(path);
    setCopied(path);
    setTimeout(() => setCopied(""), 1200);
  };
  const meta = (key) => family[key];
  const dupes = family.duplicateCount || 0;
  return html`
    <div class="detail">
      <button class="close" onClick=${onClose}>close</button>
      <h2>${family.name}</h2>
      <div class="sub">
        ${family.category}${family.categoryConfidence !== undefined
          ? ` · ${Math.round(family.categoryConfidence * 100)}% (${family.categorySource})`
          : ""}
        · ${family.styleCount} style${family.styleCount === 1 ? "" : "s"} ·
        ${family.fileCount} file${family.fileCount === 1 ? "" : "s"}
        ${dupes
          ? html`
              ·
              <span class="dup">
                ${dupes} duplicate${dupes === 1 ? "" : "s"}
              </span>
            `
          : null}
      </div>

      <div class="sec">
        <h4>metadata</h4>
        <dl class="kv">
          <${Row} label="folder" value=${family.dir} />
          <${Row} label="foundry" value=${meta("foundry")} />
          <${Row} label="designer" value=${meta("designer")} />
          <${Row} label="manufacturer" value=${meta("manufacturer")} />
          <${Row} label="version" value=${meta("version")} />
          <${Row} label="vendor ID" value=${meta("vendorID")} />
          <${Row}
            label="weights"
            value=${family.weights
              .map((w) => `${w}${WEIGHT_NAMES[w] ? ` ${WEIGHT_NAMES[w]}` : ""}`)
              .join(", ")}
          />
          <${Row} label="widths" value=${family.widths.join(", ")} />
          <${Row} label="formats" value=${family.formats.join(", ")} />
          <${Row}
            label="coverage"
            value=${(family.coverage || []).join(", ")}
          />
          <${Row} label="features" value=${(family.features || []).join(" ")} />
          <${Row} label="tags" value=${(family.tags || []).join(", ")} />
          <${Row}
            label="signals"
            value=${family.signals
              ? `serif ${family.signals[0] ?? "—"} · stem ${family.signals[1] ?? "—"}` +
                ` · width ${family.signals[2] ?? "—"} · mono ${family.signals[3] ?? "—"}`
              : null}
          />
          <${Row} label="description" value=${meta("description")} />
          <${Row} label="copyright" value=${meta("copyright")} />
          <${Row} label="trademark" value=${meta("trademark")} />
          <${Row} label="license" value=${meta("license")} />
          <${Row} label="license URL" value=${meta("licenseURL")} />
        </dl>
      </div>

      <div class="sec">
        <h4>${family.styleCount} styles</h4>
        ${family.styles.map((style, i) => {
          const weight = sf(style, family, "weight", 400);
          const italic = sf(style, family, "italic", false);
          const signals = sf(style, family, "signals", null);
          const created = fmtDate(sf(style, family, "created", 0));
          const preview =
            "preview" in style ? style.preview : style.files[0].path;
          return html`
            <div class="styleitem" key=${style.name + i}>
              <div class="sname">
                <b style="color:var(--fg)">${style.name}</b>
                <span class="chip">
                  ${weight}${WEIGHT_NAMES[weight]
                    ? ` ${WEIGHT_NAMES[weight]}`
                    : ""}
                </span>
                <span class="chip">
                  ${sf(style, family, "width", "normal")}
                </span>
                ${italic
                  ? html`
                      <span class="chip">italic</span>
                    `
                  : null}
                ${sf(style, family, "mono", false)
                  ? html`
                      <span class="chip">mono</span>
                    `
                  : null}
                <span>${sf(style, family, "glyphs", 0)} glyphs</span>
                ${created
                  ? html`
                      <span>· ${created}</span>
                    `
                  : null}
                ${signals && signals[0] != null
                  ? html`
                      <span>· serif ${signals[0]}</span>
                    `
                  : null}
              </div>
              <${Specimen}
                path=${preview}
                text=${text}
                size=${size}
                italic=${false}
              />
              ${style.files.map(
                (file) => html`
                  <div
                    class="path ${file.duplicateOf ? "dup" : ""}"
                    key=${file.path}
                    title="click to copy"
                    onClick=${() => copy(file.path)}
                  >
                    ${copied === file.path ? "✓ copied  " : ""}${file.path} ·
                    ${file.format} · ${fmtBytes(file.size)}
                    ${file.duplicateOf
                      ? ` · duplicate of ${file.duplicateOf}`
                      : ""}
                  </div>
                `,
              )}
            </div>
          `;
        })}
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------------- app

const DEFAULT_TEXT = "Handgloves ABCDEFG abcdefg 0123456789";

function App({ manifest }) {
  const [state, setState] = useState(parseHash);
  const [text, setText] = useState(
    () => localStorage.getItem("fl.text") || DEFAULT_TEXT,
  );
  const [size, setSize] = useState(
    () => Number(localStorage.getItem("fl.size")) || 34,
  );
  const [theme, setTheme] = useState(
    () =>
      localStorage.getItem("fl.theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  );
  const [selected, setSelected] = useState(null);
  const [asideOpen, setAsideOpen] = useState(
    () =>
      (localStorage.getItem("fl.aside") ?? (innerWidth > 860 ? "1" : "0")) ===
      "1",
  );
  const scroller = useRef(null);

  useEffect(() => {
    const onHash = () => setState(parseHash());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("fl.theme", theme);
  }, [theme]);
  useEffect(() => {
    localStorage.setItem("fl.text", text);
  }, [text]);
  useEffect(() => {
    localStorage.setItem("fl.size", size);
  }, [size]);
  useEffect(() => {
    localStorage.setItem("fl.aside", asideOpen ? "1" : "0");
  }, [asideOpen]);

  const update = useCallback((patch, push) => {
    setState((prev) => {
      const next = { ...prev, ...patch };
      writeHash(next, push);
      return next;
    });
    if (scroller.current) scroller.current.scrollTop = 0;
  }, []);

  const toggle = useCallback((key, value) => {
    setState((prev) => {
      const list = prev[key];
      const next = {
        ...prev,
        [key]: list.includes(value)
          ? list.filter((v) => v !== value)
          : [...list, value],
      };
      writeHash(next, false);
      return next;
    });
    if (scroller.current) scroller.current.scrollTop = 0;
  }, []);

  const families = manifest.families;
  const results = useMemo(() => {
    const list = filterFamilies(families, state);
    return list.sort(SORTS[state.sort] || SORTS.name);
  }, [families, state]);

  // facet counts come from everything filtered *except* that facet, so the
  // numbers say what each option would add rather than what is already showing
  const counts = useMemo(
    () => ({
      cat: tally(filterFamilies(families, state, "cat"), (f) => f.category),
      width: tally(filterFamilies(families, state, "width"), (f) => f.widthSet),
      fmt: tally(filterFamilies(families, state, "fmt"), (f) => f.formats),
      cov: tally(filterFamilies(families, state, "cov"), (f) => f.coverage),
      feat: tally(filterFamilies(families, state, "feat"), (f) => f.features),
      foundry: tally(
        filterFamilies(families, state, "foundry"),
        (f) => f.foundry,
      ),
    }),
    [families, state],
  );

  const rowHeight = Math.round(38 + size * 1.35);
  const [range, setRange] = useState([0, 40]);
  useEffect(() => {
    const el = scroller.current;
    if (!el) return undefined;
    const onScroll = () => {
      const viewport = Math.max(el.clientHeight, 600);
      const start = Math.max(0, Math.floor(el.scrollTop / rowHeight) - 4);
      const end = Math.min(
        results.length,
        Math.ceil((el.scrollTop + viewport) / rowHeight) + 4,
      );
      setRange([start, end]);
    };
    onScroll();
    el.addEventListener("scroll", onScroll, { passive: true });
    addEventListener("resize", onScroll);
    return () => {
      el.removeEventListener("scroll", onScroll);
      removeEventListener("resize", onScroll);
    };
  }, [results, rowHeight]);

  const go = useCallback((path) => {
    setSelected(null);
    setState((prev) => {
      const next = { ...prev, path };
      writeHash(next, true);
      return next;
    });
    if (scroller.current) scroller.current.scrollTop = 0;
  }, []);

  const visible = results.slice(range[0], range[1]);
  const totalStyles = results.reduce((n, f) => n + f.styleCount, 0);

  return html`
    <div class="app">
      <header>
        <button
          class="aside-toggle ${asideOpen ? "on" : ""}"
          title="toggle filters"
          aria-label="toggle filters"
          aria-expanded=${asideOpen}
          onClick=${() => setAsideOpen((v) => !v)}
        >
          ☰
        </button>
        <span class="brand">Type Library</span>
        <span class="count">
          ${results.length.toLocaleString()} families ·
          ${totalStyles.toLocaleString()} styles
        </span>
        <span class="grow">
          <input
            type="search"
            placeholder="search every field — name, designer, copyright, path…"
            value=${state.q}
            onInput=${(e) => update({ q: e.target.value })}
          />
        </span>
        <input
          type="text"
          style="width:230px"
          value=${text}
          aria-label="preview text"
          onInput=${(e) => setText(e.target.value)}
        />
        <label class="inline">
          size
          <input
            type="range"
            min="10"
            max="120"
            value=${size}
            onInput=${(e) => setSize(Number(e.target.value))}
          />
          <span style="width:28px;text-align:right">${size}</span>
        </label>
        <button onClick=${() => setTheme(theme === "dark" ? "light" : "dark")}>
          ${theme === "dark" ? "☀" : "☾"}
        </button>
      </header>

      <div class="panes">
        <aside class=${asideOpen ? "" : "collapsed"}>
          <${Group} title="folders">
            <div class="tree">
              <div
                class="tnode ${state.path.length === 0 ? "sel" : ""}"
                style="padding-left:6px"
                onClick=${() => go([])}
              >
                <span class="tw"></span>
                <span class="tl">all fonts</span>
                <span class="n">${manifest.tree.families}</span>
              </div>
              ${Object.entries(manifest.tree.children)
                .sort((a, b) => a[0].localeCompare(b[0]))
                .map(
                  ([name, node]) => html`
                    <${TreeNode}
                      key=${name}
                      name=${name}
                      node=${node}
                      prefix=${[]}
                      current=${state.path}
                      go=${go}
                      depth=${0}
                    />
                  `,
                )}
            </div>
          <//>

          <${Group} title="category">
            <${CheckList}
              counts=${counts.cat}
              selected=${state.cat}
              order=${CATEGORY_ORDER}
              limit=${12}
              onToggle=${(v) => toggle("cat", v)}
            />
          <//>

          <${Group} title="weight">
            <label class="inline">
              min
              <input
                type="range"
                min="100"
                max="950"
                step="50"
                value=${state.wmin}
                onInput=${(e) => update({ wmin: Number(e.target.value) })}
              />
              <span>${state.wmin}</span>
            </label>
            <label class="inline">
              max
              <input
                type="range"
                min="100"
                max="950"
                step="50"
                value=${state.wmax}
                onInput=${(e) => update({ wmax: Number(e.target.value) })}
              />
              <span>${state.wmax}</span>
            </label>
          <//>

          <${Group} title="width">
            <${CheckList}
              counts=${counts.width}
              selected=${state.width}
              order=${WIDTH_ORDER}
              limit=${9}
              onToggle=${(v) => toggle("width", v)}
            />
          <//>

          <${Group} title="style">
            ${["any", "yes", "no"].map(
              (v) => html`
                <label
                  class="opt ${state.italic === v ? "sel" : ""}"
                  key=${`i${v}`}
                >
                  <input
                    type="radio"
                    name="italic"
                    checked=${state.italic === v}
                    onChange=${() => update({ italic: v })}
                  />
                  <span>italic: ${v}</span>
                </label>
              `,
            )}
            ${["any", "yes", "no"].map(
              (v) => html`
                <label
                  class="opt ${state.mono === v ? "sel" : ""}"
                  key=${`m${v}`}
                >
                  <input
                    type="radio"
                    name="mono"
                    checked=${state.mono === v}
                    onChange=${() => update({ mono: v })}
                  />
                  <span>monospaced: ${v}</span>
                </label>
              `,
            )}
          <//>

          <${Group} title="opentype features" defaultOpen=${false}>
            <${CheckList}
              counts=${counts.feat}
              selected=${state.feat}
              limit=${14}
              searchable=${true}
              onToggle=${(v) => toggle("feat", v)}
            />
          <//>

          <${Group} title="foundry / designer" defaultOpen=${false}>
            <${CheckList}
              counts=${counts.foundry}
              selected=${state.foundry}
              limit=${14}
              searchable=${true}
              onToggle=${(v) => toggle("foundry", v)}
            />
          <//>

          <${Group} title="unicode coverage" defaultOpen=${false}>
            <${CheckList}
              counts=${counts.cov}
              selected=${state.cov}
              limit=${12}
              onToggle=${(v) => toggle("cov", v)}
            />
          <//>

          <${Group} title="format" defaultOpen=${false}>
            <${CheckList}
              counts=${counts.fmt}
              selected=${state.fmt}
              limit=${8}
              onToggle=${(v) => toggle("fmt", v)}
            />
          <//>
        </aside>

        <main>
          <div class="toolbar">
            <div class="crumbs">
              <a onClick=${() => go([])}>all</a>
              ${state.path.map(
                (seg, i) => html`
                  <span key=${seg + i}>
                    /
                    <a onClick=${() => go(state.path.slice(0, i + 1))}>
                      ${seg}
                    </a>
                  </span>
                `,
              )}
            </div>
            <span style="flex:1"></span>
            <label class="inline">
              sort
              <select
                value=${state.sort}
                onChange=${(e) => update({ sort: e.target.value })}
              >
                <option value="name">A–Z</option>
                <option value="name-desc">Z–A</option>
                <option value="styles">most styles</option>
                <option value="glyphs">most glyphs</option>
                <option value="newest">newest</option>
                <option value="folder">folder</option>
              </select>
            </label>
            ${(state.q ||
              state.cat.length ||
              state.width.length ||
              state.fmt.length ||
              state.cov.length ||
              state.feat.length ||
              state.foundry.length ||
              state.wmin > 100 ||
              state.wmax < 950 ||
              state.italic !== "any" ||
              state.mono !== "any") &&
            html`
              <button
                onClick=${() =>
                  update({
                    q: "",
                    cat: [],
                    width: [],
                    fmt: [],
                    cov: [],
                    feat: [],
                    foundry: [],
                    wmin: 100,
                    wmax: 950,
                    italic: "any",
                    mono: "any",
                  })}
              >
                clear filters
              </button>
            `}
          </div>

          <div class="scroller" ref=${scroller}>
            ${results.length === 0
              ? html`
                  <div class="empty">nothing matches.</div>
                `
              : html`
                  <div
                    class="rows"
                    style=${`height:${results.length * rowHeight}px`}
                  >
                    ${visible.map((family, i) => {
                      const index = range[0] + i;
                      const style =
                        family.styles.find(
                          (s) =>
                            !sf(s, family, "italic", false) &&
                            sf(s, family, "weight", 400) === 400,
                        ) || family.styles[0];
                      const preview =
                        "preview" in style
                          ? style.preview
                          : style.files[0].path;
                      return html`
                        <div
                          class="row ${selected === family.id ? "sel" : ""}"
                          key=${family.id}
                          style=${`top:${index * rowHeight}px;height:${rowHeight}px`}
                          onClick=${() =>
                            setSelected(
                              selected === family.id ? null : family.id,
                            )}
                        >
                          <div class="head">
                            <span class="fam">${family.name}</span>
                            <span class="meta">
                              ${[
                                family.category,
                                `${family.styleCount} style${family.styleCount === 1 ? "" : "s"}`,
                                family.dir,
                                family.foundry,
                              ]
                                .filter(Boolean)
                                .join(" · ")}
                            </span>
                          </div>
                          <${Specimen}
                            path=${preview}
                            text=${text}
                            size=${size}
                          />
                        </div>
                      `;
                    })}
                  </div>
                `}
          </div>
        </main>

        ${selected &&
        html`
          <${Detail}
            family=${results.find((f) => f.id === selected) ||
            families.find((f) => f.id === selected)}
            text=${text}
            size=${size}
            onClose=${() => setSelected(null)}
          />
        `}
      </div>
    </div>
  `;
}

// ------------------------------------------------------------------ bootstrap

fetch("manifest.json")
  .then((r) => {
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  })
  .then((manifest) => {
    fontBase = new URL(`${manifest.root}/`, location.href);
    buildIndex(manifest);
    render(
      html`
        <${App} manifest=${manifest} />
      `,
      document.getElementById("app"),
    );
  })
  .catch((err) => {
    document.getElementById("app").innerHTML =
      `<div class="boot">could not load manifest.json — ${err.message}<br><br>` +
      `run <code>python3 _scripts/build-manifest.py</code>, then serve with ` +
      `<code>python3 _scripts/serve.py</code>.</div>`;
  });
