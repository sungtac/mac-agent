#!/usr/bin/env bash
# Transcribes an audio file to text using local whisper.cpp (Metal-accelerated,
# fully offline — no audio ever leaves this Mac). Multilingual, auto-detects
# language (works for Korean).
#
# Usage: transcribe.sh <audio-file>
#   Prints the transcript to stdout. All whisper.cpp diagnostics go to stderr.
set -euo pipefail

WHISPER_CLI="/opt/homebrew/bin/whisper-cli"
MODEL="$HOME/mac-agent/whisper-models/ggml-large-v3-turbo-q5_0.bin"
FFMPEG="/opt/homebrew/bin/ffmpeg"

INPUT="${1:?usage: transcribe.sh <audio-file>}"
[ -f "$INPUT" ] || { echo "no such file: $INPUT" >&2; exit 1; }

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

EXT_LOWER="$(echo "${INPUT##*.}" | tr '[:upper:]' '[:lower:]')"

# whisper.cpp natively decodes wav/flac/mp3/ogg; anything else (m4a/aac from
# phone voice memos, etc.) gets converted first.
case "$EXT_LOWER" in
  wav|flac|mp3|ogg)
    AUDIO_FOR_WHISPER="$INPUT"
    ;;
  *)
    AUDIO_FOR_WHISPER="$WORKDIR/converted.wav"
    "$FFMPEG" -y -loglevel error -i "$INPUT" -ar 16000 -ac 1 "$AUDIO_FOR_WHISPER"
    ;;
esac

"$WHISPER_CLI" -m "$MODEL" -f "$AUDIO_FOR_WHISPER" -l auto -nt 2>/dev/null
