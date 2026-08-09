import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execSync, spawn } from 'node:child_process';
import { chromium } from 'playwright';
import {
  DEFAULTS as SHARED_DEFAULTS,
  LAYOUT,
  normalizeSubtitleSegments,
  buildSubtitleStateTimeline,
} from '../../shared/subtitle-layout/index.js';

const [,, inJsonPath, outPath] = process.argv;
if (!inJsonPath || !outPath) {
  console.error('Usage: node render-css.mjs <input.json> <out.mp4>');
  process.exit(1);
}

const input = JSON.parse(await fs.readFile(inJsonPath, 'utf8'));
const timeStart = Date.now();
const width = Number(input.width || 1280);
const height = Number(input.height || 720);
const fps = Number(input.fps || 30);
const fontSize = Number(input.fontSize || 36);
const subtitleStyle = String(input.subtitleStyle || 'bg-dark');
const enableHighlight = Boolean(input.enableHighlight);
const bgOpacity = Math.max(0, Math.min(100, Number(input.bgOpacity || 68)));
const audioPath = input.audioPath;
const slidePath = input.slidePath;
const normalized = normalizeSubtitleSegments(input.segments || []);
const isQwen = String(input.alignBackend || '').toLowerCase().includes('qwen3');

// ── Probe audio duration ─────────────────────────────────────────────────────
const probe = JSON.parse(execSync(
  `ffprobe -v quiet -print_format json -show_format ${JSON.stringify(audioPath)}`,
  { encoding: 'utf8' }
));
const audioDuration = Number(probe?.format?.duration || 1);

// ── Subtitle state timeline ──────────────────────────────────────────────────
const states = buildSubtitleStateTimeline({
  normalizedSegments: normalized,
  durationSec: audioDuration,
  enableHighlight,
  isQwenBackend: isQwen,
  forceWordTimeline: true,
  highlightLeadSec: SHARED_DEFAULTS.HIGHLIGHT_LEAD_SEC,
  segmentLingerSec: SHARED_DEFAULTS.SEGMENT_LINGER_SEC,
  minWordMs: SHARED_DEFAULTS.DEFAULT_SUBTITLE_MIN_WORD_MS,
  toleranceSec: SHARED_DEFAULTS.SUBTITLE_ACTIVE_TOLERANCE_SEC,
});

// ── Strip height ─────────────────────────────────────────────────────────────
const stripHeight = Math.max(160, Math.round(height * 0.24));
const stateDir = await fs.mkdtemp(path.join(os.tmpdir(), 'slideai-playwright-'));

console.error(`[RENDER] Playwright CSS renderer started. Width: ${width}, Height: ${height}, Strip Height: ${stripHeight}`);

// ── Playwright Capture ───────────────────────────────────────────────────────
const browser = await chromium.launch({
  args: [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-animations',
    '--disable-web-security'
  ]
});

const page = await browser.newPage({
  viewport: { width, height },
  deviceScaleFactor: 1, // Ensure 1:1 pixel mapping
});

// Navigate to the headless variant with query params to set styles
// Assuming Vite is running on localhost:5174
const hlUrl = `http://localhost:5174/subtitle-playwright-variant?headless=1&style=${encodeURIComponent(subtitleStyle)}&fz=${fontSize}&alpha=${bgOpacity}&hl=${enableHighlight ? 1 : 0}`;
console.error(`[RENDER] Navigating to: ${hlUrl}`);
await page.goto(hlUrl, { waitUntil: 'networkidle' });

// Inject CSS to firmly disable animations just in case
await page.addStyleTag({
  content: `
    * {
      transition: none !important;
      animation: none !important;
    }
    html, body, #app { background-color: transparent !important; background: transparent !important; }
  `
});

console.error(`[RENDER] Waiting for FONTS_READY...`);
await page.waitForFunction(() => window.__FONTS_READY === true, { timeout: 15000 }).catch(e => {
  console.error(`[RENDER] Warning: FONTS_READY wait timed out. Check local fonts.`);
});

let stateIdx = 0;
const concatLines = [];

const clipRegion = {
  x: 0,
  y: height - stripHeight,
  width: width,
  height: stripHeight
};

const captureStart = Date.now();
for (const st of states) {
  const duration = Math.max(0.001, Number(st.end) - Number(st.start));
  const pieces = Array.isArray(st.pieces) ? st.pieces : [];

  // Update state in DOM
  await page.evaluate((data) => {
    window.__DOM_UPDATED = false;
    window.setSubtitleState(data);
  }, pieces);

  // Wait for Vue to finish DOM update
  await page.waitForFunction(() => window.__DOM_UPDATED === true);

  const statePath = path.join(stateDir, `state_${String(stateIdx).padStart(6, '0')}.png`);

  // Screenshot only the bottom strip with omitBackground to preserve transparency
  await page.screenshot({
    path: statePath,
    omitBackground: true,
    clip: clipRegion
  });

  concatLines.push(`file '${statePath.replace(/'/g, "'\\''")}'`);
  concatLines.push(`duration ${duration.toFixed(6)}`);
  stateIdx++;
}

await browser.close();
console.error(`[RENDER] Capture done: ${Date.now() - captureStart}ms, ${stateIdx} states`);

// Repeat last frame (required by ffmpeg concat demuxer)
if (stateIdx > 0) {
  const lastPath = path.join(stateDir, `state_${String(stateIdx - 1).padStart(6, '0')}.png`);
  concatLines.push(`file '${lastPath.replace(/'/g, "'\\''")}'`);
}
const concatPath = path.join(stateDir, 'states.txt');
await fs.writeFile(concatPath, concatLines.join('\n') + '\n', 'utf8');

// ── Background pre-decode (rawvideo yuv420p) ─────────────────────────────────
const bgRawPath = path.join(stateDir, 'bg.yuv420p');
let useBgRaw = false;
try {
  execSync(
    ['ffmpeg', '-y', '-i', slidePath, '-pix_fmt', 'yuv420p', '-vframes', '1', bgRawPath]
      .map((a) => JSON.stringify(a)).join(' ') + ' 2>/dev/null',
    { encoding: 'utf8' },
  );
  useBgRaw = true;
  console.error('[RENDER] BG rawvideo pre-decode OK');
} catch {
  console.error('[RENDER] BG rawvideo pre-decode failed – will use -loop 1');
}

// ── Encoder selection ────────────────────────────────────────────────────────
let encoder = 'libx264';
try {
  const cl = execSync('ffmpeg -codecs 2>&1', { encoding: 'utf8' });
  if (cl.includes('h264_nvenc')) encoder = 'h264_nvenc';
  else if (cl.includes('h264_qsv')) encoder = 'h264_qsv';
} catch {}
console.error(`[RENDER] Encoder: ${encoder}`);

// ── FFmpeg compose ───────────────────────────────────────────────────────────
const buildArgs = (enc) => [
  '-y',
  ...(useBgRaw
    ? ['-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', `${width}x${height}`, '-i', bgRawPath]
    : ['-loop', '1', '-i', slidePath]),
  '-f', 'concat', '-safe', '0', '-i', concatPath,
  '-i', audioPath,
  '-filter_complex',
    `[1:v]format=rgba[ov];[0:v][ov]overlay=0:${height - stripHeight}:shortest=1[v]`,
  '-map', '[v]', '-map', '2:a',
  ...(enc === 'h264_nvenc'
    ? ['-c:v', 'h264_nvenc', '-preset', 'fast', '-rc', 'vbr', '-b:v', '5M']
    : enc === 'h264_qsv'
    ? ['-c:v', 'h264_qsv', '-preset', 'fast', '-b:v', '5M']
    : ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage', '-crf', '22']),
  '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', '-shortest',
  outPath,
];

const encStart = Date.now();
await new Promise((resolve, reject) => {
  const run = (enc) => {
    const p = spawn('ffmpeg', buildArgs(enc), { stdio: 'pipe' });
    let err = '';
    p.stderr.on('data', (d) => { err += d.toString(); });
    p.on('close', (code) => {
      if (code === 0) {
        console.error(`[RENDER] FFmpeg done: ${Date.now() - encStart}ms (${enc})`);
        resolve();
      } else if (enc !== 'libx264') {
        console.error(`[RENDER] ${enc} failed, fallback to libx264`);
        run('libx264');
      } else {
        reject(new Error(`ffmpeg failed: ${err.slice(-600)}`));
      }
    });
  };
  run(encoder);
});

// ── Cleanup ──────────────────────────────────────────────────────────────────
try {
  for (const f of await fs.readdir(stateDir)) await fs.unlink(path.join(stateDir, f)).catch(() => {});
  await fs.rmdir(stateDir).catch(() => {});
} catch {}

const total = Date.now() - timeStart;
console.log(JSON.stringify({ ok: true, renderer: 'playwright-css', states: stateIdx, width, height, elapsedMs: total }));
console.error(`[RENDER] DONE total=${total}ms, states=${stateIdx}, avg=${(total / Math.max(1, stateIdx)).toFixed(1)}ms/state`);
