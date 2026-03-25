"""
ChoralSplit backend — FastAPI + Audiveris + music21
"""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="ChoralSplit API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# Where Audiveris is installed (set via env var or override here)
AUDIVERIS_CMD = os.environ.get("AUDIVERIS_CMD", "/opt/audiveris/bin/Audiveris")

# Temp directory for jobs (cleaned up after response)
JOBS_DIR = Path(tempfile.gettempdir()) / "choralsplit_jobs"
JOBS_DIR.mkdir(exist_ok=True)

# Serve generated files
app.mount("/files", StaticFiles(directory=str(JOBS_DIR)), name="files")


# ── PDF preprocessing ────────────────────────────────────────────────────────

def preprocess_pdf(pdf_path: Path, deskew: bool = False, to_bw: bool = False) -> Path:
    """Apply optional deskew and/or B&W conversion using ImageMagick.
    Returns the path to the preprocessed PDF (or original if nothing to do)."""
    if not deskew and not to_bw:
        return pdf_path
    out_path = pdf_path.with_name("score_preprocessed.pdf")
    cmd = [
        "convert",
        str(pdf_path),
        "-quality", "100",
        "-compress", "lossless",
    ]
    if deskew:
        cmd += ["-deskew", "40%"]
    if to_bw:
        cmd += ["-colorspace", "Gray", "-threshold", "50%"]
    cmd.append(str(out_path))
    subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    return out_path


# ── Audiveris ────────────────────────────────────────────────────────────────

def run_audiveris(pdf_path: Path, output_dir: Path) -> tuple[list[Path], list[dict]]:
    """Run Audiveris in batch mode.
    Returns (xml_files, error_bars) where error_bars is a list of
    {bar: int, message: str} parsed from Audiveris warnings/errors."""
    cmd = [
        AUDIVERIS_CMD,
        "-batch",
        "-export",
        "-option", "org.audiveris.omr.text.tesseract.TesseractOCR.useOCR=false",
        "-option", "org.audiveris.omr.sheet.ProcessingSwitches.dynamics=false",
        "-output", str(output_dir),
        "--",
        str(pdf_path),
    ]
    env = os.environ.copy()
    env["JAVA_TOOL_OPTIONS"] = "-Djava.awt.headless=true"
    result = subprocess.run(cmd, capture_output=True,
                            text=True, timeout=480, env=env)

    if result.returncode != 0:
        raise RuntimeError(
            f"Audiveris failed (exit {result.returncode}):\n"
            + result.stderr[-3000:]
        )

    xml_files = list(output_dir.rglob("*.xml")) + \
        list(output_dir.rglob("*.mxl"))
    if not xml_files:
        raise RuntimeError(
            "Audiveris completed but produced no MusicXML output. "
            "The score may be a scan rather than typeset, or the PDF is corrupt."
        )

    # Parse log for measure-level warnings/errors
    full_log = result.stdout + result.stderr
    for lf in output_dir.rglob("*.log"):
        try:
            full_log += lf.read_text(errors="ignore")
        except Exception:
            pass
    error_bars = _parse_audiveris_error_bars(full_log)
    return xml_files, error_bars


def _parse_audiveris_error_bars(log_text: str) -> list[dict]:
    """Extract bar numbers with WARN/ERROR from Audiveris logs."""
    issues: list[dict] = []
    seen: set[tuple] = set()
    for line in log_text.splitlines():
        if not re.search(r'\bWARN|\bERROR', line, re.IGNORECASE):
            continue
        m = re.search(r'[Mm]easure\s*#?\s*(\d+)', line)
        if not m:
            m = re.search(r'\bbar\s+#?(\d+)\b', line, re.IGNORECASE)
        if not m:
            continue
        bar_num = int(m.group(1))
        # Trim to just the meaningful part of the message
        msg = line.strip()
        for sep in (' - ', '] '):
            if sep in msg:
                msg = msg.rsplit(sep, 1)[-1]
        msg = msg[:120]
        key = (bar_num, msg[:40])
        if key not in seen:
            seen.add(key)
            issues.append({"bar": bar_num, "message": msg})
    return sorted(issues, key=lambda x: x["bar"])


def render_pdf_pages(pdf_path: Path, output_dir: Path) -> list[Path]:
    """Render each PDF page to a JPEG at 150 DPI using ImageMagick."""
    subprocess.run(
        ["convert", "-density", "150", str(pdf_path),
         "-quality", "85", str(output_dir / "orig-%02d.jpg")],
        check=True, capture_output=True, timeout=120,
    )
    pages = sorted(output_dir.glob("orig-*.jpg"))
    if not pages:
        # Single-page fallback: ImageMagick may omit the number suffix
        single = output_dir / "orig.jpg"
        if single.exists():
            renamed = output_dir / "orig-00.jpg"
            single.rename(renamed)
            pages = [renamed]
    return pages


def render_musicxml_svg(
    xml_path: Path, output_dir: Path, error_bars: list[int] | None = None,
) -> list[Path]:
    """Render MusicXML to per-page SVG files using Verovio.
    If error_bars is provided, measures with those numbers get a red highlight."""
    import verovio
    vrv = verovio.toolkit()
    vrv.setOptions(json.dumps({
        "pageWidth": 2100,
        "pageHeight": 2970,
        "adjustPageHeight": False,
        "scale": 40,
        "footer": "none",
        "header": "none",
        "breaks": "auto",
        "svgViewBox": True,
    }))
    vrv.loadFile(str(xml_path))
    error_set = set(error_bars or [])
    pages = []
    for i in range(1, vrv.getPageCount() + 1):
        svg_text = vrv.renderToSVG(i)
        if error_set:
            svg_text = _highlight_error_bars_in_svg(svg_text, error_set)
        svg_path = output_dir / f"score-{i:02d}.svg"
        svg_path.write_text(svg_text)
        pages.append(svg_path)
    return pages


def _highlight_error_bars_in_svg(svg_text: str, error_bars: set[int]) -> str:
    """Inject semi-transparent red rectangles behind measures flagged with errors.
    Verovio emits <g class="measure" ...> elements with an id like 'measure-MxxxxxxN'
    where N encodes the measure number."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return svg_text  # don't break if SVG is malformed

    ns = {'svg': 'http://www.w3.org/2000/svg'}
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')

    for g in root.iter('{http://www.w3.org/2000/svg}g'):
        cls = g.get('class', '')
        if 'measure' not in cls:
            continue
        # Try to extract measure number from id or from a data attribute
        gid = g.get('id', '')
        m = re.search(r'(\d+)$', gid)
        if not m:
            # Try Verovio's newer naming
            m = re.search(r'measure-(\d+)', gid)
        if not m:
            continue
        measure_num = int(m.group(1))
        if measure_num not in error_bars:
            continue
        # Compute bounding box from child elements
        bbox = _svg_group_bbox(g)
        if bbox is None:
            continue
        x, y, w, h = bbox
        rect = ET.SubElement(g, '{http://www.w3.org/2000/svg}rect')
        rect.set('x', str(x - 2))
        rect.set('y', str(y - 2))
        rect.set('width', str(w + 4))
        rect.set('height', str(h + 4))
        rect.set('fill', 'rgba(255,60,60,0.18)')
        rect.set('stroke', '#ff3c3c')
        rect.set('stroke-width', '1.5')
        rect.set('rx', '3')
        # Insert as first child so it sits behind the notes
        g.insert(0, rect)

    return ET.tostring(root, encoding='unicode')


def _svg_group_bbox(g) -> tuple[float, float, float, float] | None:
    """Rough bounding box from x/y/width/height attrs of children."""
    xs, ys, x2s, y2s = [], [], [], []
    for el in g.iter():
        try:
            ex = float(el.get('x', 'nan'))
            ey = float(el.get('y', 'nan'))
            xs.append(ex)
            ys.append(ey)
            ew = float(el.get('width', '0'))
            eh = float(el.get('height', '0'))
            x2s.append(ex + ew)
            y2s.append(ey + eh)
        except (ValueError, TypeError):
            continue
    if not xs:
        return None
    min_x, min_y = min(xs), min(ys)
    max_x = max(x2s) if x2s else max(xs)
    max_y = max(y2s) if y2s else max(ys)
    return (min_x, min_y, max_x - min_x, max_y - min_y)


# ── music21 splitting ────────────────────────────────────────────────────────

def split_parts(xml_path: Path, output_dir: Path, part_count: str, bpm: int | None = None, corrections_text: str | None = None) -> dict:
    """
    Parse MusicXML with music21, strip dynamics, apply corrections,
    optionally override tempo, split by part, write one MIDI per part + an all-parts MIDI.
    Returns a dict with 'parts' list and 'all_midi_path'.
    """
    import music21  # imported here so startup is fast if music21 is missing

    score = music21.converter.parse(str(xml_path))
    parts = score.parts

    if not parts:
        raise RuntimeError("No parts found in the MusicXML output.")

    # Remove all dynamic markings from the score
    for el in score.recurse():
        if isinstance(el, music21.dynamics.Dynamic):
            el.activeSite.remove(el)

    # Apply corrections (key, tempo, time signature) if provided
    if corrections_text:
        instructions = parse_corrections_with_llm(corrections_text)
        apply_corrections(score, instructions)

    # Override tempo globally if the BPM field was set
    if bpm:
        for el in score.recurse():
            if isinstance(el, music21.tempo.MetronomeMark):
                el.activeSite.remove(el)
        for part in parts:
            part.insert(0, music21.tempo.MetronomeMark(number=bpm))

    # Extract individual voices from parts (important for closed scores
    # where e.g. soprano+alto share a staff, tenor+bass share a staff).
    # If Audiveris returned fewer parts than expected, try voicesToParts().
    target = None
    if part_count != "auto":
        try:
            target = int(part_count)
        except ValueError:
            pass

    if target and len(parts) < target:
        expanded = []
        for part in parts:
            try:
                voices = part.voicesToParts()
                if len(voices) > 1:
                    expanded.extend(voices.parts)
                else:
                    expanded.append(part)
            except Exception:
                expanded.append(part)
        if len(expanded) > len(parts):
            # Rebuild the score with expanded parts
            new_score = music21.stream.Score()
            for p in expanded:
                new_score.insert(0, p)
            # Carry over tempo/key from original
            score = new_score
            parts = score.parts

    # Write full score (all parts) as MIDI
    # Add metronome click track to the full score
    click_part = _make_click_track(score)
    score.insert(0, click_part)
    all_midi_path = output_dir / "All parts.mid"
    score.write("midi", fp=str(all_midi_path))

    # Split into individual parts
    results = []
    seen_names: dict[str, int] = {}
    for i, part in enumerate(parts):
        part_name = _part_name(part, i)
        safe = _safe_filename(part_name)

        # De-duplicate: "Voice", "Voice 2", "Voice 3", …
        if safe in seen_names:
            seen_names[safe] += 1
            safe = f"{safe} {seen_names[safe]}"
            part_name = f"{part_name} {seen_names[safe.rsplit(' ', 1)[0]]}"
        else:
            seen_names[safe] = 1

        midi_filename = f"{safe}.mid"
        midi_path = output_dir / midi_filename

        # Wrap in a fresh Score with the click track
        single = music21.stream.Score()
        single.append(part)
        single.insert(0, _make_click_track_for_part(part))
        single.write("midi", fp=str(midi_path))

        note_count = sum(
            1 for el in part.flat.notes
            if el.isNote or el.isChord
        )

        results.append({
            "name": part_name,
            "midi_path": midi_path,
            "note_count": note_count,
        })

    return {"parts": results, "all_midi_path": all_midi_path}


def _part_name(part, index: int) -> str:
    """Extract a human-readable name from a music21 Part."""
    import music21
    try:
        instr = part.getInstrument()
        if instr and instr.partName:
            return instr.partName
    except Exception:
        pass
    # Fallback to part ID or generic label
    return part.id or f"Part {index + 1}"


def _safe_filename(name: str) -> str:
    """Strip characters that are unsafe in filenames."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()


# ── Click track (woodblock metronome) ────────────────────────────────────────

# MIDI percussion: High Wood Block = 76, Low Wood Block = 77
# We use channel 10 (percussion) via music21's Unpitched percussion support.
CLICK_BEAT1_PITCH = 76   # high wood block – emphasis on beat 1
CLICK_BEATN_PITCH = 77   # low wood block  – other beats
CLICK_BEAT1_VEL = 60     # subtle but audible
CLICK_BEATN_VEL = 40     # quieter for non-downbeats


def _make_click_track(score) -> "music21.stream.Part":
    """Build a percussion click track spanning the full score, using the
    first part's time signatures to determine beats per measure."""
    return _make_click_track_for_part(score.parts[0])


def _make_click_track_for_part(source_part) -> "music21.stream.Part":
    """Build a woodblock click track matching the measures/time-sigs of a part."""
    import music21

    click = music21.stream.Part()
    click.partName = "Click"
    # Set to a Woodblock instrument so music21 routes it to MIDI channel 10
    wb = music21.instrument.Woodblock()
    click.insert(0, wb)

    for measure in source_part.getElementsByClass("Measure"):
        click_m = music21.stream.Measure(number=measure.number)
        click_m.offset = measure.offset

        # Get the active time signature for this measure
        ts = measure.getContextByClass(music21.meter.TimeSignature)
        if ts is None:
            ts = music21.meter.TimeSignature("4/4")

        beats = ts.numerator
        beat_dur = music21.duration.Duration(4.0 / ts.denominator)

        for beat_num in range(beats):
            if beat_num == 0:
                pitch = CLICK_BEAT1_PITCH
                vel = CLICK_BEAT1_VEL
            else:
                pitch = CLICK_BEATN_PITCH
                vel = CLICK_BEATN_VEL

            n = music21.note.Unpitched()
            n.midi = pitch
            n.duration = beat_dur
            n.volume = music21.volume.Volume(velocity=vel)
            n.storedInstrument = wb
            click_m.insert(beat_dur.quarterLength * beat_num, n)

        click.insert(measure.offset, click_m)

    return click


# ── Score corrections (via Anthropic Haiku) ─────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def parse_corrections_with_llm(user_text: str) -> list[dict]:
    """Use Claude Haiku to parse natural-language score correction descriptions
    into structured instructions for key, tempo, and time signature changes."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not configured on the server.")

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model="claude-haiku-4-20250414",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_text}],
        system=(
            "You are a music theory assistant. The user will describe corrections "
            "to a choral score — key changes, tempo changes, and/or time signature "
            "changes. Parse their description into a JSON array of objects.\n\n"
            "Each object must have:\n"
            "  - \"bar\": integer bar/measure number (1-based). "
            "If the user says 'starts at' or 'from the beginning' without a bar number, use 1.\n"
            "  - \"type\": one of \"key\", \"tempo\", or \"time_signature\"\n"
            "  - \"value\": the value to set, formatted as follows:\n\n"
            "For type=\"key\": a music21 key string.\n"
            "  sharps = '#', flats = '-'. "
            "  e.g. Bb major = \"B- major\", F# minor = \"f# minor\", "
            "Eb minor = \"e- minor\", D major = \"D major\".\n\n"
            "For type=\"tempo\": an integer BPM, e.g. 120\n\n"
            "For type=\"time_signature\": a time signature string, e.g. \"3/4\", \"6/8\"\n\n"
            "Examples:\n"
            "  User: 'Starts in Bb major at 100bpm, 3/4 time. Modulates to D major at bar 33. "
            "Tempo change to 80bpm at bar 50.'\n"
            "  Output: [{\"bar\":1,\"type\":\"key\",\"value\":\"B- major\"}, "
            "{\"bar\":1,\"type\":\"tempo\",\"value\":100}, "
            "{\"bar\":1,\"type\":\"time_signature\",\"value\":\"3/4\"}, "
            "{\"bar\":33,\"type\":\"key\",\"value\":\"D major\"}, "
            "{\"bar\":50,\"type\":\"tempo\",\"value\":80}]\n\n"
            "Return ONLY the JSON array, nothing else."
        ),
    )

    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


def apply_corrections(score, instructions: list[dict]):
    """Apply parsed correction instructions to a music21 score.
    Each instruction is {bar: int, type: 'key'|'tempo'|'time_signature', value: ...}."""
    import music21

    for instr in instructions:
        bar_num = instr["bar"]
        change_type = instr["type"]
        value = instr["value"]

        for part in score.parts:
            for measure in part.getElementsByClass("Measure"):
                if measure.number == bar_num:
                    if change_type == "key":
                        for ks in measure.getElementsByClass("KeySignature"):
                            measure.remove(ks)
                        measure.insert(0, music21.key.Key(value))

                    elif change_type == "tempo":
                        for mm in measure.getElementsByClass("MetronomeMark"):
                            measure.remove(mm)
                        measure.insert(
                            0, music21.tempo.MetronomeMark(number=int(value)))

                    elif change_type == "time_signature":
                        for ts in measure.getElementsByClass("TimeSignature"):
                            measure.remove(ts)
                        measure.insert(0, music21.meter.TimeSignature(value))

                    break

    # Re-spell accidentals based on the corrected key signatures
    has_key_changes = any(i["type"] == "key" for i in instructions)
    if has_key_changes:
        for part in score.parts:
            part.makeAccidentals(inPlace=True, overrideStatus=True)


# ── ZIP helper ───────────────────────────────────────────────────────────────

def make_zip(midi_files: list[Path], zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for mf in midi_files:
            zf.write(mf, mf.name)


SOUNDFONT = "/usr/share/sounds/sf2/FluidR3_GM.sf2"


def midi_to_mp3(midi_path: Path) -> Path:
    """Convert a MIDI file to MP3 via fluidsynth (WAV) + ffmpeg."""
    wav_path = midi_path.with_suffix(".wav")
    mp3_path = midi_path.with_suffix(".mp3")

    subprocess.run(
        ["fluidsynth", "-ni", SOUNDFONT,
            str(midi_path), "-F", str(wav_path), "-r", "44100"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path), "-q:a", "2", str(mp3_path)],
        check=True, capture_output=True,
    )
    wav_path.unlink(missing_ok=True)
    return mp3_path


# ── Route ────────────────────────────────────────────────────────────────────

@app.post("/api/split")
async def split_score(
    file: UploadFile = File(...),
    score_format: str = Form("auto"),
    part_count: str = Form("auto"),
    tempo_bpm: str = Form(""),
    corrections: str = Form(""),
    deskew: str = Form("0"),
    bw: str = Form("0"),
    preprocess_only: str = Form("0"),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, detail="Only PDF files are accepted.")

    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True)

    try:
        # 1. Save uploaded PDF
        pdf_path = job_dir / "score.pdf"
        with pdf_path.open("wb") as f:
            content = await file.read()
            f.write(content)

        # 2. Preprocess PDF to 300 DPI for better OMR accuracy
        pdf_path = preprocess_pdf(
            pdf_path,
            deskew=deskew == "1",
            to_bw=bw == "1",
        )

        # Early return: just serve the preprocessed PDF
        if preprocess_only == "1":
            return {
                "preprocess_pdf_url": f"/files/{job_id}/{pdf_path.name}"
            }

        # 3. Run Audiveris
        audiveris_out = job_dir / "audiveris"
        audiveris_out.mkdir()
        xml_files, error_bars = run_audiveris(pdf_path, audiveris_out)
        # take first (multi-page scores may produce one file)
        xml_path = xml_files[0]

        # Render score preview: original PDF pages as JPEGs + Verovio SVGs
        error_bar_nums = [e["bar"] for e in error_bars]
        preview = None
        try:
            preview_dir = job_dir / "preview"
            preview_dir.mkdir()
            pdf_page_paths = render_pdf_pages(pdf_path, preview_dir)
            svg_page_paths = render_musicxml_svg(
                xml_path, preview_dir, error_bars=error_bar_nums,
            )
            preview = {
                "pdf_pages": [f"/files/{job_id}/preview/{p.name}" for p in pdf_page_paths],
                "svg_pages": [f"/files/{job_id}/preview/{p.name}" for p in svg_page_paths],
            }
        except Exception:
            pass  # preview is optional; never break the main pipeline

        # 4. Split parts with music21
        midi_out = job_dir / "midi"
        midi_out.mkdir()
        bpm = int(tempo_bpm) if tempo_bpm.strip().isdigit() else None
        split_result = split_parts(
            xml_path, midi_out, part_count, bpm,
            corrections.strip() if corrections else None,
        )
        parts = split_result["parts"]
        all_midi_path = split_result["all_midi_path"]

        # 4. Convert MIDI to MP3
        for p in parts:
            p["mp3_path"] = midi_to_mp3(p["midi_path"])
        all_mp3_path = midi_to_mp3(all_midi_path)

        # 5. Build ZIP containing both MIDI and MP3 files
        zip_path = job_dir / "all_parts.zip"
        all_files = (
            [p["midi_path"] for p in parts]
            + [p["mp3_path"] for p in parts]
            + [all_midi_path, all_mp3_path]
        )
        make_zip(all_files, zip_path)

        # 6. Return JSON with download URLs
        return {
            "job_id": job_id,
            "parts": [
                {
                    "name": p["name"],
                    "mp3_url": f"/files/{job_id}/midi/{p['mp3_path'].name}",
                    "note_count": p["note_count"],
                }
                for p in parts
            ],
            "all_mp3_url": f"/files/{job_id}/midi/{all_mp3_path.name}",
            "zip_url": f"/files/{job_id}/all_parts.zip",
            "preview": preview,
            "error_bars": error_bars,
        }

    except RuntimeError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(422, detail=str(exc))
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, detail=f"Unexpected error: {exc}")


@app.post("/api/reprocess")
async def reprocess_score(
    job_id: str = Form(...),
    corrections: str = Form(""),
    tempo_bpm: str = Form(""),
    part_count: str = Form("auto"),
):
    """Re-run music21 splitting on an existing job's MusicXML with new corrections.
    Skips Audiveris entirely — just re-processes the existing XML."""
    job_dir = JOBS_DIR / job_id
    if not job_dir.exists():
        raise HTTPException(
            404, detail="Job not found — the server may have restarted. Please re-upload the PDF.")

    audiveris_out = job_dir / "audiveris"
    xml_files = list(audiveris_out.rglob("*.xml")) + \
        list(audiveris_out.rglob("*.mxl"))
    if not xml_files:
        raise HTTPException(
            404, detail="Original MusicXML not found. Please re-upload the PDF.")
    xml_path = xml_files[0]

    try:
        # Clear old MIDI/MP3 output
        midi_out = job_dir / "midi"
        if midi_out.exists():
            shutil.rmtree(midi_out)
        midi_out.mkdir()

        bpm = int(tempo_bpm) if tempo_bpm.strip().isdigit() else None
        split_result = split_parts(
            xml_path, midi_out, part_count, bpm,
            corrections.strip() if corrections else None,
        )
        parts = split_result["parts"]
        all_midi_path = split_result["all_midi_path"]

        for p in parts:
            p["mp3_path"] = midi_to_mp3(p["midi_path"])
        all_mp3_path = midi_to_mp3(all_midi_path)

        zip_path = job_dir / "all_parts.zip"
        all_files = (
            [p["midi_path"] for p in parts]
            + [p["mp3_path"] for p in parts]
            + [all_midi_path, all_mp3_path]
        )
        make_zip(all_files, zip_path)

        return {
            "job_id": job_id,
            "parts": [
                {
                    "name": p["name"],
                    "mp3_url": f"/files/{job_id}/midi/{p['mp3_path'].name}",
                    "note_count": p["note_count"],
                }
                for p in parts
            ],
            "all_mp3_url": f"/files/{job_id}/midi/{all_mp3_path.name}",
            "zip_url": f"/files/{job_id}/all_parts.zip",
        }

    except RuntimeError as exc:
        raise HTTPException(422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(500, detail=f"Unexpected error: {exc}")


@app.get("/health")
def health():
    return {"status": "ok"}
