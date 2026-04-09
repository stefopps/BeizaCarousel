#!/usr/bin/env python3
"""
Batch: Carousel → MP4s in mp4-out/

1) Spreadsheet (default): variants.csv + base_session.json (exported from index.html) in
   this folder; assets under ../covers/, ../audio/, cached SRT in ../srt/. Each row merges
   text into the base session; SRT priority: CSV column → srt/<symbolName>.srt → Whisper.
   Run: python make_videos.py   (needs base_session.json; without it, falls back to legacy)
       python make_videos.py --transcribe-only
       python make_videos.py --legacy-folder
       python make_videos.py --studio   (Tk: preview first caption line, scale/position → save JSON)
   Template caption layout is also saved as caption_layout_preset.json; batch applies it to
   base_session before each CSV row (row fields can still override).

2) Legacy: Cover.jpg + *.json sessions in this folder (embedded audio data URLs).

ASS matches batch-json-to-mp4.cjs / index.html (PlayRes, Alliance, margins, boost words).

Requires: FFmpeg on PATH (libx264 + libass). Optional: whisper CLI for transcription.

Import process_session_folder or process_csv_pipeline for the GUI.

Caption studio (`python make_videos.py --studio`)
    Tk preview for caption size / bottom margin / boost. Renders the first caption line with
    the same rules as index.html: boost-word list + **markers → heavier face (Alliance regular
    + bold when those fonts exist; else Segoe UI / Arial). FFmpeg ASS uses the same ** logic
    with Alliance + {\\b1} emphasis as the browser export.
    Saves session JSON + optional caption_layout_preset.json (see above).

    Slider responsiveness (debounce)
        Style sliders use tkinter variable ``trace_add`` + ``after_idle`` by default so many
        updates in one UI batch collapse to a single redraw. Tune without editing logic by
        setting ``STUDIO_STYLE_DEBOUNCE_MS`` below: ``None`` keeps ``after_idle``; set an int
        (e.g. ``16``) to use a timed debounce in milliseconds instead. Implementation:
        ``CaptionStudio._on_style_slider_trace``.

    Elastic land: Tk preview + FFmpeg ASS (\\an2\\move, same easing as studio)
        Checkbox + optional “Replay land”; cue slider fires on mouse release when enabled.
        Tune motion via ``STUDIO_ELASTIC_LAND_STEPS`` and ``STUDIO_ELASTIC_FRAME_MS`` below
        (more steps = smoother; higher ms = slower). Does not affect batch MP4 output.
"""

from __future__ import annotations

# Caption studio: optional fixed debounce for style sliders (ms). None = use after_idle only.
STUDIO_STYLE_DEBOUNCE_MS: int | None = None
# Elastic land: studio preview stepping + FFmpeg ASS duration (steps × frame_ms).
STUDIO_ELASTIC_LAND_STEPS = 14
STUDIO_ELASTIC_FRAME_MS = 26
ELASTIC_LAND_MS = float(STUDIO_ELASTIC_LAND_STEPS * STUDIO_ELASTIC_FRAME_MS)

import argparse
import base64
import copy
import csv
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
CAROUSEL_ROOT = SCRIPT_DIR.parent
CAROUSEL_FONTS_DIR = CAROUSEL_ROOT / "fonts"
# PostScript / typographic family inside fonts/alliance-no-1-*.ttf (libass matches this name).
ASS_FONT_FAMILY = "Alliance No.1"
ALLIANCE_FONT_FILES = (
    "alliance-no-1-light.ttf",
    "alliance-no-1-bold.ttf",
    "alliance-no-1-black.ttf",
)
COVER_NAME = "Cover.jpg"
OUT_SUBDIR = "mp4-out"
DEFAULT_VARIANTS_CSV = SCRIPT_DIR / "variants.csv"
DEFAULT_BASE_SESSION = SCRIPT_DIR / "base_session.json"
# Written by `python make_videos.py --studio` (Save). Python batch applies these slide3
# layout keys to the base session before each CSV row merge so encodes stay consistent.
CAPTION_LAYOUT_PRESET = SCRIPT_DIR / "caption_layout_preset.json"

CAPTION_LAYOUT_KEYS = (
    "slide3CaptionScale",
    "slide3TitleBottomPct",
    "slide3CaptionNudgePct",
    "slide3CaptionEmPct",
    "slide3CaptionElasticLand",
    "slide3CaptionColor",
    "slide3CaptionKaraokeColor",
)

# Base font size as a fraction of PlayResY — scales correctly at any DPI/resolution.
# 3.1% of height ≈ 34px at 1080px tall, ≈ 93px at 3000px tall (200 DPI portrait).
ASS_BASE_FS_PCT = 0.031

# index.html: SAND_CTA / TERRA_FALLBACK + TC + outroTagPillBgResolve → captionAccentColor(s)
SAND_CTA = "#C8905A"
TERRA_FALLBACK = "#6E4538"
# Keys mirror index.html TC (only what captionAccentColor / resolve need)
TC: dict[str, dict[str, str]] = {
    "sand": {"ctaBg": "#1A1714"},
    "cream": {"ctaBg": "#1A1714"},
    "grey": {"ctaBg": "#1A1714"},
    "white": {"ctaBg": "#1A1714"},
    "dark": {"ctaBg": "#C8905A"},
    "black": {"tagPillBg": "#6E4538", "ctaBg": "#C8905A"},
}

# Downscale wide covers before encode (faster). Comma inside min() escaped for filtergraph.
FFMPEG_SCALE = (
    r"scale=w='min(1920\,iw)':h=-2,"
    r"scale=trunc(iw/2)*2:trunc(ih/2)*2"
)


def parse_data_url(data_url: str) -> tuple[bytes, str]:
    m = re.match(r"^data:([^;]+);base64,(.+)$", data_url, re.DOTALL)
    if not m:
        raise ValueError("Not a data URL")
    mime = m.group(1).split(";")[0].strip().lower()
    raw = base64.b64decode(m.group(2))
    if "mpeg" in mime or mime.endswith("/mp3"):
        ext = ".mp3"
    elif "mp4" in mime or "aac" in mime or "m4a" in mime:
        ext = ".m4a"
    elif "wav" in mime:
        ext = ".wav"
    elif "ogg" in mime:
        ext = ".ogg"
    else:
        ext = ".m4a"
    return raw, ext


def parse_srt_ms(t: str) -> float:
    t = t.strip().replace(",", ".")
    parts = t.split(":")
    if len(parts) < 3:
        return 0.0
    h, m_, s = int(parts[0]), int(parts[1]), float(parts[2])
    return (h * 3600 + m_ * 60 + s) * 1000.0


def parse_srt_to_cues(srt: str) -> list[dict]:
    cues = []
    raw = (srt or "").replace("\r\n", "\n").strip()
    if not raw:
        return cues
    for block in re.split(r"\n\n+", raw):
        lines = block.split("\n")
        if len(lines) < 2:
            continue
        li = 1 if re.match(r"^\d+$", lines[0].strip() or "") else 0
        if li >= len(lines):
            continue
        mm = re.match(
            r"(\d\d:\d\d:\d\d[,.]\d{3})\s*-->\s*(\d\d:\d\d:\d\d[,.]\d{3})",
            lines[li],
        )
        if not mm:
            continue
        text = "\n".join(lines[li + 1 :]).strip()
        cues.append(
            {
                "start": parse_srt_ms(mm.group(1)),
                "end": parse_srt_ms(mm.group(2)),
                "text": text,
            }
        )
    return cues


def apply_boost_words(raw: str, boost_csv: str | None) -> str:
    if not boost_csv or not str(boost_csv).strip():
        return raw
    terms = sorted(
        {x.strip() for x in re.split(r"[,;]+", boost_csv) if x.strip()},
        key=len,
        reverse=True,
    )
    s = raw
    for term in terms:
        esc = re.escape(term)

        def repl(m: re.Match) -> str:
            w = m.group(0)
            i = m.start()
            buf = m.string
            pre, post = buf[max(0, i - 2) : i], buf[i + len(w) : i + len(w) + 2]
            if "*" in pre or "*" in post:
                return w
            return f"**{w}**"

        s = re.sub(rf"(?i)\b({esc})\b", repl, s)
    return s


def ms_to_ass(ms: float) -> str:
    if ms < 0 or not (ms == ms):
        ms = 0.0
    cs = int(ms / 10) % 100
    t = int(ms // 1000)
    sec = t % 60
    t //= 60
    m_ = t % 60
    h = t // 60
    return f"{h}:{m_:02d}:{sec:02d}.{cs:02d}"


def escape_ass(s: str) -> str:
    return (
        s.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def hex_to_ass_colour(hex_str: str) -> str:
    """#RRGGBB → ASS &HAABBGGRR — index.html hexToAssColour."""
    m = re.match(r"^#?([0-9A-Fa-f]{6})$", (hex_str or "").strip())
    if not m:
        return "&H00FFFFFF"
    r = int(m.group(1)[0:2], 16)
    g = int(m.group(1)[2:4], 16)
    b = int(m.group(1)[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


def normalize_hex_color(value: Any) -> str | None:
    """#RRGGBB normalizer used by studio color controls / overrides."""
    s = str(value or "").strip()
    if not s:
        return None
    if not s.startswith("#"):
        s = "#" + s
    if not re.match(r"^#[0-9A-Fa-f]{6}$", s):
        return None
    return s.upper()


def _tc_row(theme: str | None) -> dict[str, str]:
    return dict(TC.get(theme or "cream") or TC["cream"])


def outro_tag_pill_bg_resolve(slide: dict, c: dict[str, str]) -> str:
    """index.html outroTagPillBgResolve(s, c) → #RRGGBB."""
    o = slide.get("outroTagPillBg")
    if o and str(o).strip():
        s = str(o).strip()
        if re.match(r"^#[0-9A-Fa-f]{6}$", s):
            return s
    a = slide.get("outroTagPillAccent")
    if a == "sand":
        return SAND_CTA
    if a == "terracotta":
        tg = c.get("tagPillBg")
        return tg if tg else TERRA_FALLBACK
    tg = c.get("tagPillBg")
    if tg:
        return tg
    return c.get("ctaBg") or SAND_CTA


def caption_accent_hex(slide: dict) -> str:
    """index.html captionAccentColor(s) — theme + optional outro pill overrides."""
    c = _tc_row(slide.get("theme"))
    return outro_tag_pill_bg_resolve(slide, c)


def caption_primary_hex(m: dict[str, Any]) -> str:
    """index.html renderSlideMediaStrip: captionLight ? #111 : white."""
    override = normalize_hex_color(m.get("captionColorOverride"))
    if override:
        return override
    if m.get("captionLight"):
        return "#111111"
    return "#FFFFFF"


def caption_karaoke_hex(slide: dict, m: dict[str, Any]) -> str:
    """Color for active karaoke/boost words in ASS + studio preview."""
    override = normalize_hex_color(m.get("captionKaraokeColorOverride"))
    if override:
        return override
    if m.get("captionLight"):
        # Match browser default for light strips: boosted/active words stay dark.
        return "#111111"
    return caption_accent_hex(slide)


def caption_muted_ass(m: dict[str, Any]) -> str:
    """Inactive karaoke words — index.html ASS_MUTED_LIGHT / ASS_MUTED_DARK."""
    override = normalize_hex_color(m.get("captionColorOverride"))
    if override:
        return hex_to_ass_colour(override)
    return hex_to_ass_colour("#999999" if m.get("captionLight") else "#B3B3B3")


def get_export_pixel_size(
    fmt: str | None, globals_: dict[str, Any] | None
) -> tuple[int, int]:
    """Same as index.html getExportPixelSize — drives ASS PlayResX/Y."""
    ex = {
        "sq": (1080, 1080),
        "port": (864, 1080),
        "st": (607, 1080),
    }
    ew0, eh0 = ex.get(fmt or "sq", ex["sq"])
    dpi = 150
    if globals_ and globals_.get("exportDpi") is not None:
        try:
            dpi = float(globals_["exportDpi"])
        except (TypeError, ValueError):
            dpi = 150
    m = dpi / 72.0
    return int(round(ew0 * m)), int(round(eh0 * m))


def is_counter_content_slide(s: dict | None) -> bool:
    if not s or s.get("type") != "content":
        return False
    if s.get("showCounter") is False:
        return False
    if s.get("slide3symbol") is True:
        return False
    if s.get("slide4photo") is not None:
        return False
    if s.get("slide5symbol") is True:
        return False
    return True


def get_slide_media_for_captions(slide: dict | None) -> dict[str, Any] | None:
    """Same fields as batch-json-to-mp4.cjs getSlideMediaForCaptions (caption strip)."""
    if not slide:
        return None
    if slide.get("slide3symbol"):
        if slide.get("slide3ShowMedia") is not True:
            return None
        cues: list = []
        raw_cues = slide.get("slide3SrtCues")
        if raw_cues and len(raw_cues):
            cues = raw_cues
        else:
            cues = parse_srt_to_cues(slide.get("slide3Srt") or "")
        t_b = slide.get("slide3TitleBottomPct")
        if t_b is None:
            t_b = 8.0
        try:
            t_b = float(t_b)
        except (TypeError, ValueError):
            t_b = 8.0
        nudge = slide.get("slide3CaptionNudgePct")
        if nudge is None:
            nudge = 6.0
        try:
            nudge = float(nudge)
        except (TypeError, ValueError):
            nudge = 6.0
        bot = min(100.0, max(0.0, t_b + nudge))
        return {
            "srt": slide.get("slide3Srt") or "",
            "srtCues": cues,
            "bottom": bot,
            "captionLight": True,
            "captionScale": slide.get("slide3CaptionScale")
            if slide.get("slide3CaptionScale") is not None
            else 100,
            "captionEmPct": slide.get("slide3CaptionEmPct")
            if slide.get("slide3CaptionEmPct") is not None
            else 130,
            "captionBoostCsv": slide.get("slide3CaptionBoostWords"),
            "captionElasticLand": slide.get("slide3CaptionElasticLand") is not False,
            "captionColorOverride": normalize_hex_color(slide.get("slide3CaptionColor")),
            "captionKaraokeColorOverride": normalize_hex_color(
                slide.get("slide3CaptionKaraokeColor")
            ),
        }
    if is_counter_content_slide(slide):
        if slide.get("cntMediaShow") is not True:
            return None
        cues = []
        raw_cues = slide.get("cntMediaSrtCues")
        if raw_cues and len(raw_cues):
            cues = raw_cues
        else:
            cues = parse_srt_to_cues(slide.get("cntMediaSrt") or "")
        cb = slide.get("cntMediaBottomPct")
        if cb is None:
            cb = 4.0
        try:
            cb = float(cb)
        except (TypeError, ValueError):
            cb = 4.0
        return {
            "srt": slide.get("cntMediaSrt") or "",
            "srtCues": cues,
            "bottom": cb,
            "captionLight": slide.get("counterTextWhite") is False,
            "captionScale": slide.get("cntCaptionScale")
            if slide.get("cntCaptionScale") is not None
            else 100,
            "captionEmPct": slide.get("cntCaptionEmPct")
            if slide.get("cntCaptionEmPct") is not None
            else 130,
            "captionBoostCsv": slide.get("cntCaptionBoostWords"),
            "captionElasticLand": slide.get("cntCaptionElasticLand") is not False,
            "captionColorOverride": normalize_hex_color(slide.get("cntCaptionColor")),
            "captionKaraokeColorOverride": normalize_hex_color(
                slide.get("cntCaptionKaraokeColor")
            ),
        }
    return None


def inner_caption_token(part: str) -> str:
    mm = re.match(r"^\*\*([^*]+)\*\*$", part or "")
    return mm.group(1) if mm else part


def ease_out_back(t: float) -> float:
    """CaptionStudio._ease_out_back — overshoot easing; t in [0,1]."""
    t = max(0.0, min(1.0, float(t)))
    c1 = 1.70158
    c3 = c1 + 1.0
    tt = t - 1.0
    return 1.0 + c3 * tt * tt * tt + c1 * tt * tt


def caption_elastic_move_tag(
    *,
    play_res_x: int,
    play_res_y: int,
    margin_v: int,
    cue_start: float,
    w_start: float,
    w_end: float,
    land_ms: float,
) -> str:
    """
    ASS \\an2\\move for bottom-center anchor — matches studio elastic land (drop + ease-out-back).
    """
    y_final = int(play_res_y - margin_v)
    drop = max(28, min(96, play_res_y // 8))
    x_c = int(round(play_res_x / 2.0))

    def y_at(elapsed_ms: float) -> int:
        if land_ms <= 0:
            return y_final
        tt = max(0.0, min(1.0, float(elapsed_ms) / float(land_ms)))
        prog = ease_out_back(tt)
        return int(round(y_final - (1.0 - prog) * float(drop)))

    e0 = float(w_start) - float(cue_start)
    e1 = float(w_end) - float(cue_start)
    y0 = y_at(e0)
    y1 = y_at(e1)
    dur = max(1, int(round(float(w_end) - float(w_start))))
    return "{\\an2\\move(%d,%d,%d,%d,0,%d)}" % (x_c, y0, x_c, y1, dur)


def caption_karaoke_dialogues_for_cue(
    c: dict[str, Any],
    base_fs: int,
    em_scale_pct: int | float,
    boost_csv: str | None,
    emph_ass: str,
    mut_ass: str,
    *,
    elastic_land: bool = False,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
    margin_v: int = 64,
    land_ms: float = ELASTIC_LAND_MS,
) -> list[str]:
    """index.html captionKaraokeDialoguesForCue — one Dialogue line per word time slice."""
    try:
        em = max(1.0, float(em_scale_pct) / 100.0)
    except (TypeError, ValueError):
        em = 1.3
    em_fs = max(1, int(round(base_fs * em)))
    cue_start = float(c["start"])
    cue_end = float(c["end"])
    dur = max(1.0, cue_end - cue_start)
    raw = re.sub(r"<[^>]+>", "", str(c.get("text") or ""))
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    n_l = max(1, len(lines))
    out: list[str] = []
    for li, line in enumerate(lines):
        t = apply_boost_words(line, boost_csv)
        parts = re.findall(r"\*\*[^*]+\*\*|\S+|\s+", t)
        word_count = sum(1 for p in parts if p.strip())
        if not word_count:
            continue
        line_start = cue_start + dur * (li / n_l)
        line_end = cue_start + dur * ((li + 1) / n_l)
        line_dur = max(1.0, line_end - line_start)
        for wi in range(word_count):
            w_start = line_start + line_dur * (wi / word_count)
            w_end = line_start + line_dur * ((wi + 1) / word_count)
            wp = 0
            chunks: list[str] = []
            reset = "" if elastic_land else "{\\r}"
            for p in parts:
                if not p.strip():
                    chunks.append(escape_ass(p))
                    continue
                is_act = wp == wi
                wp += 1
                inner = inner_caption_token(p)
                if is_act:
                    chunks.append(
                        "{\\fs"
                        + str(em_fs)
                        + "}{\\b1}{\\c"
                        + emph_ass
                        + "}"
                        + escape_ass(inner)
                        + reset
                    )
                else:
                    chunks.append(
                        "{\\fs"
                        + str(base_fs)
                        + "}{\\b0}{\\c"
                        + mut_ass
                        + "}"
                        + escape_ass(inner)
                        + reset
                    )
            prefix = (
                caption_elastic_move_tag(
                    play_res_x=play_res_x,
                    play_res_y=play_res_y,
                    margin_v=margin_v,
                    cue_start=cue_start,
                    w_start=w_start,
                    w_end=w_end,
                    land_ms=land_ms,
                )
                if elastic_land
                else ""
            )
            text = prefix + "".join(chunks)
            out.append(
                f"Dialogue: 0,{ms_to_ass(w_start)},{ms_to_ass(w_end)},Default,,0,0,0,,{text}"
            )
    return out


def build_ass_for_ffmpeg_pack(
    slide: dict,
    m: dict[str, Any],
    fmt: str | None,
    globals_: dict[str, Any] | None,
) -> str:
    """
    Mirrors batch-json-to-mp4.cjs buildAssForFfmpegPack / index.html.
    Caption size knob scales ASS base font (editor preview uses the same % idea).
    """
    cues: list[dict] = []
    if m.get("srtCues") and len(m["srtCues"]):
        cues = m["srtCues"]  # type: ignore[assignment]
    else:
        cues = parse_srt_to_cues(str(m.get("srt") or ""))

    ew, eh = get_export_pixel_size(fmt, globals_)
    try:
        bottom_pct = float(m.get("bottom") if m.get("bottom") is not None else 6.0)
    except (TypeError, ValueError):
        bottom_pct = 6.0
    margin_v = max(24, int(round(eh * (bottom_pct / 100.0))))

    try:
        cap_scale = float(m.get("captionScale") if m.get("captionScale") is not None else 100)
    except (TypeError, ValueError):
        cap_scale = 100.0
    base_fs = max(8, int(round(eh * ASS_BASE_FS_PCT * max(0.5, cap_scale / 100.0))))

    em_pct = m.get("captionEmPct") if m.get("captionEmPct") is not None else 130
    boost = m.get("captionBoostCsv")

    primary_ass = hex_to_ass_colour(caption_primary_hex(m))
    # Default matches browser behavior; studio color overrides can replace both base and karaoke.
    emph_ass = hex_to_ass_colour(caption_karaoke_hex(slide, m))
    if m.get("captionLight"):
        border_style, outline_w, shadow_w = 1, 0, 0
    else:
        border_style, outline_w, shadow_w = 3, 3, 0

    mut_ass = caption_muted_ass(m)

    elastic = bool(m.get("captionElasticLand") is not False)
    events: list[str] = []
    for c in cues:
        events.extend(
            caption_karaoke_dialogues_for_cue(
                c,
                base_fs,
                em_pct,
                boost,
                emph_ass,
                mut_ass,
                elastic_land=elastic,
                play_res_x=ew,
                play_res_y=eh,
                margin_v=margin_v,
                land_ms=ELASTIC_LAND_MS,
            )
        )

    # PrimaryColour = base caption; ** emphasis uses emph_ass in dialogue (see online preview).
    # Light captions: flat (no opaque box / heavy outline) like final vertical exports.
    style = (
        f"Style: Default,{ASS_FONT_FAMILY},{base_fs},{primary_ass},&H000000FF,&H00000000,&H80000000,"
        f"-1,0,0,0,100,100,0,0,{border_style},{outline_w},{shadow_w},2,48,48,{margin_v},1"
    )

    return "\r\n".join(
        [
            "[Script Info]",
            "Title: Beiza captions",
            "ScriptType: v4.00+",
            f"PlayResX: {ew}",
            f"PlayResY: {eh}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            style,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            "\r\n".join(events),
        ]
    )


def find_embedded_audio_slide(slides: list) -> tuple[int, dict] | None:
    """Same scan order as batch-json-to-mp4.cjs findEmbeddedAudio."""
    for i, s in enumerate(slides):
        if not s or not isinstance(s, dict):
            continue
        if (
            s.get("slide3symbol") is True
            and s.get("slide3ShowMedia") is True
            and s.get("slide3AudioData")
        ):
            return i, s
        if (
            is_counter_content_slide(s)
            and s.get("cntMediaShow") is True
            and s.get("cntMediaAudio")
        ):
            return i, s
    return None


def _is_carousel_session_file(p: Path) -> bool:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return "deckSchemaVersion" in d


def find_slide3_index(slides: list) -> int:
    for i, s in enumerate(slides):
        if isinstance(s, dict) and s.get("slide3symbol"):
            return i
    return -1


def slide3_cues_list(slide: dict) -> list[dict]:
    """SRT cues for symbol slide (embedded cues or parsed slide3Srt)."""
    if not slide.get("slide3symbol"):
        return []
    if slide.get("slide3SrtCues") and len(slide["slide3SrtCues"]):
        return slide["slide3SrtCues"]
    return parse_srt_to_cues(slide.get("slide3Srt") or "")


def caption_preview_line_from_slide(slide: dict, cue_index: int) -> str:
    """First line of cue #cue_index (0-based) — scrub in studio when cue 0 has no on-screen text yet."""
    cues = slide3_cues_list(slide)
    if not cues or cue_index < 0 or cue_index >= len(cues):
        return ""
    raw = (cues[cue_index].get("text") or "").strip()
    if not raw:
        return ""
    return raw.split("\n")[0].strip()


def first_caption_line_from_slide(slide: dict) -> str:
    """First line of the first SRT cue on symbol slide (preview does not require media strip on)."""
    return caption_preview_line_from_slide(slide, 0)


def format_cue_time_range_s(cue: dict) -> str:
    """Short time range for studio label."""
    try:
        s = float(cue.get("start", 0) or 0) / 1000.0
        e = float(cue.get("end", 0) or 0) / 1000.0
    except (TypeError, ValueError):
        return ""
    return f"{s:.1f}s–{e:.1f}s"


def bottom_pct_to_slide3_fields(bottom_total: float) -> tuple[float, float]:
    """Split ASS `bottom` % into slide3TitleBottomPct + slide3CaptionNudgePct.

    Studio now allows 0..100% so this keeps the title anchor stable and moves via nudge.
    """
    b = max(0.0, min(100.0, float(bottom_total)))
    t_b = 8.0
    nudge = b - t_b
    return t_b, nudge


def apply_studio_knobs_to_slide3(
    slide: dict,
    *,
    caption_scale_pct: float,
    bottom_total_pct: float,
    em_pct: float,
    caption_color: str | None = None,
    karaoke_color: str | None = None,
    elastic_land: bool = True,
) -> None:
    """Write Tk studio values into slide 3 — same keys FFmpeg ASS reads."""
    slide["slide3CaptionScale"] = max(40.0, min(220.0, float(caption_scale_pct)))
    t_b, nudge = bottom_pct_to_slide3_fields(bottom_total_pct)
    slide["slide3TitleBottomPct"] = t_b
    slide["slide3CaptionNudgePct"] = nudge
    slide["slide3CaptionEmPct"] = max(100.0, min(200.0, float(em_pct)))
    slide["slide3CaptionColor"] = normalize_hex_color(caption_color)
    slide["slide3CaptionKaraokeColor"] = normalize_hex_color(karaoke_color)
    slide["slide3CaptionElasticLand"] = bool(elastic_land)


def load_caption_layout_preset(path: Path | None = None) -> dict[str, Any] | None:
    """Load template caption layout JSON (see CAPTION_LAYOUT_PRESET)."""
    p = path or CAPTION_LAYOUT_PRESET
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def apply_caption_layout_preset_to_slide3(
    slide: dict | None, preset: dict[str, Any] | None
) -> bool:
    """Apply saved template keys onto symbol slide 3. Returns True if anything was set."""
    if not slide or not slide.get("slide3symbol") or not preset:
        return False
    changed = False
    for k in CAPTION_LAYOUT_KEYS:
        if k in preset and preset[k] is not None:
            slide[k] = preset[k]
            changed = True
    return changed


def save_caption_layout_preset(
    slide: dict, *, source_session: str | None = None, path: Path | None = None
) -> Path:
    """Persist slide3 layout for the next batch run (same folder as make_videos.py)."""
    out = path or CAPTION_LAYOUT_PRESET
    payload: dict[str, Any] = {"presetSchemaVersion": 1}
    if source_session:
        payload["sourceSession"] = source_session
    for k in CAPTION_LAYOUT_KEYS:
        if k in slide and slide[k] is not None:
            payload[k] = slide[k]
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def read_studio_knobs_from_slide3(
    slide: dict,
) -> tuple[float, float, float, bool, str, str]:
    """Scale %, combined bottom margin %, emphasis %, elastic land, subtitle color, karaoke color."""
    try:
        sc = float(slide.get("slide3CaptionScale"))
    except (TypeError, ValueError):
        sc = 100.0
    t_b = slide.get("slide3TitleBottomPct")
    if t_b is None:
        t_b = 8.0
    try:
        t_b = float(t_b)
    except (TypeError, ValueError):
        t_b = 8.0
    nudge = slide.get("slide3CaptionNudgePct")
    if nudge is None:
        nudge = 6.0
    try:
        nudge = float(nudge)
    except (TypeError, ValueError):
        nudge = 6.0
    bottom_tot = min(100.0, max(0.0, t_b + nudge))
    try:
        em = float(slide.get("slide3CaptionEmPct"))
    except (TypeError, ValueError):
        em = 130.0
    raw_el = slide.get("slide3CaptionElasticLand")
    if raw_el is None:
        elastic = True
    elif isinstance(raw_el, bool):
        elastic = raw_el
    else:
        elastic = bool(raw_el)
    subtitle_col = normalize_hex_color(slide.get("slide3CaptionColor")) or "#111111"
    karaoke_col = normalize_hex_color(slide.get("slide3CaptionKaraokeColor")) or "#111111"
    return sc, bottom_tot, em, elastic, subtitle_col, karaoke_col


def summarize_caption_settings(data: dict[str, Any]) -> str:
    """Human-readable caption + export settings from one session object (for GUI)."""
    if "deckSchemaVersion" not in data:
        return "Not a Carousel session (missing deckSchemaVersion)."
    slides = data.get("slides") or []
    found = find_embedded_audio_slide(slides)
    fmt = data.get("fmt") or "sq"
    globs = data.get("globals") if isinstance(data.get("globals"), dict) else {}
    dpi = globs.get("exportDpi")
    ew, eh = get_export_pixel_size(str(fmt), globs)
    lines = [
        f"Export format: {fmt!r}, exportDpi: {dpi!r} → ASS PlayRes {ew}×{eh}",
    ]
    if not found:
        lines.append(
            "No embedded audio strip: turn on “Audio + captions” on slide 3 (or counter strip) and save."
        )
        return "\n".join(lines)
    _idx, slide = found
    m = get_slide_media_for_captions(slide)
    if not m:
        lines.append(
            "Audio found but caption strip is off in the editor — enable Audio + captions and save."
        )
        return "\n".join(lines)
    lines.append(
        f"Captions: size {m.get('captionScale')!r}%, emphasis {m.get('captionEmPct')!r}%, "
        f"bottom margin ≈ {m.get('bottom')!r}% of frame height, "
        f"elastic land {'on' if m.get('captionElasticLand') is not False else 'off'}"
    )
    th = slide.get("theme") or "cream"
    lines.append(
        f"Theme {th!r} → base text {caption_primary_hex(m)}, "
        f"**words** accent {caption_accent_hex(slide)} (same as browser preview)"
    )
    boost = m.get("captionBoostCsv")
    if boost and str(boost).strip():
        lines.append(f"Boost words: {boost!r}")
    return "\n".join(lines)


def caption_summary_for_folder(work_dir: Path) -> str:
    """Scan folder for one session JSON and return summarize_caption_settings (first match)."""
    for jpath in sorted(work_dir.glob("*.json")):
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "deckSchemaVersion" not in data:
            continue
        return f"({jpath.name})\n{summarize_caption_settings(data)}"
    return "No session JSON with deckSchemaVersion in this folder."


def run_ffmpeg(
    cover: Path,
    audio: Path,
    ass: Path,
    out_mp4: Path,
    log: Callable[[str], None] | None = None,
) -> None:
    work_dir = ass.parent
    font_dest = work_dir / "fonts"
    use_fontsdir = False
    if CAROUSEL_FONTS_DIR.is_dir():
        font_dest.mkdir(exist_ok=True)
        for fn in ALLIANCE_FONT_FILES:
            src = CAROUSEL_FONTS_DIR / fn
            if src.is_file():
                shutil.copy2(src, font_dest / fn)
                use_fontsdir = True
    if use_fontsdir:
        vf = f"{FFMPEG_SCALE},ass={ass.name}:fontsdir=fonts"
    else:
        vf = f"{FFMPEG_SCALE},ass={ass.name}"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-y",
        "-loop",
        "1",
        "-i",
        str(cover.resolve()),
        "-i",
        str(audio),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(out_mp4.resolve()),
    ]
    _run_ffmpeg_stream(cmd, ass.parent, log)


def run_ffmpeg_no_subs(
    cover: Path,
    audio: Path,
    out_mp4: Path,
    log: Callable[[str], None] | None = None,
) -> None:
    vf = FFMPEG_SCALE
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-y",
        "-loop",
        "1",
        "-i",
        str(cover.resolve()),
        "-i",
        str(audio),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(out_mp4.resolve()),
    ]
    _run_ffmpeg_stream(cmd, audio.parent, log)


def _run_ffmpeg_stream(
    cmd: list[str],
    cwd: Path,
    log: Callable[[str], None] | None,
) -> None:
    """FFmpeg writes progress to stderr; stream both streams for the GUI."""
    p = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert p.stderr is not None
    for line in p.stderr:
        if log:
            log(line.rstrip())
    p.wait()
    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, None, None)


def _deep_clone(o: Any) -> Any:
    return copy.deepcopy(o)


def next_versioned_mp4_path(path: Path) -> Path:
    """Return a non-overwriting mp4 path: name.mp4, name-v2.mp4, name-v3.mp4..."""
    if not path.exists():
        return path
    stem = path.stem
    suf = path.suffix or ".mp4"
    i = 2
    while True:
        cand = path.with_name(f"{stem}-v{i}{suf}")
        if not cand.exists():
            return cand
        i += 1


def read_variants_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for raw in r:
            rows.append(
                {
                    (k or "").strip().lower().replace("\ufeff", ""): (v or "")
                    for k, v in raw.items()
                }
            )
        return rows


def csv_row_to_delta(
    row: dict[str, str], base: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Same field mapping as index.html variantsCsvRowToDelta (uses base slides for explainer/counter indices)."""

    def g(k: str) -> str:
        return (row.get(k) or "").strip()

    slides_list: list[Any] = list((base or {}).get("slides") or [])
    slides_m: dict[str, Any] = {}
    pan_m: dict[str, Any] = {}

    def find_slide5_explainer_ix() -> int:
        for i, s in enumerate(slides_list):
            if isinstance(s, dict) and s.get("slide5symbol") is True:
                return i
        return 4

    def find_counter_content_ix() -> int:
        for i, s in enumerate(slides_list):
            if not isinstance(s, dict):
                continue
            if (
                s.get("type") == "content"
                and s.get("showCounter", True) is not False
                and s.get("slide3symbol") is not True
                and s.get("slide4photo") is None
                and s.get("slide5symbol") is not True
            ):
                return i
        return 5

    t2, hw2, t3 = g("slide3_title"), g("slide3_hw"), g("slide4_title")
    photo_caption = (g("slide4_para") or g("photo_para")).strip()
    explainer_explicit = (
        g("explainer_para") or g("slide5_para") or g("slide5_body")
    ).strip()
    legacy_t5 = g("slide5_title")
    counter_title = (g("counter_title") or g("counter_name")).strip()

    explainer_body = explainer_explicit
    did_auto_split = False
    if (
        not counter_title
        and legacy_t5
        and photo_caption
        and not explainer_explicit
        and len(legacy_t5) <= 40
        and len(photo_caption) >= max(35, len(legacy_t5) * 3)
    ):
        counter_title = legacy_t5
        explainer_body = photo_caption
        photo_caption = ""
        did_auto_split = True
    if not explainer_body and not did_auto_split and legacy_t5 and not counter_title:
        explainer_body = legacy_t5

    if t2:
        slides_m["2"] = {"title": t2}
    if hw2:
        slides_m["2"] = {**(slides_m.get("2") or {}), "hw": hw2}
    if t3:
        slides_m["3"] = {**(slides_m.get("3") or {}), "title": t3}
    if photo_caption:
        slides_m["3"] = {
            **(slides_m.get("3") or {}),
            "para": photo_caption,
            "showPara": True,
        }
    if explainer_body:
        ix_expl = find_slide5_explainer_ix()
        slides_m[str(ix_expl)] = {
            **(slides_m.get(str(ix_expl)) or {}),
            "para": explainer_body,
            "showPara": True,
        }
    if counter_title:
        ix_cnt_title = find_counter_content_ix()
        slides_m[str(ix_cnt_title)] = {
            **(slides_m.get(str(ix_cnt_title)) or {}),
            "title": counter_title,
        }
    pt, pst = g("pantext"), g("pansubtext")
    if pt or pst:
        pan_m["1"] = {}
        if pt:
            pan_m["1"]["panText"] = pt
        if pst:
            pan_m["1"]["panSubText"] = pst
    em_raw = g("empct")
    em_num: float | None = None
    if em_raw:
        try:
            em_num = float(em_raw.replace(",", "."))
        except ValueError:
            em_num = None
    boost = g("boostwords")
    srt_inline = g("srt").replace("\\n", "\n")
    has_cap = bool(srt_inline.strip() or boost or em_num is not None)
    if has_cap:
        slides_m["2"] = {**(slides_m.get("2") or {}), "slide3ShowMedia": True}
        if em_num is not None:
            slides_m["2"]["slide3CaptionEmPct"] = em_num
        if boost:
            slides_m["2"]["slide3CaptionBoostWords"] = boost
            ix_cnt = find_counter_content_ix()
            slides_m[str(ix_cnt)] = {
                **(slides_m.get(str(ix_cnt)) or {}),
                "cntCaptionBoostWords": boost,
            }
        if srt_inline.strip():
            slides_m["2"]["slide3Srt"] = srt_inline
    delta: dict[str, Any] = {"deltaSchemaVersion": 1}
    if slides_m:
        delta["slides"] = slides_m
    if pan_m:
        delta["panGroups"] = pan_m
    sym, cn, vos = g("symbolname"), g("carouselnum"), g("voiceover_script")
    if sym or cn or vos:
        delta["_meta"] = {
            "symbolName": sym,
            "carouselNum": cn,
            "voiceoverScript": vos,
        }
    if has_cap:
        cap: dict[str, Any] = {}
        if srt_inline.strip():
            cap["srt"] = srt_inline
        if boost:
            cap["boostWords"] = boost
        if em_num is not None:
            cap["emPct"] = em_num
        delta["captions"] = cap
    return delta


def apply_session_delta(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Mirror index.html applyDelta (text + captions; no binary)."""
    merged = _deep_clone(base)
    if delta.get("_meta") is not None:
        merged["_meta"] = delta["_meta"]
    if delta.get("_voiceover") is not None:
        merged["_voiceover"] = {
            **(merged.get("_voiceover") or {}),
            **delta["_voiceover"],
        }
    if delta.get("panGroups"):
        merged["panGroups"] = merged.get("panGroups") or {}
        for pid, patch in delta["panGroups"].items():
            merged["panGroups"][pid] = {
                **(merged["panGroups"].get(pid) or {}),
                **patch,
            }
    if delta.get("slides"):
        merged["slides"] = merged.get("slides") or []
        for k, patch in delta["slides"].items():
            try:
                i = int(k)
            except ValueError:
                continue
            if 0 <= i < len(merged["slides"]):
                merged["slides"][i] = {**merged["slides"][i], **patch}
    if delta.get("captions"):
        slides = merged.get("slides") or []
        ix = next((i for i, s in enumerate(slides) if s.get("slide3symbol")), -1)
        if ix >= 0:
            t = slides[ix]
            cap = delta["captions"]
            if cap.get("srt") is not None:
                t["slide3Srt"] = cap["srt"]
            if cap.get("boostWords") is not None:
                t["slide3CaptionBoostWords"] = cap["boostWords"]
            if cap.get("emPct") is not None:
                t["slide3CaptionEmPct"] = cap["emPct"]
        ix2 = next(
            (
                i
                for i, s in enumerate(slides)
                if is_counter_content_slide(s) and s.get("cntMediaShow")
            ),
            -1,
        )
        if ix2 >= 0 and delta["captions"].get("counterSrt") is not None:
            slides[ix2]["cntMediaSrt"] = delta["captions"]["counterSrt"]
    return merged


def _guess_audio_mime(ext: str) -> str:
    e = ext.lower()
    if e in (".mp3", ".mpeg"):
        return "audio/mpeg"
    if e == ".wav":
        return "audio/wav"
    if e in (".m4a", ".aac"):
        return "audio/mp4"
    if e == ".ogg":
        return "audio/ogg"
    return "audio/mpeg"


def embed_audio_file_into_slide3(session: dict[str, Any], audio_path: Path) -> None:
    slides = session.get("slides") or []
    idx = next((i for i, s in enumerate(slides) if s.get("slide3symbol")), -1)
    if idx < 0:
        return
    raw = audio_path.read_bytes()
    mime = _guess_audio_mime(audio_path.suffix)
    b64 = base64.standard_b64encode(raw).decode("ascii")
    slides[idx]["slide3AudioData"] = f"data:{mime};base64,{b64}"
    slides[idx]["slide3AudioName"] = audio_path.name
    slides[idx]["slide3ShowMedia"] = True


def run_whisper_to_srt(audio_path: Path, out_srt: Path, log: Callable[[str], None]) -> bool:
    """Run OpenAI whisper CLI; writes out_srt (cache path)."""
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="whisper-"))
    try:
        subprocess.run(
            [
                "whisper",
                str(audio_path.resolve()),
                "--output_format",
                "srt",
                "--output_dir",
                str(tmp),
                "--language",
                "en",
            ],
            check=True,
        )
        produced = tmp / f"{audio_path.stem}.srt"
        if not produced.is_file():
            log(f"[whisper] missing expected {produced.name}")
            return False
        shutil.copy(produced, out_srt)
        log(f"[whisper] {audio_path.name} → {out_srt.name}")
        return True
    except FileNotFoundError:
        log(
            "[fail] whisper executable not found. Install: pip install openai-whisper "
            "(and ffmpeg), or add whisper to PATH."
        )
        return False
    except subprocess.CalledProcessError as e:
        log(f"[fail] whisper exited {e.returncode} for {audio_path.name}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def resolve_srt_for_row(
    row: dict[str, str],
    symbol: str,
    srt_dir: Path,
    audio_path: Path | None,
    log: Callable[[str], None],
    *,
    allow_whisper: bool,
) -> str | None:
    csv_srt = (row.get("srt") or "").replace("\\n", "\n").strip()
    if csv_srt:
        return csv_srt
    cached = srt_dir / f"{symbol}.srt"
    if cached.is_file():
        return cached.read_text(encoding="utf-8")
    if allow_whisper and audio_path and audio_path.is_file() and not cached.is_file():
        if run_whisper_to_srt(audio_path, cached, log):
            if cached.is_file():
                return cached.read_text(encoding="utf-8")
    return None


def process_csv_pipeline(
    csv_path: Path,
    base_json: Path,
    carousel_root: Path,
    *,
    transcribe_only: bool,
    log: Callable[[str], None] = print,
) -> None:
    """
    variants.csv + base_session.json → mp4-out/<symbolName>.mp4
    Optional columns match index.html Load CSV. SRT: CSV > srt/<symbol>.srt > Whisper(audio).
    """
    if not csv_path.is_file():
        log(f"Missing CSV: {csv_path}")
        return

    audio_dir = carousel_root / "audio"
    srt_dir = carousel_root / "srt"
    srt_dir.mkdir(parents=True, exist_ok=True)

    rows = read_variants_csv(csv_path)
    if not rows:
        log("CSV has no rows.")
        return

    if transcribe_only:
        for row in rows:
            symbol = (row.get("symbolname") or "").strip()
            if not symbol:
                log("[skip] empty symbolName row")
                continue
            audio_name = (row.get("audiofile") or "").strip()
            audio_path = audio_dir / audio_name if audio_name else None
            if not audio_path or not audio_path.is_file():
                log(f"[skip] {symbol} — no audio for transcription")
                continue
            resolve_srt_for_row(
                row,
                symbol,
                srt_dir,
                audio_path,
                log,
                allow_whisper=True,
            )
        log("Transcribe-only pass done.")
        return

    if not base_json.is_file():
        log(f"Missing base session JSON: {base_json}")
        log("Export a full session from the browser as base_session.json next to variants.csv.")
        return
    try:
        base_data: dict[str, Any] = json.loads(
            base_json.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as e:
        log(f"Invalid base JSON: {e}")
        return
    if "deckSchemaVersion" not in base_data:
        log("base_session.json has no deckSchemaVersion.")
        return

    preset = load_caption_layout_preset()
    if preset:
        bs = base_data.get("slides") or []
        pix = find_slide3_index(bs)
        if pix >= 0 and apply_caption_layout_preset_to_slide3(bs[pix], preset):
            log(
                f"[caption] Applied template layout from {CAPTION_LAYOUT_PRESET.name} "
                "(before CSV merge; row fields can still override)."
            )

    covers = carousel_root / "covers"
    out_dir = carousel_root / OUT_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        symbol = (row.get("symbolname") or "").strip()
        if not symbol:
            log("[skip] empty symbolName row")
            continue
        cover_name = (row.get("coverimage") or "").strip()
        audio_name = (row.get("audiofile") or "").strip()
        cover_path = covers / cover_name if cover_name else Path()
        audio_path = audio_dir / audio_name if audio_name else None

        delta = csv_row_to_delta(row, base_data)
        merged = apply_session_delta(base_data, delta)

        if not cover_name or not cover_path.is_file():
            log(f"[skip] {symbol} — missing cover ({cover_name or '—'})")
            continue
        if not audio_path or not audio_path.is_file():
            log(f"[skip] {symbol} — missing audio ({audio_name or '—'})")
            continue

        srt_text = resolve_srt_for_row(
            row,
            symbol,
            srt_dir,
            audio_path,
            log,
            allow_whisper=True,
        )
        slides = merged.get("slides") or []
        idx3 = next((i for i, s in enumerate(slides) if s.get("slide3symbol")), -1)
        if idx3 >= 0 and srt_text:
            slides[idx3]["slide3Srt"] = srt_text
        elif idx3 >= 0 and not srt_text:
            log(f"[skip] {symbol} — no SRT (CSV, srt/{symbol}.srt, or Whisper)")
            continue

        embed_audio_file_into_slide3(merged, audio_path)

        fmt = merged.get("fmt")
        globs = merged.get("globals") if isinstance(merged.get("globals"), dict) else None
        slide = slides[idx3]
        m = get_slide_media_for_captions(slide)
        out_mp4 = next_versioned_mp4_path(out_dir / f"{symbol}.mp4")

        tmp = Path(tempfile.mkdtemp(prefix="beiza-csv-"))
        try:
            ext = audio_path.suffix or ".mp3"
            audio_tmp = tmp / f"audio{ext}"
            shutil.copy2(audio_path, audio_tmp)

            if m and srt_text:
                ass_body = build_ass_for_ffmpeg_pack(slide, m, fmt, globs)
                ass_path = tmp / "captions.ass"
                ass_path.write_text(ass_body, encoding="utf-8")
                log(f"[mux] {symbol} → {out_mp4.name} (ASS)")
                run_ffmpeg(cover_path, audio_tmp, ass_path, out_mp4, log=log)
            else:
                log(f"[mux] {symbol} → {out_mp4.name} (no subs)")
                run_ffmpeg_no_subs(cover_path, audio_tmp, out_mp4, log=log)
            log("")
        except subprocess.CalledProcessError as e:
            log(f"[fail] {symbol} — ffmpeg exited with code {e.returncode}.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    log("CSV pipeline done.")


def process_session_folder(
    work_dir: Path,
    log: Callable[[str], None] = print,
) -> None:
    """
    work_dir: folder containing Cover.jpg, *.json, writes mp4-out/*.mp4
    """
    cover = work_dir / COVER_NAME
    out_dir = work_dir / OUT_SUBDIR

    if not cover.is_file():
        log(f"Missing {COVER_NAME} in: {work_dir}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    json_files = sorted(work_dir.glob("*.json"))
    if not json_files:
        log("No .json files in this folder.")
        return

    for jpath in json_files:
        name = jpath.stem
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            log(f"[skip] {jpath.name} — invalid JSON: {e}")
            continue

        if "deckSchemaVersion" not in data:
            log(f"[skip] {jpath.name} — no deckSchemaVersion (not a full session).")
            continue

        slides = data.get("slides") or []
        found = find_embedded_audio_slide(slides)
        if not found:
            log(
                f"[skip] {jpath.name} — no embedded audio (slide 3 “Audio + captions” or counter strip)."
            )
            continue

        _slide_idx, slide = found
        preset = load_caption_layout_preset()
        if preset and slide.get("slide3symbol"):
            apply_caption_layout_preset_to_slide3(slide, preset)

        fmt = data.get("fmt")
        globals_ = data.get("globals") if isinstance(data.get("globals"), dict) else None

        if slide.get("slide3symbol") is True and slide.get("slide3ShowMedia") is True:
            data_url = slide.get("slide3AudioData")
        else:
            data_url = slide.get("cntMediaAudio")
        if not data_url:
            log(f"[skip] {jpath.name} — missing audio data URL.")
            continue

        try:
            audio_bytes, ext = parse_data_url(str(data_url))
        except Exception as e:
            log(f"[skip] {jpath.name} — audio: {e}")
            continue

        m = get_slide_media_for_captions(slide)
        srt = (m.get("srt") or "").strip() if m else ""

        out_mp4 = next_versioned_mp4_path(out_dir / f"{name}.mp4")
        tmp = Path(tempfile.mkdtemp(prefix="beiza-vid-"))
        try:
            audio_path = tmp / f"audio{ext}"
            audio_path.write_bytes(audio_bytes)

            if srt and m:
                ass_body = build_ass_for_ffmpeg_pack(slide, m, fmt, globals_)
                ass_path = tmp / "captions.ass"
                ass_path.write_text(ass_body, encoding="utf-8")
                log(f"[mux] {jpath.name} -> {out_mp4.name} (with ASS, from saved caption settings)")
                run_ffmpeg(cover, audio_path, ass_path, out_mp4, log=log)
            else:
                log(
                    f"[mux] {jpath.name} -> {out_mp4.name} (no SRT or caption strip off — video only)"
                )
                run_ffmpeg_no_subs(cover, audio_path, out_mp4, log=log)
            log("")
        except subprocess.CalledProcessError as e:
            log(f"[fail] {jpath.name} — ffmpeg exited with code {e.returncode}.")
            continue
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    log("Done.")


def run_caption_studio(initial_json: Path | None = None) -> None:
    """
    Tkinter caption studio (`--studio`): cover + cue scrub, layout sliders, elastic land
    (preview + FFmpeg ASS). Saves slide3 fields to the session JSON and caption_layout_preset.json.

    Tuning (see module docstring at top of make_videos.py):
        STUDIO_STYLE_DEBOUNCE_MS — None = after_idle; set e.g. 16 for timed debounce.
        STUDIO_ELASTIC_LAND_STEPS / STUDIO_ELASTIC_FRAME_MS — elastic land motion.

    Requires: pip install Pillow
    """
    import os
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk

    try:
        from PIL import Image, ImageDraw, ImageFont, ImageTk
    except ImportError:
        print("Caption studio needs Pillow: pip install Pillow")
        return

    try:
        _pil_resample = Image.Resampling.LANCZOS  # Pillow ≥10
    except AttributeError:
        _pil_resample = Image.LANCZOS  # type: ignore[attr-defined]

    def win_font(size: int) -> Any:
        windir = os.environ.get("WINDIR", r"C:\Windows")
        for fn in ("segoeui.ttf", "arial.ttf", "calibri.ttf"):
            p = os.path.join(windir, "Fonts", fn)
            if os.path.isfile(p):
                return ImageFont.truetype(p, size)
        return ImageFont.load_default()

    def studio_pair_fonts(size_reg: int, size_emph: int) -> tuple[Any, Any]:
        """
        Regular + heavy face for caption preview — mirrors index.html (Alliance + fw 800).
        Prefers Alliance regular + bold if present under Windows Fonts; else Segoe UI + bold.
        """
        windir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        reg_path: str | None = None
        bold_path: str | None = None
        if windir.is_dir():
            for p in sorted(windir.iterdir()):
                n = p.name.lower()
                if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                    continue
                if "alliance" in n:
                    if any(x in n for x in ("bold", "heavy", "black", "bd")):
                        bold_path = str(p)
                    elif reg_path is None:
                        reg_path = str(p)
        segoe, segoe_b = windir / "segoeui.ttf", windir / "segoeuib.ttf"
        arial, arial_b = windir / "arial.ttf", windir / "arialbd.ttf"
        if reg_path and bold_path:
            return (
                ImageFont.truetype(reg_path, size_reg),
                ImageFont.truetype(bold_path, size_emph),
            )
        if segoe.is_file() and segoe_b.is_file():
            return (
                ImageFont.truetype(str(segoe), size_reg),
                ImageFont.truetype(str(segoe_b), size_emph),
            )
        if arial.is_file() and arial_b.is_file():
            return (
                ImageFont.truetype(str(arial), size_reg),
                ImageFont.truetype(str(arial_b), size_emph),
            )
        if reg_path:
            return (
                ImageFont.truetype(reg_path, size_reg),
                ImageFont.truetype(reg_path, size_emph),
            )
        return (win_font(size_reg), win_font(size_emph))

    class CaptionStudio(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("Carousel — caption layout (cue preview)")
            self.geometry("620x920")
            self.minsize(560, 760)

            self.session_path: Path | None = None
            self.data: dict[str, Any] | None = None
            self.slide3_idx = -1
            self._photo: Any = None
            self._cue_count = 0
            self.cue_idx_var = tk.DoubleVar(value=0.0)
            self.cover_path: Path | None = None
            self._playing = False
            self._play_after_id: str | None = None
            self._style_idle_pending = False
            self._style_debounce_after: str | None = None
            self._style_traces: list[tuple[Any, str]] = []
            self._cover_cache_key: tuple[Any, ...] | None = None
            self._cover_cache_img: Any | None = None
            self._anim_land_var = tk.BooleanVar(value=True)
            self._carry_cover_var = tk.BooleanVar(value=False)
            self._anim_after_ids: list[str] = []

            self.scale_var = tk.DoubleVar(value=100.0)
            self.bottom_var = tk.DoubleVar(value=14.0)
            self.em_var = tk.DoubleVar(value=130.0)
            self.caption_color_var = tk.StringVar(value="#111111")
            self.karaoke_color_var = tk.StringVar(value="#111111")

            # Scroll container so all studio controls remain reachable on smaller displays.
            wrap = ttk.Frame(self, padding=4)
            wrap.pack(fill=tk.BOTH, expand=True)
            self._scroll_canvas = tk.Canvas(wrap, highlightthickness=0)
            self._scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            ysb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._scroll_canvas.yview)
            ysb.pack(side=tk.RIGHT, fill=tk.Y)
            self._scroll_canvas.configure(yscrollcommand=ysb.set)
            self._scroll_body = ttk.Frame(self._scroll_canvas)
            self._scroll_win = self._scroll_canvas.create_window(
                (0, 0), window=self._scroll_body, anchor="nw"
            )
            self._scroll_body.bind(
                "<Configure>",
                lambda _e: self._scroll_canvas.configure(
                    scrollregion=self._scroll_canvas.bbox("all")
                ),
            )
            self._scroll_canvas.bind(
                "<Configure>",
                lambda e: self._scroll_canvas.itemconfigure(self._scroll_win, width=e.width),
            )
            self._scroll_canvas.bind_all("<MouseWheel>", self._on_mousewheel, add=True)

            top = ttk.Frame(self._scroll_body, padding=8)
            top.pack(fill=tk.X)
            ttk.Button(top, text="Open session JSON…", command=self.on_open).pack(
                side=tk.LEFT
            )
            self.path_lbl = ttk.Label(top, text="(no file)", foreground="#555")
            self.path_lbl.pack(side=tk.LEFT, padx=10)

            cov = ttk.Frame(self._scroll_body, padding=(8, 0))
            cov.pack(fill=tk.X)
            ttk.Label(cov, text="Cover:").pack(side=tk.LEFT)
            self.cover_lbl = ttk.Label(cov, text="(not loaded)", foreground="#555")
            self.cover_lbl.pack(side=tk.LEFT, padx=(6, 10))
            ttk.Button(cov, text="Open cover…", command=self.on_open_cover).pack(side=tk.LEFT)
            ttk.Button(cov, text=f"Use {COVER_NAME}", command=self.on_reset_cover).pack(
                side=tk.LEFT, padx=(6, 0)
            )
            ttk.Checkbutton(
                cov,
                text=f"Use selected cover for render ({COVER_NAME}) on Save",
                variable=self._carry_cover_var,
            ).pack(side=tk.LEFT, padx=(8, 0))
            self.btn_play = ttk.Button(cov, text="Play", command=self.on_play)
            self.btn_play.pack(side=tk.LEFT, padx=(12, 0))
            self.btn_pause = ttk.Button(cov, text="Pause", command=self.on_pause, state="disabled")
            self.btn_pause.pack(side=tk.LEFT, padx=(4, 0))

            self.line_lbl = ttk.Label(
                self._scroll_body,
                text="First caption line appears here.",
                wraplength=480,
                justify=tk.CENTER,
            )
            self.line_lbl.pack(pady=(4, 4))

            cue_row = ttk.Frame(self._scroll_body, padding=(8, 0))
            cue_row.pack(fill=tk.X)
            ttk.Label(cue_row, text="Preview cue (frame):").pack(side=tk.LEFT)
            self.cue_scale = ttk.Scale(
                cue_row,
                from_=0.0,
                to=1.0,
                orient=tk.HORIZONTAL,
                variable=self.cue_idx_var,
                command=self._on_cue_scrub,
            )
            self.cue_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
            self.cue_meta_lbl = ttk.Label(cue_row, text="—", width=22)
            self.cue_meta_lbl.pack(side=tk.LEFT)
            self.cue_scale.bind("<ButtonRelease-1>", self._on_cue_release, add=True)

            anim_row = ttk.Frame(self._scroll_body, padding=(8, 2))
            anim_row.pack(fill=tk.X)
            ttk.Checkbutton(
                anim_row,
                text="Elastic land (preview + FFmpeg export)",
                variable=self._anim_land_var,
            ).pack(side=tk.LEFT)
            ttk.Button(anim_row, text="Replay land", command=self._replay_land_animation).pack(
                side=tk.LEFT, padx=(10, 0)
            )

            prev = ttk.LabelFrame(
                self._scroll_body, text="Preview (scaled to fit — matches proportions)", padding=6
            )
            prev.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
            self.canvas = tk.Canvas(prev, width=400, height=620, bg="white", highlightthickness=1)
            self.canvas.pack()

            ctl = ttk.Frame(self._scroll_body, padding=8)
            ctl.pack(fill=tk.X)
            tabs = ttk.Notebook(ctl)
            tabs.pack(fill=tk.X)
            tab_layout = ttk.Frame(tabs, padding=4)
            tab_color = ttk.Frame(tabs, padding=4)
            tabs.add(tab_layout, text="Layout")
            tabs.add(tab_color, text="Colors")

            ttk.Label(tab_layout, text="Caption size %").grid(row=0, column=0, sticky="w")
            self.scale_s = ttk.Scale(
                tab_layout,
                from_=40,
                to=200,
                orient=tk.HORIZONTAL,
                variable=self.scale_var,
            )
            self.scale_s.grid(row=0, column=1, sticky="ew", padx=6)
            self.scale_v = ttk.Label(tab_layout, width=6)
            self.scale_v.grid(row=0, column=2)

            ttk.Label(tab_layout, text="From bottom %").grid(
                row=1, column=0, sticky="w", pady=(6, 0)
            )
            self.bottom_s = ttk.Scale(
                tab_layout,
                from_=0,
                to=100,
                orient=tk.HORIZONTAL,
                variable=self.bottom_var,
            )
            self.bottom_s.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
            self.bottom_v = ttk.Label(tab_layout, width=6)
            self.bottom_v.grid(row=1, column=2, pady=(6, 0))

            ttk.Label(tab_layout, text="Boost size %").grid(
                row=2, column=0, sticky="w", pady=(6, 0)
            )
            self.em_s = ttk.Scale(
                tab_layout,
                from_=100,
                to=170,
                orient=tk.HORIZONTAL,
                variable=self.em_var,
            )
            self.em_s.grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))
            self.em_v = ttk.Label(tab_layout, width=6)
            self.em_v.grid(row=2, column=2, pady=(6, 0))
            tab_layout.columnconfigure(1, weight=1)

            ttk.Label(tab_color, text="Subtitle color (#RRGGBB)").grid(
                row=0, column=0, sticky="w"
            )
            ttk.Entry(tab_color, textvariable=self.caption_color_var, width=14).grid(
                row=0, column=1, sticky="w", padx=(6, 0)
            )
            ttk.Button(
                tab_color, text="Pick…", command=lambda: self._pick_color(self.caption_color_var)
            ).grid(row=0, column=2, padx=(8, 0))

            ttk.Label(tab_color, text="Karaoke active color (#RRGGBB)").grid(
                row=1, column=0, sticky="w", pady=(8, 0)
            )
            ttk.Entry(tab_color, textvariable=self.karaoke_color_var, width=14).grid(
                row=1, column=1, sticky="w", padx=(6, 0), pady=(8, 0)
            )
            ttk.Button(
                tab_color, text="Pick…", command=lambda: self._pick_color(self.karaoke_color_var)
            ).grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

            for v in (
                self.scale_var,
                self.bottom_var,
                self.em_var,
                self.caption_color_var,
                self.karaoke_color_var,
            ):
                tid = v.trace_add("write", self._on_style_slider_trace)
                self._style_traces.append((v, tid))

            bot = ttk.Frame(self._scroll_body, padding=8)
            bot.pack(fill=tk.X)
            ttk.Button(bot, text="Save to JSON", command=self.on_save).pack(side=tk.LEFT)
            ttk.Button(bot, text="Save preset as…", command=self.on_save_preset_as).pack(
                side=tk.LEFT, padx=(8, 0)
            )
            ttk.Button(bot, text="Load template preset", command=self.on_load_preset).pack(
                side=tk.LEFT, padx=(8, 0)
            )
            ttk.Button(bot, text="Load preset file…", command=self.on_load_preset_file).pack(
                side=tk.LEFT, padx=(8, 0)
            )
            ttk.Label(
                bot,
                text=f"Also writes {CAPTION_LAYOUT_PRESET.name} for batch defaults.",
                foreground="#666",
            ).pack(side=tk.LEFT, padx=12)

            start = initial_json if initial_json and initial_json.is_file() else DEFAULT_BASE_SESSION
            if start.is_file():
                self.load_path(start)
            else:
                j = next(
                    (p for p in sorted(SCRIPT_DIR.glob("*.json")) if _is_carousel_session_file(p)),
                    None,
                )
                if j:
                    self.load_path(j)
                else:
                    self._resolve_cover_path()
                    self.redraw()
            self.protocol("WM_DELETE_WINDOW", self._on_close_window)

        def _on_close_window(self) -> None:
            self.on_pause()
            self._cancel_land_animation()
            try:
                self._scroll_canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            if self._style_debounce_after is not None:
                try:
                    self.after_cancel(self._style_debounce_after)
                except (tk.TclError, ValueError):
                    pass
                self._style_debounce_after = None
            for v, tid in self._style_traces:
                try:
                    v.trace_remove("write", tid)
                except tk.TclError:
                    pass
            self.destroy()

        def _on_mousewheel(self, event: Any) -> None:
            """Scroll studio controls with mouse wheel."""
            if not hasattr(self, "_scroll_canvas"):
                return
            try:
                delta = int(event.delta)
            except Exception:
                delta = 0
            if delta == 0:
                return
            step = -1 if delta > 0 else 1
            self._scroll_canvas.yview_scroll(step, "units")

        def _on_style_slider_trace(self, *_args: Any) -> None:
            """Coalesce redraws: after_idle (default) or STUDIO_STYLE_DEBOUNCE_MS."""
            self._cancel_land_animation()
            ms = STUDIO_STYLE_DEBOUNCE_MS
            if ms is None:
                if self._style_idle_pending:
                    return
                self._style_idle_pending = True
                self.after_idle(self._flush_style_idle)
                return
            if self._style_debounce_after is not None:
                try:
                    self.after_cancel(self._style_debounce_after)
                except (tk.TclError, ValueError):
                    pass
            self._style_debounce_after = self.after(ms, self._flush_style_debounced)

        def _flush_style_debounced(self) -> None:
            self._style_debounce_after = None
            try:
                self._redraw_static()
            except tk.TclError:
                pass

        def _flush_style_idle(self) -> None:
            self._style_idle_pending = False
            try:
                self._redraw_static()
            except tk.TclError:
                pass

        def _invalidate_cover_cache(self) -> None:
            self._cover_cache_key = None
            self._cover_cache_img = None

        def _cancel_land_animation(self) -> None:
            for aid in self._anim_after_ids:
                try:
                    self.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
            self._anim_after_ids.clear()

        @staticmethod
        def _ease_out_back(t: float) -> float:
            """Overshoot easing (string / settle feel). t in [0,1]."""
            t = max(0.0, min(1.0, t))
            c1 = 1.70158
            c3 = c1 + 1.0
            tt = t - 1.0
            return 1.0 + c3 * tt * tt * tt + c1 * tt * tt

        def _replay_land_animation(self) -> None:
            if not self._anim_land_var.get():
                messagebox.showinfo(
                    "Elastic land",
                    "Turn on “Elastic land” first to preview the motion (same easing as burned MP4).",
                )
                return
            self._start_land_animation()

        def _on_cue_release(self, _event: Any) -> None:
            if self.data and self.slide3_idx >= 0 and self._anim_land_var.get():
                self._start_land_animation()

        def _start_land_animation(self) -> None:
            if not self.data or self.slide3_idx < 0:
                return
            self._cancel_land_animation()
            layout = self._compute_preview_layout()
            if layout is None:
                return
            cw, ch, segs, y_final, subtitle_rgb, karaoke_rgb = layout
            drop = max(28, min(96, ch // 8))
            steps = STUDIO_ELASTIC_LAND_STEPS
            frame_ms = STUDIO_ELASTIC_FRAME_MS

            def tick(step: int) -> None:
                if step > steps:
                    self._paint_canvas_from_layout(
                        cw, ch, segs, y_final, subtitle_rgb, karaoke_rgb
                    )
                    return
                t = step / float(steps)
                prog = CaptionStudio._ease_out_back(t)
                # Land from above (smaller y) with overshoot via ease-out-back
                y = y_final - int((1.0 - prog) * drop)
                self._paint_canvas_from_layout(cw, ch, segs, y, subtitle_rgb, karaoke_rgb)
                nxt = step + 1
                aid = self.after(frame_ms, lambda s=nxt: tick(s))
                self._anim_after_ids.append(aid)

            tick(0)

        def _cancel_play_timer(self) -> None:
            if self._play_after_id is not None:
                try:
                    self.after_cancel(self._play_after_id)
                except (tk.TclError, ValueError):
                    pass
                self._play_after_id = None

        def on_pause(self) -> None:
            self._playing = False
            self._cancel_play_timer()
            self.btn_play.configure(state="normal")
            self.btn_pause.configure(state="disabled")

        def on_play(self) -> None:
            if not self.data or self.slide3_idx < 0:
                messagebox.showinfo("Play", "Load a session JSON first.")
                return
            slide = self.data["slides"][self.slide3_idx]
            if not slide3_cues_list(slide):
                messagebox.showinfo("Play", "No SRT cues to step through.")
                return
            self._playing = True
            self.btn_play.configure(state="disabled")
            self.btn_pause.configure(state="normal")
            self._arm_play_cue_timer()

        def _arm_play_cue_timer(self) -> None:
            self._cancel_play_timer()
            if not self._playing or not self.data or self.slide3_idx < 0:
                return
            slide = self.data["slides"][self.slide3_idx]
            cues = slide3_cues_list(slide)
            if not cues:
                self.on_pause()
                return
            idx = self._cue_index_clamped(slide)
            c = cues[idx]
            try:
                dur_ms = float(c.get("end", 0) or 0) - float(c.get("start", 0) or 0)
            except (TypeError, ValueError):
                dur_ms = 0.0
            if dur_ms <= 0:
                dur_ms = 1500.0
            dur_ms = max(200.0, min(20000.0, dur_ms))

            def _advance() -> None:
                if not self._playing or not self.data:
                    return
                sl = self.data["slides"][self.slide3_idx]
                cq = slide3_cues_list(sl)
                if not cq:
                    self.on_pause()
                    return
                i = self._cue_index_clamped(sl)
                nxt = (i + 1) % len(cq)
                self.cue_idx_var.set(float(nxt))
                sl2 = self.data["slides"][self.slide3_idx]
                self._refresh_cue_meta(sl2)
                self._update_cue_line_label(sl2)
                if self._anim_land_var.get():
                    self._start_land_animation()
                else:
                    self._redraw_static()
                self._arm_play_cue_timer()

            self._play_after_id = self.after(int(dur_ms), _advance)

        def _resolve_cover_path(self) -> None:
            """Prefer Cover.jpg next to the session JSON, else in this script folder."""
            self._invalidate_cover_cache()
            if self.session_path:
                cand = self.session_path.parent / COVER_NAME
                if cand.is_file():
                    self.cover_path = cand
                    self.cover_lbl.configure(text=cand.name, foreground="#000")
                    return
            cand2 = SCRIPT_DIR / COVER_NAME
            if cand2.is_file():
                self.cover_path = cand2
                self.cover_lbl.configure(text=f"{cand2.name} (folder)", foreground="#000")
                return
            self.cover_path = None
            self.cover_lbl.configure(
                text=f"(no {COVER_NAME} — Open cover…)", foreground="#666"
            )

        def on_open_cover(self) -> None:
            p = filedialog.askopenfilename(
                title="Cover image",
                initialdir=str(self.session_path.parent if self.session_path else SCRIPT_DIR),
                filetypes=[
                    ("Images", "*.jpg *.jpeg *.png *.webp *.bmp"),
                    ("All", "*.*"),
                ],
            )
            if p:
                self.cover_path = Path(p)
                self._invalidate_cover_cache()
                self.cover_lbl.configure(text=self.cover_path.name, foreground="#000")
                self.redraw()

        def on_reset_cover(self) -> None:
            """Back to Cover.jpg beside the loaded session (or script folder)."""
            self._resolve_cover_path()
            self.redraw()

        def on_open(self) -> None:
            p = filedialog.askopenfilename(
                title="Carousel session JSON",
                initialdir=str(SCRIPT_DIR),
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
            )
            if p:
                self.load_path(Path(p))

        def load_path(self, path: Path) -> None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                messagebox.showerror("Load failed", str(e))
                return
            if "deckSchemaVersion" not in data:
                messagebox.showerror("Load failed", "Not a Carousel session (no deckSchemaVersion).")
                return
            slides = data.get("slides") or []
            ix = find_slide3_index(slides)
            if ix < 0:
                messagebox.showerror("Load failed", "No slide 3 (symbol) in this session.")
                return
            self.session_path = path
            self.data = data
            self.slide3_idx = ix
            self.path_lbl.configure(text=path.name)
            self.on_pause()
            self._resolve_cover_path()
            slide = slides[ix]
            sc, bt, em, el, sub_col, kar_col = read_studio_knobs_from_slide3(slide)
            self.scale_var.set(sc)
            self.bottom_var.set(bt)
            self.em_var.set(em)
            self._anim_land_var.set(el)
            self.caption_color_var.set(sub_col)
            self.karaoke_color_var.set(kar_col)
            self._configure_cue_slider(slide)
            self._update_cue_line_label(slide)
            self.redraw()

        def on_load_preset(self) -> None:
            preset = load_caption_layout_preset()
            if not preset:
                messagebox.showinfo(
                    "Preset",
                    f"No {CAPTION_LAYOUT_PRESET.name} in this folder.\n"
                    "Use Save after tuning sliders to create it.",
                )
                return
            stub: dict[str, Any] = {"slide3symbol": True}
            apply_caption_layout_preset_to_slide3(stub, preset)
            sc, bt, em, el, sub_col, kar_col = read_studio_knobs_from_slide3(stub)
            self.scale_var.set(sc)
            self.bottom_var.set(bt)
            self.em_var.set(em)
            self._anim_land_var.set(el)
            self.caption_color_var.set(sub_col)
            self.karaoke_color_var.set(kar_col)
            self.redraw()

        def on_load_preset_file(self) -> None:
            p = filedialog.askopenfilename(
                title="Load caption preset JSON",
                initialdir=str(SCRIPT_DIR),
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
            )
            if not p:
                return
            preset = load_caption_layout_preset(Path(p))
            if not preset:
                messagebox.showerror("Preset", "Invalid preset JSON.")
                return
            stub: dict[str, Any] = {"slide3symbol": True}
            apply_caption_layout_preset_to_slide3(stub, preset)
            sc, bt, em, el, sub_col, kar_col = read_studio_knobs_from_slide3(stub)
            self.scale_var.set(sc)
            self.bottom_var.set(bt)
            self.em_var.set(em)
            self._anim_land_var.set(el)
            self.caption_color_var.set(sub_col)
            self.karaoke_color_var.set(kar_col)
            self.redraw()

        def on_save_preset_as(self) -> None:
            if not self.data or self.slide3_idx < 0:
                messagebox.showinfo("Save preset", "Load a session JSON first.")
                return
            p = filedialog.asksaveasfilename(
                title="Save caption preset as",
                initialdir=str(SCRIPT_DIR),
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
                initialfile="caption_layout_preset-custom.json",
            )
            if not p:
                return
            stub: dict[str, Any] = {"slide3symbol": True}
            apply_studio_knobs_to_slide3(
                stub,
                caption_scale_pct=float(self.scale_var.get()),
                bottom_total_pct=float(self.bottom_var.get()),
                em_pct=float(self.em_var.get()),
                caption_color=self.caption_color_var.get(),
                karaoke_color=self.karaoke_color_var.get(),
                elastic_land=bool(self._anim_land_var.get()),
            )
            try:
                out = save_caption_layout_preset(
                    stub, source_session=(self.session_path.name if self.session_path else None), path=Path(p)
                )
            except OSError as e:
                messagebox.showerror("Save preset failed", str(e))
                return
            messagebox.showinfo("Preset saved", f"Wrote {out.name}")

        def _cue_index_clamped(self, slide: dict) -> int:
            n = len(slide3_cues_list(slide))
            if n <= 0:
                return 0
            try:
                i = int(round(float(self.cue_idx_var.get())))
            except (TypeError, ValueError):
                i = 0
            return max(0, min(n - 1, i))

        def _configure_cue_slider(self, slide: dict) -> None:
            cues = slide3_cues_list(slide)
            self._cue_count = len(cues)
            if self._cue_count == 0:
                self.cue_idx_var.set(0.0)
                self.cue_scale.configure(from_=0.0, to=0.0, state="disabled")
                self.cue_meta_lbl.configure(text="")
                return
            self.cue_scale.configure(state="normal")
            last = float(max(0, self._cue_count - 1))
            self.cue_scale.configure(from_=0.0, to=last if last > 0 else 1.0)
            self.cue_idx_var.set(0.0)
            self._refresh_cue_meta(slide)

        def _refresh_cue_meta(self, slide: dict) -> None:
            cues = slide3_cues_list(slide)
            if not cues:
                self.cue_meta_lbl.configure(text="")
                return
            idx = self._cue_index_clamped(slide)
            tr = format_cue_time_range_s(cues[idx])
            extra = f" · {tr}" if tr else ""
            self.cue_meta_lbl.configure(text=f"{idx + 1} / {len(cues)}{extra}")

        def _update_cue_line_label(self, slide: dict) -> None:
            idx = self._cue_index_clamped(slide)
            line = caption_preview_line_from_slide(slide, idx)
            if self._cue_count == 0:
                self.line_lbl.configure(
                    text="(No SRT text on slide 3 — add captions in the browser, then re-export.)"
                )
            elif not line:
                self.line_lbl.configure(
                    text=f"Cue {idx + 1}: (empty — pick another cue)"
                )
            else:
                self.line_lbl.configure(text=f"Cue {idx + 1}, first line: {line}")

        def _on_cue_scrub(self, _val: str | None = None) -> None:
            if self._playing:
                self.on_pause()
            if not self.data or self.slide3_idx < 0:
                return
            self._cancel_land_animation()
            slide = self.data["slides"][self.slide3_idx]
            self._refresh_cue_meta(slide)
            self._update_cue_line_label(slide)
            self._redraw_static()

        def _preview_dims(self) -> tuple[int, int, float]:
            assert self.data is not None
            fmt = self.data.get("fmt")
            globs = self.data.get("globals") if isinstance(self.data.get("globals"), dict) else None
            ew, eh = get_export_pixel_size(fmt, globs)
            cw = 400
            ch = max(200, int(round(cw * eh / ew)))
            return ew, eh, cw / float(ew)

        def _draw_text_with_outline(
            self,
            draw: Any,
            xy: tuple[int, int],
            text: str,
            *,
            font: Any,
            fill: tuple[int, int, int] = (17, 17, 17),
            outline: tuple[int, int, int] = (255, 255, 255),
        ) -> None:
            x, y = xy
            for ox, oy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, -1), (-1, 0), (1, 0), (0, 1)):
                draw.text((x + ox, y + oy), text, fill=outline, font=font)
            draw.text((x, y), text, fill=fill, font=font)

        def redraw(self) -> None:
            self._cancel_land_animation()
            self._redraw_static()

        def _pick_color(self, target_var: Any) -> None:
            cur = normalize_hex_color(target_var.get()) or "#111111"
            _rgb, hx = colorchooser.askcolor(color=cur, parent=self)
            if hx:
                target_var.set(hx.upper())

        def _compute_preview_layout(
            self,
        ) -> tuple[int, int, list[tuple[str, Any, bool]], int, tuple[int, int, int], tuple[int, int, int]] | None:
            """
            Returns cw, ch, [(text, font, is_emph), ...], y_top, subtitle RGB, karaoke RGB.
            """
            if not self.data or self.slide3_idx < 0:
                return None
            slides = self.data.get("slides") or []
            slide = slides[self.slide3_idx]
            idx = self._cue_index_clamped(slide)
            line = caption_preview_line_from_slide(slide, idx)
            ew, eh, _px = self._preview_dims()
            cap_scale = float(self.scale_var.get())
            bottom_pct = float(self.bottom_var.get())
            em_pct = float(self.em_var.get())
            self.scale_v.configure(text=f"{cap_scale:.0f}")
            self.bottom_v.configure(text=f"{bottom_pct:.1f}")
            self.em_v.configure(text=f"{em_pct:.0f}")

            cw = 400
            ch = max(200, int(round(cw * eh / ew)))
            margin_px = max(0, int(round(ch * (bottom_pct / 100.0))))

            boost = slide.get("slide3CaptionBoostWords")
            raw_preview = re.sub(r"<[^>]+>", "", line or "")
            t = apply_boost_words(raw_preview, boost)
            frac = 0.034 * max(0.4, cap_scale / 100.0)
            base_px = max(16, min(int(round(ch * frac)), ch // 2))
            try:
                em = max(1.0, float(em_pct) / 100.0)
            except (TypeError, ValueError):
                em = 1.3
            emph_px = max(16, min(int(round(base_px * em)), ch // 2))
            font_reg, font_emph = studio_pair_fonts(base_px, emph_px)
            parts = re.split(r"(\*\*[^*]+\*\*)", t)
            segs: list[tuple[str, Any, bool]] = []
            for p in parts:
                if not p:
                    continue
                mm = re.match(r"^\*\*([^*]+)\*\*$", p)
                if mm:
                    segs.append((mm.group(1), font_emph, True))
                else:
                    segs.append((p, font_reg, False))
            if not segs:
                segs = [("Caption preview", font_reg, False)]

            def _seg_w(ft: Any, tx: str) -> float:
                if hasattr(ft, "getlength"):
                    return float(ft.getlength(tx))
                bb = ft.getbbox(tx)
                return float(bb[2] - bb[0])

            def _seg_h(ft: Any, tx: str) -> float:
                bb = ft.getbbox(tx)
                return float(bb[3] - bb[1])

            max_h = max(_seg_h(f, tx) for tx, f, _is_em in segs)
            y_top = ch - margin_px - int(max_h)
            y_top = max(0, min(ch - int(max_h), y_top))
            sub_hex = normalize_hex_color(self.caption_color_var.get()) or "#111111"
            kar_hex = normalize_hex_color(self.karaoke_color_var.get()) or "#111111"
            sub_rgb = tuple(int(sub_hex[i : i + 2], 16) for i in (1, 3, 5))
            kar_rgb = tuple(int(kar_hex[i : i + 2], 16) for i in (1, 3, 5))
            return (cw, ch, segs, y_top, sub_rgb, kar_rgb)

        def _paint_canvas_from_layout(
            self,
            cw: int,
            ch: int,
            segs: list[tuple[str, Any, bool]],
            y_top: int,
            subtitle_rgb: tuple[int, int, int],
            karaoke_rgb: tuple[int, int, int],
        ) -> None:
            self.canvas.configure(width=cw, height=ch)
            img = self._compose_base_image(cw, ch).copy()
            draw = ImageDraw.Draw(img)

            def _seg_h(ft: Any, tx: str) -> float:
                bb = ft.getbbox(tx)
                return float(bb[3] - bb[1])

            def _seg_w(ft: Any, tx: str) -> float:
                if hasattr(ft, "getlength"):
                    return float(ft.getlength(tx))
                bb = ft.getbbox(tx)
                return float(bb[2] - bb[0])

            max_h = max(_seg_h(f, tx) for tx, f, _is_em in segs)
            total_w = sum(_seg_w(f, tx) for tx, f, _is_em in segs)
            cursor = (cw - int(total_w)) // 2
            outline = (255, 255, 255)
            for tx, font, is_em in segs:
                h = _seg_h(font, tx)
                dy = int(max_h - h)
                x, y = cursor, y_top + dy
                fill = karaoke_rgb if is_em else subtitle_rgb
                for ox, oy in (
                    (-1, -1),
                    (1, -1),
                    (-1, 1),
                    (1, 1),
                    (0, -1),
                    (-1, 0),
                    (1, 0),
                    (0, 1),
                ):
                    draw.text((x + ox, y + oy), tx, fill=outline, font=font)
                draw.text((x, y), tx, fill=fill, font=font)
                cursor += int(_seg_w(font, tx))

            self._photo = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")

        def _redraw_static(self) -> None:
            if not self.data or self.slide3_idx < 0:
                self._redraw_cover_only()
                return
            layout = self._compute_preview_layout()
            if layout is None:
                return
            cw, ch, segs, y_top, subtitle_rgb, karaoke_rgb = layout
            self._paint_canvas_from_layout(cw, ch, segs, y_top, subtitle_rgb, karaoke_rgb)

        def _compose_base_image(self, cw: int, ch: int) -> Any:
            """Resize cover to preview size (cached), or light gray placeholder."""
            key: tuple[Any, ...]
            if self.cover_path and self.cover_path.is_file():
                try:
                    st = self.cover_path.stat()
                    key = (str(self.cover_path.resolve()), cw, ch, st.st_mtime_ns, st.st_size)
                except OSError:
                    key = (str(self.cover_path), cw, ch)
            else:
                key = ("", cw, ch)

            if self._cover_cache_key == key and self._cover_cache_img is not None:
                return self._cover_cache_img

            if self.cover_path and self.cover_path.is_file():
                try:
                    base = Image.open(self.cover_path).convert("RGB")
                    base = base.resize((cw, ch), _pil_resample)
                    self._cover_cache_key = key
                    self._cover_cache_img = base
                    return base
                except OSError:
                    pass
            blank = Image.new("RGB", (cw, ch), (245, 245, 245))
            self._cover_cache_key = key
            self._cover_cache_img = blank
            return blank

        def _redraw_cover_only(self) -> None:
            """Session not loaded: still show cover if we have one."""
            cw, ch = 400, 500
            if self.cover_path and self.cover_path.is_file():
                try:
                    im = Image.open(self.cover_path).convert("RGB")
                    w0, h0 = im.size
                    if w0 > 0 and h0 > 0:
                        ch = max(200, int(round(cw * h0 / w0)))
                except OSError:
                    pass
            self.canvas.configure(width=cw, height=ch)
            img = self._compose_base_image(cw, ch)
            self._photo = ImageTk.PhotoImage(img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")

        def on_save(self) -> None:
            if not self.data or self.slide3_idx < 0 or not self.session_path:
                messagebox.showinfo("Save", "Load a session JSON first.")
                return
            slides = self.data.get("slides") or []
            slide = slides[self.slide3_idx]
            apply_studio_knobs_to_slide3(
                slide,
                caption_scale_pct=float(self.scale_var.get()),
                bottom_total_pct=float(self.bottom_var.get()),
                em_pct=float(self.em_var.get()),
                caption_color=self.caption_color_var.get(),
                karaoke_color=self.karaoke_color_var.get(),
                elastic_land=bool(self._anim_land_var.get()),
            )
            try:
                self.session_path.write_text(
                    json.dumps(self.data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                preset_path = save_caption_layout_preset(
                    slide, source_session=self.session_path.name
                )
                carried_cover = False
                if (
                    self._carry_cover_var.get()
                    and self.cover_path
                    and self.cover_path.is_file()
                ):
                    dst = self.session_path.parent / COVER_NAME
                    if self.cover_path.resolve() != dst.resolve():
                        shutil.copy2(self.cover_path, dst)
                    carried_cover = True
            except OSError as e:
                messagebox.showerror("Save failed", str(e))
                return
            messagebox.showinfo(
                "Saved",
                f"Wrote {self.session_path.name}\n"
                f"Template defaults: {preset_path.name} (batch uses this automatically)."
                + (
                    f"\nCover: copied to {COVER_NAME} for local render folder."
                    if carried_cover
                    else ""
                ),
            )

    app = CaptionStudio()
    app.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Carousel batch: CSV + base_session.json → mp4-out/, or legacy folder JSONs."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help=f"variants.csv (default tries {DEFAULT_VARIANTS_CSV.name} beside this script)",
    )
    parser.add_argument(
        "--base",
        type=Path,
        default=DEFAULT_BASE_SESSION,
        help="Full session JSON exported from the browser (merged with each row)",
    )
    parser.add_argument(
        "--carousel-root",
        type=Path,
        default=CAROUSEL_ROOT,
        help="Folder containing covers/, audio/, srt/, mp4-out/",
    )
    parser.add_argument(
        "--transcribe-only",
        action="store_true",
        help="Whisper only: fill srt/<symbolName>.srt from audio/; no FFmpeg",
    )
    parser.add_argument(
        "--legacy-folder",
        action="store_true",
        help=f"Ignore CSV; use {COVER_NAME} + *.json in {SCRIPT_DIR.name} (original behavior).",
    )
    parser.add_argument(
        "--studio",
        action="store_true",
        help="Tkinter caption studio: first SRT line on white, tune sliders, Save → session JSON (needs Pillow).",
    )
    parser.add_argument(
        "--studio-session",
        type=Path,
        default=None,
        help=f"Session JSON to open with --studio (default: {DEFAULT_BASE_SESSION.name} in this folder).",
    )
    args = parser.parse_args()

    if args.studio:
        run_caption_studio(args.studio_session)
        return

    if args.legacy_folder:
        process_session_folder(SCRIPT_DIR)
        return

    csv_path = args.csv if args.csv is not None else DEFAULT_VARIANTS_CSV
    base_ready = args.base.is_file()
    if csv_path.is_file() and (args.transcribe_only or base_ready):
        process_csv_pipeline(
            csv_path,
            args.base,
            args.carousel_root,
            transcribe_only=args.transcribe_only,
        )
        return

    if args.transcribe_only:
        print(f"Missing CSV: {csv_path}")
        return

    process_session_folder(SCRIPT_DIR)


if __name__ == "__main__":
    main()
