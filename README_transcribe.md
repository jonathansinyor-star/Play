# Lecture Transcription & Summarization Pipeline

Transcribes missed university lectures locally with Whisper, then generates rich structured summaries via the Anthropic API.

## Setup

```bash
# 1. Install system dependency (already done if you ran the setup)
apt-get install -y ffmpeg

# 2. Install Python packages
pip3 install openai-whisper anthropic

# 3. Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

### Single file
```bash
python3 transcribe.py lecture01.mp4
# You'll be prompted for: Course name, Lecture title
```

### Whole folder (batch mode)
```bash
python3 transcribe.py --batch ./lecture-videos/
# You'll be prompted for: Course name once, then a lecture title per file
```

### Choose a Whisper model (speed vs accuracy)
| Model  | Size   | Speed    | Accuracy |
|--------|--------|----------|----------|
| tiny   | 75 MB  | fastest  | lower    |
| base   | 145 MB | fast     | good     |
| small  | 466 MB | moderate | better   |
| medium | 1.5 GB | slow     | great    |
| large  | 2.9 GB | slowest  | best     |

```bash
python3 transcribe.py --model small lecture01.mp4
python3 transcribe.py --model medium --batch ./lecture-videos/
```

## Output

All files saved in `./lecture-summaries/`:
- `{Course}_{Lecture}_transcript.txt` — raw transcript from Whisper
- `{Course}_{Lecture}_summary.txt` — structured summary with sections:
  - Full Overview
  - Key Concepts
  - Important Terms, Names & Dates
  - Core Arguments or Themes
  - NotebookLM Script (800–1000 word audio-ready overview)

## Supported formats
**Video:** `.mp4`, `.mkv`, `.mov`, `.avi`, `.webm`, `.m4v`, `.flv`  
**Audio:** `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`, `.aac`, `.opus`
