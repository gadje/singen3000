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


def render_musicxml_svg(xml_path: Path, output_dir: Path) -> list[Path]:
    """Render MusicXML to per-page SVG files using Verovio.
    Error bar highlighting is handled client-side."""
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
    pages = []
    for i in range(1, vrv.getPageCount() + 1):
        svg_path = output_dir / f"score-{i:02d}.svg"
        svg_path.write_text(vrv.renderToSVG(i))
        pages.append(svg_path)
    return pages


def strip_dynamics_from_xml(xml_path: Path, out_path: Path) -> None:
    """Write a copy of the MusicXML with all dynamic/hairpin directions removed.
    Audiveris sometimes misreads notes as dynamics; stripping them gives a cleaner
    SVG preview and avoids spurious MIDI volume changes."""
    import xml.etree.ElementTree as ET

    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""

    dyn_tags = {f"{ns}dynamics", f"{ns}wedge", f"{ns}dashes"}

    for measure in root.iter(f"{ns}measure"):
        to_remove = []
        for direction in list(measure):
            if direction.tag != f"{ns}direction":
                continue
            # Check every direction-type child — remove the direction only if
            # ALL its direction-type children are pure dynamics/hairpin content.
            dir_types = direction.findall(f"{ns}direction-type")
            if not dir_types:
                continue
            all_dyn = all(
                all(child.tag in dyn_tags for child in dt)
                for dt in dir_types
            )
            if all_dyn:
                to_remove.append(direction)
        for el in to_remove:
            measure.remove(el)

    tree.write(str(out_path), xml_declaration=True, encoding="UTF-8")


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
    """Use Claude Haiku to parse natural-language score corrections
    into structured instructions (key, tempo, time sig, and note-level edits)."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not configured on the server.")

    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": user_text}],
        system=(
            "You are a music theory assistant. The user will describe corrections "
            "to a choral score. Parse their description into a JSON array of objects.\n\n"
            "Each object must have:\n"
            "  - \"bar\": integer bar/measure number (1-based).\n"
            "  - \"part\": which voice/part to edit. Use a string like \"Soprano\", "
            "\"Alto\", \"Tenor\", \"Bass\", or positional terms: \"top\" (first part), "
            "\"bottom\" (last part), or a 1-based integer index. "
            "Use \"all\" to apply to every part (for key/tempo/time_sig changes).\n"
            "  - \"type\": one of:\n"
            "      \"key\", \"tempo\", \"time_signature\", \"replace_notes\", \"delete_notes\"\n"
            "  - \"value\": depends on type (see below).\n\n"
            "TYPE DETAILS:\n\n"
            "type=\"key\": value is a music21 key string. "
            "sharps='#', flats='-'. e.g. Bb major = \"B- major\", "
            "F# minor = \"f# minor\". part should be \"all\".\n\n"
            "type=\"tempo\": value is an integer BPM. part should be \"all\".\n\n"
            "type=\"time_signature\": value is e.g. \"3/4\". part should be \"all\".\n\n"
            "type=\"replace_notes\": value is an array of note/rest objects that "
            "REPLACE the entire contents of that bar in that part. Each object:\n"
            "  - For a single note: {\"pitch\": \"C4\", \"duration\": 2.0}\n"
            "  - For a chord: {\"pitches\": [\"E2\", \"B2\"], \"duration\": 2.0}\n"
            "  - For a rest: {\"rest\": true, \"duration\": 1.0}\n"
            "Duration is in quarter-note lengths: whole=4.0, half/minim=2.0, "
            "quarter/crotchet=1.0, eighth/quaver=0.5, dotted half=3.0, dotted quarter=1.5.\n"
            "Pitches use scientific notation: C4=middle C, B2=low B, F#5, Bb3 etc.\n"
            "Use '#' for sharps and 'b' for flats in pitch names (NOT '-').\n\n"
            "type=\"delete_notes\": value is null. Replaces the bar with a whole rest.\n\n"
            "PART NAME MAPPING:\n"
            "  'top voice'/'top part'/'voice 1' -> \"top\"\n"
            "  'bottom voice'/'bottom part'/'last voice' -> \"bottom\"\n"
            "  'soprano'/'S' -> \"Soprano\"\n"
            "  'alto'/'A' -> \"Alto\"\n"
            "  'tenor'/'T' -> \"Tenor\"\n"
            "  'bass'/'B'/'baritone' -> \"Bass\"\n\n"
            "EXAMPLES:\n"
            "User: 'Bar 14 in bottom voice should be low E and B as a minim "
            "followed by a crotchet rest'\n"
            "Output: [{\"bar\":14,\"part\":\"bottom\",\"type\":\"replace_notes\","
            "\"value\":[{\"pitches\":[\"E2\",\"B2\"],\"duration\":2.0},"
            "{\"rest\":true,\"duration\":1.0}]}]\n\n"
            "User: 'Bar 8 soprano: D5 crotchet, E5 crotchet, F#5 minim'\n"
            "Output: [{\"bar\":8,\"part\":\"Soprano\",\"type\":\"replace_notes\","
            "\"value\":[{\"pitch\":\"D5\",\"duration\":1.0},"
            "{\"pitch\":\"E5\",\"duration\":1.0},"
            "{\"pitch\":\"F#5\",\"duration\":2.0}]}]\n\n"
            "User: 'Starts in Bb major at 100bpm, 3/4 time'\n"
            "Output: [{\"bar\":1,\"part\":\"all\",\"type\":\"key\",\"value\":\"B- major\"},"
            "{\"bar\":1,\"part\":\"all\",\"type\":\"tempo\",\"value\":100},"
            "{\"bar\":1,\"part\":\"all\",\"type\":\"time_signature\",\"value\":\"3/4\"}]\n\n"
            "If nothing can be parsed, return [].\n"
            "Return ONLY the JSON array, nothing else."
        ),
    )

    if not response.content:
        raise RuntimeError(
            f"The corrections assistant returned no content "
            f"(stop_reason: {response.stop_reason!r}). Try rephrasing."
        )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if not raw:
        raise RuntimeError(
            "The corrections assistant returned an empty response. "
            "Try rephrasing your corrections."
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Could not parse corrections as JSON: {exc}\nRaw: {raw[:300]}"
        ) from exc
    if isinstance(parsed, dict):
        parsed = next((v for v in parsed.values()
                      if isinstance(v, list)), parsed)
    if not parsed:
        raise RuntimeError(
            "No corrections could be parsed from your input. "
            "Try being more specific, e.g. 'Bar 14 bass: E2 and B2 minim, crotchet rest'"
        )
    return parsed


def _resolve_part(score, part_ref) -> list:
    """Resolve a part reference ('all', 'top', 'bottom', name, or index) to a
    list of music21 Part objects."""
    parts = list(score.parts)
    if not parts:
        return []
    if part_ref == "all":
        return parts
    if part_ref == "top":
        return [parts[0]]
    if part_ref == "bottom":
        return [parts[-1]]
    if isinstance(part_ref, int):
        idx = part_ref - 1  # 1-based -> 0-based
        return [parts[idx]] if 0 <= idx < len(parts) else [parts[-1]]
    # Try matching by name (case-insensitive)
    ref_lower = str(part_ref).lower()
    for p in parts:
        pname = ""
        try:
            instr = p.getInstrument()
            if instr and instr.partName:
                pname = instr.partName
        except Exception:
            pass
        if not pname:
            pname = p.id or ""
        if ref_lower in pname.lower():
            return [p]
    # Fallback: return last part (common "bass" default)
    return [parts[-1]]


def apply_corrections(score, instructions: list[dict]):
    """Apply parsed correction instructions to a music21 score.
    Supports key, tempo, time_signature, replace_notes, and delete_notes."""
    import music21

    for instr in instructions:
        bar_num = instr["bar"]
        change_type = instr["type"]
        value = instr.get("value")
        part_ref = instr.get("part", "all")
        target_parts = _resolve_part(score, part_ref)

        for part in target_parts:
            for measure in part.getElementsByClass("Measure"):
                if measure.number != bar_num:
                    continue

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

                elif change_type == "delete_notes":
                    # Remove all notes/rests, insert a whole rest
                    for el in list(measure.notesAndRests):
                        measure.remove(el)
                    measure.insert(0, music21.note.Rest(quarterLength=4.0))

                elif change_type == "replace_notes":
                    # Remove existing notes/rests
                    for el in list(measure.notesAndRests):
                        measure.remove(el)
                    offset = 0.0
                    for item in value:
                        dur = float(item.get("duration", 1.0))
                        if item.get("rest"):
                            n = music21.note.Rest(quarterLength=dur)
                        elif "pitches" in item:
                            # Chord
                            n = music21.chord.Chord(
                                item["pitches"],
                                quarterLength=dur,
                            )
                        elif "pitch" in item:
                            n = music21.note.Note(
                                item["pitch"],
                                quarterLength=dur,
                            )
                        else:
                            offset += dur
                            continue
                        measure.insert(offset, n)
                        offset += dur

                break  # found the bar, move to next instruction

    # Re-spell accidentals if key changes were made
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
        preview = None
        try:
            preview_dir = job_dir / "preview"
            preview_dir.mkdir()
            pdf_page_paths = render_pdf_pages(pdf_path, preview_dir)
            # Strip dynamics before rendering so the SVG shows clean notation
            clean_xml = preview_dir / "score_clean.xml"
            strip_dynamics_from_xml(xml_path, clean_xml)
            svg_page_paths = render_musicxml_svg(clean_xml, preview_dir)
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
    Skips Audiveris — re-processes the existing XML, regenerates SVG preview and audio."""
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
        bpm = int(tempo_bpm) if tempo_bpm.strip().isdigit() else None
        corrections_text = corrections.strip() if corrections else None

        # Apply corrections to a saved copy so both SVG and MIDI use the same XML
        corrected_xml = job_dir / "corrected.xml"
        if corrections_text or bpm:
            _apply_and_save_xml(xml_path, corrected_xml, corrections_text, bpm)
            working_xml = corrected_xml
        else:
            working_xml = xml_path

        # Regenerate SVG preview from the corrected XML
        preview = None
        try:
            preview_dir = job_dir / "preview"
            preview_dir.mkdir(exist_ok=True)
            # Remove old SVGs so stale pages don't linger
            for old_svg in preview_dir.glob("score-*.svg"):
                old_svg.unlink()
            clean_xml = preview_dir / "score_clean.xml"
            strip_dynamics_from_xml(working_xml, clean_xml)
            svg_page_paths = render_musicxml_svg(clean_xml, preview_dir)
            preview = {
                "svg_pages": [
                    f"/files/{job_id}/preview/{p.name}" for p in svg_page_paths
                ],
            }
        except Exception:
            pass  # preview failure never breaks audio generation

        # Clear old MIDI/MP3 output and regenerate
        midi_out = job_dir / "midi"
        if midi_out.exists():
            shutil.rmtree(midi_out)
        midi_out.mkdir()

        # Pass corrections=None since they're already baked into working_xml
        split_result = split_parts(working_xml, midi_out, part_count, bpm=None)
        parts = split_result["parts"]
        all_midi_path = split_result["all_midi_path"]

        for p in parts:
            p["mp3_path"] = midi_to_mp3(p["midi_path"])
        all_mp3_path = midi_to_mp3(all_midi_path)

        zip_path = job_dir / "all_parts.zip"
        make_zip(
            [p["midi_path"] for p in parts]
            + [p["mp3_path"] for p in parts]
            + [all_midi_path, all_mp3_path],
            zip_path,
        )

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
        }

    except RuntimeError as exc:
        raise HTTPException(422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(500, detail=f"Unexpected error: {exc}")


def _apply_and_save_xml(
    xml_path: Path, out_path: Path,
    corrections_text: str | None, bpm: int | None,
) -> None:
    """Parse MusicXML, apply corrections + tempo, write corrected XML to out_path."""
    import music21

    score = music21.converter.parse(str(xml_path))

    if corrections_text:
        instructions = parse_corrections_with_llm(corrections_text)
        apply_corrections(score, instructions)

    if bpm:
        for el in score.recurse():
            if isinstance(el, music21.tempo.MetronomeMark):
                el.activeSite.remove(el)
        for part in score.parts:
            part.insert(0, music21.tempo.MetronomeMark(number=bpm))

    score.write("musicxml", fp=str(out_path))


@app.get("/health")
def health():
    return {"status": "ok"}
