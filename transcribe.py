#!/usr/bin/env python3
"""
Lecture Transcription & Summarization Pipeline
Uses local Whisper for transcription + Anthropic API for structured summaries.
"""

import os
import sys
import subprocess
import tempfile
from pathlib import Path
import whisper
import anthropic

SUMMARY_DIR = Path(__file__).parent / "lecture-summaries"
SUMMARY_DIR.mkdir(exist_ok=True)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}


def extract_audio(video_path: Path, out_path: Path):
    """Strip audio from video using ffmpeg."""
    print(f"  Extracting audio from {video_path.name}...")
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(out_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr}")


def transcribe(audio_path: Path, model_name: str = "base") -> str:
    """Transcribe audio using local Whisper."""
    print(f"  Loading Whisper model '{model_name}'...")
    model = whisper.load_model(model_name)
    print("  Transcribing... (this may take a while)")
    result = model.transcribe(str(audio_path), fp16=False)
    return result["text"].strip()


def summarize(transcript: str, course: str, lecture_title: str) -> str:
    """Send transcript to Anthropic API and return a structured summary."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set.\n"
            "Export it before running: export ANTHROPIC_API_KEY=sk-ant-..."
        )

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are an expert academic note-taker helping a university student who missed a lecture.

Course: {course}
Lecture Title: {lecture_title}

Below is the full transcript of the lecture. Please produce a detailed, well-structured summary with exactly these sections:

---

## Course & Lecture Title
{course} — {lecture_title}

## Full Overview
[A detailed paragraph summary (at least 200 words) covering all major ideas and arguments in plain English. No unnecessary jargon. Long enough to stand alone as a study resource.]

## Key Concepts
[Bullet point for every important concept, each with a 2–3 sentence plain-English explanation.]

## Important Terms, Names & Dates
[Glossary-style section. For each term/name/date: bold it, then give a clear definition/context.]

## Core Arguments or Themes
[The 3–5 biggest ideas the lecture is building toward, explained simply. Number them.]

## NotebookLM Script
[An 800–1000 word dense-but-conversational overview of everything important from the lecture. Written as if explaining to a smart friend who missed class. No jargon unless explained inline. Suitable for a 5–10 minute audio overview.]

---

TRANSCRIPT:
{transcript}
"""

    print("  Sending transcript to Anthropic API...")
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def safe_filename(text: str) -> str:
    """Turn a string into a safe filename fragment."""
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in text).strip().replace(" ", "_")


def process_file(input_path: Path, course: str, lecture_title: str, whisper_model: str = "base"):
    """Full pipeline for a single file."""
    suffix = input_path.suffix.lower()
    tmp_audio = None

    try:
        if suffix in VIDEO_EXTENSIONS:
            tmp_audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_audio.close()
            audio_path = Path(tmp_audio.name)
            extract_audio(input_path, audio_path)
        elif suffix in AUDIO_EXTENSIONS:
            audio_path = input_path
        else:
            print(f"  Skipping unsupported file type: {suffix}")
            return

        transcript = transcribe(audio_path, model_name=whisper_model)

        # Save raw transcript alongside summary
        fname_base = safe_filename(f"{course}_{lecture_title}")
        transcript_path = SUMMARY_DIR / f"{fname_base}_transcript.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        print(f"  Transcript saved → {transcript_path}")

        summary = summarize(transcript, course, lecture_title)

        summary_path = SUMMARY_DIR / f"{fname_base}_summary.txt"
        full_output = (
            f"Course: {course}\nLecture: {lecture_title}\n"
            f"Source file: {input_path.name}\n"
            f"{'='*60}\n\n"
            + summary
        )
        summary_path.write_text(full_output, encoding="utf-8")
        print(f"  Summary saved  → {summary_path}")

    finally:
        if tmp_audio and Path(tmp_audio.name).exists():
            Path(tmp_audio.name).unlink()


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def main():
    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  Single file : python3 transcribe.py <file>")
        print("  Folder batch: python3 transcribe.py --batch <folder>")
        print("")
        print("Options:")
        print("  --model <name>   Whisper model (tiny/base/small/medium/large). Default: base")
        sys.exit(1)

    # Parse --model flag
    whisper_model = "base"
    if "--model" in args:
        idx = args.index("--model")
        whisper_model = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    batch_mode = "--batch" in args
    if batch_mode:
        args.remove("--batch")

    if not args:
        print("Error: no file or folder specified.")
        sys.exit(1)

    target = Path(args[0])

    if batch_mode:
        if not target.is_dir():
            print(f"Error: '{target}' is not a directory.")
            sys.exit(1)

        files = sorted(
            f for f in target.iterdir()
            if f.suffix.lower() in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
        )

        if not files:
            print(f"No supported media files found in '{target}'.")
            sys.exit(1)

        print(f"Found {len(files)} file(s) in '{target}':\n")
        for f in files:
            print(f"  {f.name}")

        print()
        course = ask("Enter the course name for ALL files in this folder: ")

        for i, f in enumerate(files, 1):
            print(f"\n[{i}/{len(files)}] Processing: {f.name}")
            lecture_title = ask(f"  Lecture title for '{f.name}': ")
            process_file(f, course, lecture_title, whisper_model)

        print("\nAll done! Summaries saved in ./lecture-summaries/")

    else:
        target = Path(args[0])
        if not target.exists():
            print(f"Error: file '{target}' not found.")
            sys.exit(1)

        print(f"\nFile: {target.name}")
        course = ask("Enter the course name: ")
        lecture_title = ask("Enter the lecture title: ")

        print(f"\nProcessing '{target.name}'...")
        process_file(target, course, lecture_title, whisper_model)
        print("\nDone! Check ./lecture-summaries/ for your files.")


if __name__ == "__main__":
    main()
