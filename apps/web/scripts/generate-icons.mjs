#!/usr/bin/env node
/**
 * Renders the Arena64 application icons into `public/icons/`.
 *
 * ## Why a script rather than committed artwork alone
 *
 * A64-020.9 §6 asks for reproducible source asset generation. A PNG in a
 * repository is a number nobody can re-derive: when the mark changes, the
 * only honest way to regenerate every size at the same crispness is to
 * regenerate them all from one description. That description is this file.
 *
 * ## Why no image library
 *
 * `sharp`, `canvas` and `resvg` are all native builds — a compiler on
 * every machine that ever runs this, for a mark made of two rectangles and
 * a circle. `node:zlib` is the only thing a PNG encoder actually needs
 * (CLAUDE.md §2.6), so this writes the format directly: 8-bit RGBA,
 * filter 0, one `IDAT`. Roughly sixty lines, and it has no version to
 * update.
 *
 * ## The mark itself is a placeholder, and says so
 *
 * A 2×2 draughts board with one piece on it, in the neutral palette
 * `app/styles/globals.css` already establishes. **A64-025 Product
 * Experience Redesign owns the real brand**; this exists so that the
 * manifest is installable and the home-screen tile is not a stretched
 * screenshot. `specs/frontend.md` §20 records that.
 *
 * Usage: `npm run assets:icons`
 */
import { deflateSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "public", "icons");

/** The neutral palette, as `globals.css` states it in oklch. */
const BACKGROUND = [10, 10, 10, 255]; // oklch(0.145 0 0)
const LIGHT = [250, 250, 250, 255]; // oklch(0.985 0 0)
const DARK_SQUARE = [38, 38, 38, 255];

/**
 * Supersampling factor. The mark is drawn at 4× and box-filtered down, so
 * the circle and the rounded corners get antialiasing without a rasteriser.
 */
const SUPERSAMPLE = 4;

/**
 * The mark, in normalised coordinates over the icon's square.
 *
 * `inset` is what makes the maskable variant different from the plain one
 * and the only thing that differs: a maskable icon may be cropped to a
 * circle of 80% diameter, so the board has to sit inside the middle ~60%
 * with background bleeding to every edge.
 */
function markColorAt(x, y, inset) {
  const size = 1 - inset * 2;
  const bx = (x - inset) / size;
  const by = (y - inset) / size;
  if (bx < 0 || bx > 1 || by < 0 || by > 1) return BACKGROUND;

  // The tile's rounded corners, in board-local space.
  const radius = 0.14;
  const cx = Math.min(Math.max(bx, radius), 1 - radius);
  const cy = Math.min(Math.max(by, radius), 1 - radius);
  if (Math.hypot(bx - cx, by - cy) > radius) return BACKGROUND;

  const column = bx < 0.5 ? 0 : 1;
  const row = by < 0.5 ? 0 : 1;
  const isLightSquare = (row + column) % 2 === 0;

  // The piece: on the dark square at bottom-left, so it reads against it.
  const pieceCenter = { x: 0.25, y: 0.75 };
  if (Math.hypot(bx - pieceCenter.x, by - pieceCenter.y) <= 0.165) return LIGHT;

  return isLightSquare ? LIGHT : DARK_SQUARE;
}

/** Renders one square icon as raw RGBA bytes. */
function renderRgba(size, inset) {
  const pixels = Buffer.alloc(size * size * 4);
  const step = 1 / (size * SUPERSAMPLE);

  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let sy = 0; sy < SUPERSAMPLE; sy += 1) {
        for (let sx = 0; sx < SUPERSAMPLE; sx += 1) {
          const px = (x * SUPERSAMPLE + sx + 0.5) * step;
          const py = (y * SUPERSAMPLE + sy + 0.5) * step;
          const [cr, cg, cb, ca] = markColorAt(px, py, inset);
          r += cr;
          g += cg;
          b += cb;
          a += ca;
        }
      }
      const samples = SUPERSAMPLE * SUPERSAMPLE;
      const offset = (y * size + x) * 4;
      pixels[offset] = Math.round(r / samples);
      pixels[offset + 1] = Math.round(g / samples);
      pixels[offset + 2] = Math.round(b / samples);
      pixels[offset + 3] = Math.round(a / samples);
    }
  }
  return pixels;
}

function chunk(type, body) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(body.length);
  const typed = Buffer.concat([Buffer.from(type, "ascii"), body]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(typed));
  return Buffer.concat([length, typed, crc]);
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buffer) {
  let c = 0xffffffff;
  for (const byte of buffer) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function encodePng(size, pixels) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8; // bit depth
  header[9] = 6; // colour type: RGBA
  // 10, 11, 12 stay zero: deflate, adaptive filtering, no interlace.

  // One filter byte (0 = None) per scanline, then the scanline itself.
  const raw = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y += 1) {
    pixels.copy(raw, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4);
  }

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    // Level 9 so the committed bytes are stable and small — these are
    // build inputs, not something regenerated per request.
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/**
 * Every icon this product ships, and what each one is for.
 *
 * `any` and `maskable` are separate files rather than one file claiming
 * both purposes: a launcher that masks an `any` icon crops the board, and
 * a browser that renders a `maskable` icon un-masked shows a mark floating
 * in a field of padding. Both look like a mistake, and both are avoided by
 * having two.
 */
const ICONS = [
  { file: "icon-192.png", size: 192, inset: 0.06 },
  { file: "icon-512.png", size: 512, inset: 0.06 },
  { file: "icon-maskable-512.png", size: 512, inset: 0.2 },
  // 180 is what iOS asks for and the only size it reads.
  { file: "apple-touch-icon.png", size: 180, inset: 0.06 },
];

mkdirSync(OUT_DIR, { recursive: true });
for (const { file, size, inset } of ICONS) {
  const png = encodePng(size, renderRgba(size, inset));
  writeFileSync(join(OUT_DIR, file), png);
  console.log(`${file.padEnd(24)} ${size}×${size}  ${png.length} bytes`);
}
