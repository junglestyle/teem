import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


MAX_AUDIO = 8 * 1024 * 1024
MAX_SECONDS = 120
RUNNER_SECONDS = 180


class SpeechError(Exception):
    pass


class SpeechRunner:
    def __init__(self, config_path, scratch_root):
        config = json.loads(Path(config_path).read_text())
        if set(config) != {"executable", "executable_sha256", "model", "model_sha256", "language"} or \
           not isinstance(config["language"], str) or not config["language"] or \
           not isinstance(config["model_sha256"], str) or len(config["model_sha256"]) != 64 or \
           not isinstance(config["executable_sha256"], str) or len(config["executable_sha256"]) != 64:
            raise ValueError("invalid speech configuration")
        self.executable = Path(config["executable"]).resolve(strict=True)
        self.model = Path(config["model"]).resolve(strict=True)
        self.language = config["language"]
        executable_hash = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        if executable_hash != config["executable_sha256"]:
            raise ValueError("speech executable identity mismatch")
        model_hash = hashlib.sha256()
        with self.model.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                model_hash.update(chunk)
        if model_hash.hexdigest() != config["model_sha256"]:
            raise ValueError("speech model identity mismatch")
        self.scratch_root = Path(scratch_root).resolve()
        self.scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.scratch_root, 0o700)
        for path in self.scratch_root.glob("teem-speech-*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        self.lock = threading.Lock()
        self.process = None
        self.stopping = False

    def stop(self):
        self.stopping = True
        process = self.process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _limits(self):
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        # CPU time sums across whisper threads; the wall-clock deadline is the real bound.
        cpu = int(RUNNER_SECONDS * (os.cpu_count() or 1)) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    def _run(self, command, scratch, deadline, capture=False):
        # No sandbox here: the speech container is the sandbox (no network, no secrets, read-only).
        timeout = deadline - time.monotonic()
        if timeout <= 0 or self.stopping:
            raise SpeechError("transcription timed out")
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, cwd=scratch,
                                       stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=self._limits,
                                       env={"PATH": "/usr/bin:/bin", "HOME": str(scratch)})
        except OSError as exc:
            raise SpeechError("local recognition unavailable") from exc
        self.process = process
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise SpeechError("transcription timed out") from exc
        finally:
            self.process = None
        if process.returncode or self.stopping:
            raise SpeechError(f"local recognition failed at {command[0]}")
        return output

    def transcribe(self, audio, media_type):
        if not self.lock.acquire(blocking=False):
            raise SpeechError("busy")
        try:
            return self._transcribe(audio, media_type)
        finally:
            self.lock.release()

    def _transcribe(self, audio, media_type):
        if media_type not in ("audio/webm", "audio/mp4", "audio/ogg") or not 0 < len(audio) <= MAX_AUDIO:
            raise SpeechError("unsupported or empty recording")
        deadline = time.monotonic() + RUNNER_SECONDS
        with tempfile.TemporaryDirectory(prefix="teem-speech-", dir=self.scratch_root) as directory:
            scratch = Path(directory)
            input_path = scratch / "recording"
            input_path.write_bytes(audio)
            # ffprobe checks the container and codec, not the browser's MIME claim.
            try:
                raw = self._run(["/usr/bin/ffprobe", "-v", "error", "-show_entries",
                                 "format=format_name,duration:stream=codec_type,codec_name", "-of", "json",
                                 str(input_path)], scratch, deadline, capture=True)
            except SpeechError as exc:
                if "timed out" in str(exc):
                    raise
                raise SpeechError("invalid recording") from exc
            try:
                probe = json.loads(raw)
                formats = probe["format"]["format_name"].split(",")
                duration = probe["format"].get("duration")
                streams = probe["streams"]
                valid = len(streams) == 1 and streams[0]["codec_type"] == "audio" and (
                    media_type == "audio/webm" and "matroska" in formats and streams[0]["codec_name"] == "opus" or
                    media_type == "audio/mp4" and "mov" in formats and streams[0]["codec_name"] == "aac" or
                    media_type == "audio/ogg" and "ogg" in formats and streams[0]["codec_name"] == "opus")
                if not valid or duration is not None and not 0 < float(duration) <= MAX_SECONDS + 1:
                    raise ValueError
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                raise SpeechError("invalid or overlong recording") from exc
            try:
                self._run(["/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", str(input_path),
                           "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                           str(scratch / "input.wav")], scratch, deadline)
            except SpeechError as exc:
                if "timed out" in str(exc):
                    raise
                raise SpeechError("invalid recording") from exc
            try:
                with wave.open(str(scratch / "input.wav")) as wav:
                    if wav.getnchannels() != 1 or wav.getframerate() != 16000 or wav.getsampwidth() != 2 or \
                       not 0 < wav.getnframes() <= MAX_SECONDS * 16000:
                        raise ValueError
            except (OSError, wave.Error, ValueError) as exc:
                raise SpeechError("invalid or overlong recording") from exc
            self._run([str(self.executable), "-m", str(self.model), "-f", str(scratch / "input.wav"),
                       "-l", self.language, "-otxt", "-of", str(scratch / "transcript")], scratch, deadline)
            output = scratch / "transcript.txt"
            if not output.is_file() or output.stat().st_size > 32 * 1024:
                raise SpeechError("invalid transcription output")
            try:
                text = output.read_text(encoding="utf-8").strip()
            except UnicodeError as exc:
                raise SpeechError("invalid transcription output") from exc
            if not text or len(text) > 8000:
                raise SpeechError("invalid transcription output")
            return text


class Handler(BaseHTTPRequestHandler):
    """The speech service: POST /transcribe with the recording as the body and its media type."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if self.path != "/transcribe" or not 0 < length <= MAX_AUDIO:
            return self.reply(400, {"error": "unsupported or empty recording"})
        audio = self.rfile.read(length)
        try:
            self.reply(200, {"text": self.server.runner.transcribe(audio, self.headers.get("Content-Type", ""))})
        except SpeechError as exc:
            self.reply(422, {"error": str(exc)})

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


def serve(config_path, scratch_root, host, port):
    # One request at a time: a transcription uses every core anyway.
    server = HTTPServer((host, port), Handler)
    server.runner = SpeechRunner(config_path, scratch_root)
    server.serve_forever()


class SpeechClient:
    """The server's side of the speech service."""

    def __init__(self, url):
        self.url = url.rstrip("/")

    def transcribe(self, audio, media_type):
        request = urllib.request.Request(self.url + "/transcribe", data=audio, method="POST",
                                         headers={"Content-Type": media_type})
        try:
            with urllib.request.urlopen(request, timeout=RUNNER_SECONDS + 30) as response:
                return json.loads(response.read(64 * 1024))["text"]
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read(4096))["error"]
            except (ValueError, KeyError, TypeError):
                message = f"speech service returned {exc.code}"
            raise SpeechError(message) from None
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
            raise SpeechError("speech service unavailable") from None
