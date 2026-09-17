// Minimal store-only ZIP writer. No compression: font files barely shrink and
// deflate would mean pulling in a dependency, which this app does not have.
//
//   buildZip([{ name, bytes }, ...]) -> Blob
//   saveBlob(blob, "Agenda.zip")

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) {
    c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

/** MS-DOS packed date and time, the only timestamp a basic ZIP carries. */
function dosStamp(date) {
  const year = Math.max(1980, date.getFullYear());
  return {
    time:
      (date.getHours() << 11) |
      (date.getMinutes() << 5) |
      (Math.floor(date.getSeconds() / 2) & 0x1f),
    date: ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
  };
}

function writer(size) {
  const bytes = new Uint8Array(size);
  const view = new DataView(bytes.buffer);
  let at = 0;
  return {
    bytes,
    u16(v) {
      view.setUint16(at, v, true);
      at += 2;
    },
    u32(v) {
      view.setUint32(at, v >>> 0, true);
      at += 4;
    },
    raw(src) {
      bytes.set(src, at);
      at += src.length;
    },
  };
}

/**
 * @param {{name: string, bytes: Uint8Array, modified?: Date}[]} entries
 * @returns {Blob}
 */
export function buildZip(entries) {
  const encoder = new TextEncoder();
  const parts = [];
  const central = [];
  let offset = 0;

  entries.forEach((entry) => {
    const name = encoder.encode(entry.name);
    const crc = crc32(entry.bytes);
    const stamp = dosStamp(entry.modified || new Date());
    const size = entry.bytes.length;

    const local = writer(30 + name.length);
    local.u32(0x04034b50); // local file header
    local.u16(20); // version needed
    local.u16(0x0800); // UTF-8 filenames
    local.u16(0); // stored, no compression
    local.u16(stamp.time);
    local.u16(stamp.date);
    local.u32(crc);
    local.u32(size); // compressed
    local.u32(size); // uncompressed
    local.u16(name.length);
    local.u16(0); // extra field length
    local.raw(name);

    parts.push(local.bytes, entry.bytes);

    const dir = writer(46 + name.length);
    dir.u32(0x02014b50); // central directory header
    dir.u16(20); // version made by
    dir.u16(20); // version needed
    dir.u16(0x0800);
    dir.u16(0);
    dir.u16(stamp.time);
    dir.u16(stamp.date);
    dir.u32(crc);
    dir.u32(size);
    dir.u32(size);
    dir.u16(name.length);
    dir.u16(0); // extra
    dir.u16(0); // comment
    dir.u16(0); // disk number
    dir.u16(0); // internal attrs
    dir.u32(0); // external attrs
    dir.u32(offset);
    dir.raw(name);
    central.push(dir.bytes);

    offset += local.bytes.length + size;
  });

  const centralSize = central.reduce((n, b) => n + b.length, 0);
  const end = writer(22);
  end.u32(0x06054b50); // end of central directory
  end.u16(0); // this disk
  end.u16(0); // disk with central directory
  end.u16(entries.length);
  end.u16(entries.length);
  end.u32(centralSize);
  end.u32(offset);
  end.u16(0); // comment length

  return new Blob([...parts, ...central, end.bytes], {
    type: "application/zip",
  });
}

/** Hand a URL to the browser as a download, without fetching it first. */
export function saveUrl(url, filename) {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/** Strip anything a filesystem would object to. */
export function safeName(text) {
  return (
    (text || "fonts")
      .replace(/[\\/:*?"<>|\x00-\x1f]/g, "-")
      .replace(/\s+/g, " ")
      .trim() || "fonts"
  );
}
