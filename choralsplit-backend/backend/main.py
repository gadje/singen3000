"""
ChoralSplit backend — FastAPI + Audiveris + music21
"""
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

# Serve generated MIDI/ZIP files
app.mount("/files", StaticFiles(directory=str(JOBS_DIR)), name="files")


# ── Audiveris ────────────────────────────────────────────────────────────────

def run_audiveris(pdf_path: Path, output_dir: Path) -> list[Path]:
    """Run Audiveris in batch mode; return list of MusicXML output files."""
    cmd = [
        AUDIVERIS_CMD,
        "-batch",
        "-export",
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
    return xml_files


# ── music21 splitting ────────────────────────────────────────────────────────

def split_parts(xml_path: Path, output_dir: Path, part_count: str) -> list[dict]:
    """
    Parse MusicXML with music21, split by part, write one MIDI per part.
    Returns a list of dicts: {name, midi_path, note_count}
    """
    import music21  # imported here so startup is fast if music21 is missing

    score = music21.converter.parse(str(xml_path))
    parts = score.parts

    if not parts:
        raise RuntimeError("No parts found in the MusicXML output.")

    # If user specified a count, warn but don't fail — use what we have
    results = []
    for i, part in enumerate(parts):
        part_name = _part_name(part, i)
        midi_filename = f"{_safe_filename(part_name)}.mid"
        midi_path = output_dir / midi_filename

        # Wrap in a fresh Score so it exports cleanly
        single = music21.stream.Score()
        single.append(part)
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

    return results


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

        # 2. Run Audiveris
        audiveris_out = job_dir / "audiveris"
        audiveris_out.mkdir()
        xml_files = run_audiveris(pdf_path, audiveris_out)
        # take first (multi-page scores may produce one file)
        xml_path = xml_files[0]

        # 3. Split parts with music21
        midi_out = job_dir / "midi"
        midi_out.mkdir()
        parts = split_parts(xml_path, midi_out, part_count)

        # 4. Convert MIDI to MP3
        for p in parts:
            p["mp3_path"] = midi_to_mp3(p["midi_path"])

        # 5. Build ZIP containing both MIDI and MP3 files
        zip_path = job_dir / "all_parts.zip"
        all_files = [p["midi_path"]
                     for p in parts] + [p["mp3_path"] for p in parts]
        make_zip(all_files, zip_path)

        # 6. Return JSON with download URLs
        return {
            "parts": [
                {
                    "name": p["name"],
                    "mp3_url": f"/files/{job_id}/midi/{p['mp3_path'].name}",
                    "note_count": p["note_count"],
                }
                for p in parts
            ],
            "zip_url": f"/files/{job_id}/all_parts.zip",
        }

    except RuntimeError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(422, detail=str(exc))
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(500, detail=f"Unexpected error: {exc}")


@app.get("/health")
def health():
    return {"status": "ok"}
