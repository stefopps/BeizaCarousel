#!/usr/bin/env node
/**
 * Headless batch: session JSON → MP4 with cover + audio + burned-in captions (ASS).
 * Matches the web app: **emphasis**, boost words, accent colour — same logic as
 * buildAssForFfmpegPack() in index.html (libass via FFmpeg). No browser, no Playwright.
 *
 * Prerequisites: Node.js 18+, FFmpeg with libass on PATH
 *
 * Usage:
 *   node batch-json-to-mp4.cjs --input ./my-jsons [--out <dir>]
 *   node batch-json-to-mp4.cjs [--cover cover.jpg] a.json b.json [--out <dir>]
 *   node batch-json-to-mp4.cjs session.json --no-subs   (video + audio only)
 *
 * Default cover: <script-dir>/Saved sessions/Cover.jpg
 */

'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

const DEFAULT_COVER = path.join(__dirname, 'Saved sessions', 'Cover.jpg');

function isCounterContentSlide(s) {
  return (
    s &&
    s.type === 'content' &&
    s.showCounter !== false &&
    s.slide3symbol !== true &&
    s.slide4photo === undefined &&
    s.slide5symbol !== true
  );
}

function parseSrtMs(t) {
  if (!t) return 0;
  const p = String(t).trim().replace(',', '.').split(':');
  if (p.length < 3) return 0;
  const sec = parseFloat(p[2]);
  if (isNaN(sec)) return 0;
  return (parseInt(p[0], 10) * 3600 + parseInt(p[1], 10) * 60 + sec) * 1000;
}

function parseSrtToCues(srt) {
  const cues = [];
  const raw = String(srt || '')
    .replace(/\r\n/g, '\n')
    .trim();
  if (!raw) return cues;
  const blocks = raw.split(/\n\n+/);
  for (let bi = 0; bi < blocks.length; bi++) {
    const lines = blocks[bi].split('\n');
    if (lines.length < 2) continue;
    let li = 0;
    if (/^\d+$/.test(lines[0].trim())) li = 1;
    if (li >= lines.length) continue;
    const m = lines[li].match(/(\d\d:\d\d:\d\d[,.]\d{3})\s*-->\s*(\d\d:\d\d:\d\d[,.]\d{3})/);
    if (!m) continue;
    const text = lines.slice(li + 1).join('\n').trim();
    cues.push({ start: parseSrtMs(m[1]), end: parseSrtMs(m[2]), text });
  }
  return cues;
}

function applyCaptionBoostWords(raw, boostCsv) {
  if (!boostCsv || !String(boostCsv).trim()) return String(raw || '');
  const terms = String(boostCsv)
    .split(/[,;]+/)
    .map(function (x) {
      return x.trim();
    })
    .filter(Boolean)
    .sort(function (a, b) {
      return b.length - a.length;
    });
  let str = String(raw || '');
  terms.forEach(function (term) {
    const esc = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    str = str.replace(new RegExp('\\b(' + esc + ')\\b', 'gi'), function (match, word, offset, s) {
      const pre = s.slice(Math.max(0, offset - 2), offset);
      const post = s.slice(offset + match.length, offset + match.length + 2);
      if (/\*/.test(pre) || /\*/.test(post)) return match;
      return '**' + word + '**';
    });
  });
  return str;
}

function getExportPixelSize(fmt, globals) {
  const EX = { sq: [1080, 1080], port: [864, 1080], st: [607, 1080] };
  const [EW0, EH0] = EX[fmt] || EX.sq;
  const dpi = globals && globals.exportDpi != null ? globals.exportDpi : 150;
  const m = dpi / 72;
  return { EW: Math.round(EW0 * m), EH: Math.round(EH0 * m) };
}

function hexToAssColour(hex) {
  const m = String(hex || '')
    .trim()
    .match(/^#?([0-9A-Fa-f]{6})$/);
  if (!m) return '&H00FFFFFF';
  const r = parseInt(m[1].slice(0, 2), 16);
  const g = parseInt(m[1].slice(2, 4), 16);
  const b = parseInt(m[1].slice(4, 6), 16);
  return (
    '&H00' +
    [b, g, r]
      .map(function (x) {
        return x.toString(16).padStart(2, '0');
      })
      .join('')
      .toUpperCase()
  );
}

const ASS_GOLD_EMPH = hexToAssColour('#C8905A');

function msToAssTime(ms) {
  if (!isFinite(ms) || ms < 0) ms = 0;
  const cs = Math.floor(ms / 10) % 100;
  let t = Math.floor(ms / 1000);
  const s = t % 60;
  t = Math.floor(t / 60);
  const m = t % 60;
  const h = Math.floor(t / 60);
  return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0') + '.' + String(cs).padStart(2, '0');
}

function escapeAssText(s) {
  return String(s || '')
    .replace(/\\/g, '\\\\')
    .replace(/\{/g, '\\{')
    .replace(/\}/g, '\\}');
}

const ASS_BASE_FS = 18;

function captionCueToAssLine(raw, baseFs, emScalePct, boostCsv) {
  const em = Math.max(1, (emScalePct != null ? emScalePct : 130) / 100);
  const emFs = Math.round(baseFs * em);
  const lines = String(raw || '')
    .replace(/<[^>]+>/g, '')
    .split(/\n/);
  return lines
    .map(function (line) {
      const t = applyCaptionBoostWords(line, boostCsv);
      const parts = t.split(/(\*\*[^*]+\*\*)/g);
      let out = '';
      parts.forEach(function (p) {
        const mm = p.match(/^\*\*([^*]+)\*\*$/);
        if (mm) {
          out += '{\\fs' + emFs + '}{\\c' + ASS_GOLD_EMPH + '}' + escapeAssText(mm[1]) + '{\\r}';
        } else {
          out += escapeAssText(p);
        }
      });
      return out;
    })
    .join('\\N');
}

/** Same as index.html getSlideMedia — caption fields only (audio not needed for ASS). */
function getSlideMediaForCaptions(slide) {
  if (slide.slide3symbol) {
    if (slide.slide3ShowMedia !== true) return null;
    const cues =
      slide.slide3SrtCues && slide.slide3SrtCues.length
        ? slide.slide3SrtCues
        : parseSrtToCues(slide.slide3Srt);
    const tB = slide.slide3TitleBottomPct != null ? slide.slide3TitleBottomPct : 8;
    let nudge = slide.slide3CaptionNudgePct;
    if (nudge == null || nudge === undefined) nudge = 6;
    const bot = Math.min(30, Math.max(0.5, tB + nudge));
    return {
      srt: slide.slide3Srt,
      srtCues: cues,
      bottom: bot,
      captionLight: true,
      captionScale: slide.slide3CaptionScale != null ? slide.slide3CaptionScale : 100,
      captionEmPct: slide.slide3CaptionEmPct != null ? slide.slide3CaptionEmPct : 130,
      captionBoostCsv: slide.slide3CaptionBoostWords,
    };
  }
  if (isCounterContentSlide(slide)) {
    if (slide.cntMediaShow !== true) return null;
    const cues =
      slide.cntMediaSrtCues && slide.cntMediaSrtCues.length
        ? slide.cntMediaSrtCues
        : parseSrtToCues(slide.cntMediaSrt);
    return {
      srt: slide.cntMediaSrt,
      srtCues: cues,
      bottom: slide.cntMediaBottomPct != null ? slide.cntMediaBottomPct : 4,
      captionLight: slide.counterTextWhite === false,
      captionScale: slide.cntCaptionScale != null ? slide.cntCaptionScale : 100,
      captionEmPct: slide.cntCaptionEmPct != null ? slide.cntCaptionEmPct : 130,
      captionBoostCsv: slide.cntCaptionBoostWords,
    };
  }
  return null;
}

function buildAssForFfmpegPack(s, m, fmt, globals) {
  const cues = m.srtCues && m.srtCues.length ? m.srtCues : parseSrtToCues(m.srt);
  const sz = getExportPixelSize(fmt || 'sq', globals);
  const EW = sz.EW;
  const EH = sz.EH;
  const marginV = Math.max(24, Math.round(EH * ((m.bottom != null ? m.bottom : 6) / 100)));
  const emPct = m.captionEmPct != null ? m.captionEmPct : 130;
  const boost = m.captionBoostCsv;
  const di = cues
    .map(function (c) {
      const txt = captionCueToAssLine(c.text, ASS_BASE_FS, emPct, boost);
      return 'Dialogue: 0,' + msToAssTime(c.start) + ',' + msToAssTime(c.end) + ',Default,,0,0,0,,' + txt;
    })
    .join('\r\n');
  const sty =
    'Style: Default,Alliance,' +
    ASS_BASE_FS +
    ',&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,3,3,0,2,48,48,' +
    marginV +
    ',1';
  return [
    '[Script Info]',
    'Title: Beiza captions',
    'ScriptType: v4.00+',
    'PlayResX: ' + EW,
    'PlayResY: ' + EH,
    'WrapStyle: 0',
    'ScaledBorderAndShadow: yes',
    '',
    '[V4+ Styles]',
    'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
    sty,
    '',
    '[Events]',
    'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
    di,
  ].join('\r\n');
}

function findEmbeddedAudio(state) {
  const slides = state.slides || [];
  for (let i = 0; i < slides.length; i++) {
    const s = slides[i];
    if (s.slide3symbol === true && s.slide3ShowMedia === true && s.slide3AudioData) {
      return { slideIndex: i, dataUrl: s.slide3AudioData };
    }
    if (isCounterContentSlide(s) && s.cntMediaShow === true && s.cntMediaAudio) {
      return { slideIndex: i, dataUrl: s.cntMediaAudio };
    }
  }
  return null;
}

function parseDataUrl(dataUrl) {
  const m = String(dataUrl).match(/^data:([^;]+);base64,(.+)$/s);
  if (!m) return null;
  return { mime: m[1].split(';')[0].trim(), buf: Buffer.from(m[2], 'base64') };
}

function extFromMime(mime) {
  const m = String(mime).toLowerCase();
  if (m.includes('mpeg') || m === 'audio/mp3') return 'mp3';
  if (m.includes('mp4') || m.includes('m4a') || m.includes('aac')) return 'm4a';
  if (m.includes('wav')) return 'wav';
  if (m.includes('ogg')) return 'ogg';
  return 'm4a';
}

function parseArgs(argv) {
  const o = { cover: null, inputDir: null, outDir: 'mp4-out', jsonFiles: [], noSubs: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--cover' && argv[i + 1]) {
      o.cover = argv[++i];
    } else if (a === '--input' && argv[i + 1]) {
      o.inputDir = argv[++i];
    } else if ((a === '--out' || a === '--output') && argv[i + 1]) {
      o.outDir = argv[++i];
    } else if (a === '--no-subs') {
      o.noSubs = true;
    } else if (a === '--help' || a === '-h') {
      o.help = true;
    } else if (a.endsWith('.json')) {
      o.jsonFiles.push(a);
    }
  }
  return o;
}

function collectJsonFiles(inputDir) {
  const abs = path.resolve(inputDir);
  if (!fs.existsSync(abs) || !fs.statSync(abs).isDirectory()) {
    throw new Error('Not a directory: ' + abs);
  }
  return fs
    .readdirSync(abs)
    .filter((f) => f.toLowerCase().endsWith('.json'))
    .map((f) => path.join(abs, f));
}

/**
 * Mux in a temp dir so ass=captions.ass needs no drive-letter escaping.
 */
function ffmpegToMp4Headless(coverPath, audioFileName, muxDir, outPath, useAss) {
  const args = [
    '-y',
    '-loop',
    '1',
    '-i',
    path.resolve(coverPath),
    '-i',
    audioFileName,
  ];
  if (useAss) {
    args.push('-vf', 'ass=captions.ass');
  }
  args.push(
    '-c:v',
    'libx264',
    '-tune',
    'stillimage',
    '-pix_fmt',
    'yuv420p',
    '-c:a',
    'aac',
    '-b:a',
    '192k',
    '-shortest',
    path.resolve(outPath)
  );
  const r = spawnSync('ffmpeg', args, { cwd: muxDir, encoding: 'utf8', shell: false });
  if (r.status !== 0) {
    const err = (r.stderr || r.stdout || '').trim();
    throw new Error('ffmpeg failed (' + r.status + '): ' + (err.slice(-1200) || 'no stderr'));
  }
}

function main() {
  const o = parseArgs(process.argv);
  const hasWork = o.jsonFiles.length > 0 || o.inputDir;
  if (o.help || !hasWork) {
    console.log(`
Usage:
  node batch-json-to-mp4.cjs [--cover <cover.jpg>] --input <dir> [--out <dir>]
  node batch-json-to-mp4.cjs [--cover <cover.jpg>] a.json b.json [--out <dir>]

  Headless: FFmpeg burns in captions.ass (**emphasis** + accent) like the web app.
  --no-subs   Video + audio only (no burned captions).

  Default cover: ${DEFAULT_COVER}
`);
    process.exit(o.help ? 0 : 1);
  }
  let coverPath;
  if (o.cover) {
    coverPath = path.resolve(o.cover);
    if (!fs.existsSync(coverPath)) {
      console.error('Cover not found: ' + coverPath);
      process.exit(1);
    }
  } else if (fs.existsSync(DEFAULT_COVER)) {
    coverPath = path.resolve(DEFAULT_COVER);
    console.log('Using default cover: ' + coverPath);
  } else {
    console.error(
      'No --cover and default missing:\n  ' + DEFAULT_COVER + '\nPass --cover <file> or add Cover.jpg there.'
    );
    process.exit(1);
  }
  let files = o.jsonFiles.slice();
  if (o.inputDir) {
    files = files.concat(collectJsonFiles(o.inputDir));
  }
  files = [...new Set(files.map((f) => path.resolve(f)))].filter((f) => fs.existsSync(f));
  if (!files.length) {
    console.error('No JSON files.');
    process.exit(1);
  }

  const outDir = path.resolve(o.outDir);
  fs.mkdirSync(outDir, { recursive: true });

  let ok = 0;
  let skip = 0;

  for (const jsonPath of files) {
    const base = path.basename(jsonPath, path.extname(jsonPath));
    const outMp4 = path.join(outDir, base + '.mp4');
    let state;
    try {
      state = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
    } catch (e) {
      console.warn('[skip] ' + jsonPath + ' — invalid JSON: ' + e.message);
      skip++;
      continue;
    }
    const found = findEmbeddedAudio(state);
    if (!found) {
      console.warn('[skip] ' + jsonPath + ' — no embedded audio.');
      skip++;
      continue;
    }
    const parsed = parseDataUrl(found.dataUrl);
    if (!parsed) {
      console.warn('[skip] ' + jsonPath + ' — audio is not a data URL.');
      skip++;
      continue;
    }
    const ext = extFromMime(parsed.mime);
    const slide = state.slides[found.slideIndex];
    const m = getSlideMediaForCaptions(slide);
    const hasSrt = m && String(m.srt || '').trim();
    const useAss = !o.noSubs && hasSrt;

    const muxDir = fs.mkdtempSync(path.join(os.tmpdir(), 'beiza-mux-'));
    const trackName = 'track.' + ext;
    try {
      fs.writeFileSync(path.join(muxDir, trackName), parsed.buf);
      if (useAss) {
        const ass = buildAssForFfmpegPack(slide, m, state.fmt, state.globals);
        fs.writeFileSync(path.join(muxDir, 'captions.ass'), ass, 'utf8');
      }
      const label = useAss ? 'ass subs' : 'no subs';
      console.log('[mux] ' + base + ' (slide ' + (found.slideIndex + 1) + ', ' + label + ') → ' + outMp4);
      ffmpegToMp4Headless(coverPath, trackName, muxDir, outMp4, useAss);
      ok++;
    } catch (e) {
      console.error('[fail] ' + jsonPath + ' — ' + e.message);
      skip++;
    } finally {
      try {
        fs.rmSync(muxDir, { recursive: true, force: true });
      } catch (_) {}
    }
  }

  console.log('\nDone. Wrote ' + ok + ' MP4(s) to ' + outDir + '. Skipped/failed: ' + skip + '.');
  process.exit(skip && !ok ? 1 : 0);
}

main();
