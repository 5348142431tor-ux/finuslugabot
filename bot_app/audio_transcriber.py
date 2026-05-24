from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


class AudioTranscriber:
    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.model = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip()
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "25"))
        self.ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model and self.ffmpeg and Path(self.ffmpeg).exists())

    def transcribe(self, source_path: str) -> str | None:
        if not self.enabled:
            return None
        wav_path = self._convert_to_wav(source_path)
        if not wav_path:
            return None
        try:
            return self._transcribe_wav(wav_path)
        finally:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _convert_to_wav(self, source_path: str) -> str | None:
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                wav_path = tmp.name
            subprocess.run(
                [
                    self.ffmpeg,
                    "-y",
                    "-i",
                    source_path,
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    wav_path,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return wav_path
        except Exception:
            return None

    def _transcribe_wav(self, wav_path: str) -> str | None:
        boundary = "----finuslugabotboundary"
        audio_bytes = Path(wav_path).read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="model"\r\n\r\n',
                self.model.encode(),
                b"\r\n",
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="response_format"\r\n\r\n',
                b"text\r\n",
                f"--{boundary}\r\n".encode(),
                b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
                b"Content-Type: audio/wav\r\n\r\n",
                audio_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        request = urllib.request.Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = response.read().decode("utf-8").strip()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return None
        if not data:
            return None
        try:
            parsed = json.loads(data)
            if isinstance(parsed, dict):
                return (parsed.get("text") or "").strip() or None
        except Exception:
            pass
        return data.strip() or None
