"""Exercise a built application, including spawned workers and real FFmpeg output."""

import argparse
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import wave

from PIL import Image
import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "executable", help="Path to the built MoneyPrinterTurbo executable"
    )
    args = parser.parse_args()
    executable = str(Path(args.executable).resolve())
    with tempfile.TemporaryDirectory(prefix="mpt-desktop-smoke-") as temporary:
        directory = Path(temporary)
        with (directory / "process.log").open("w+") as log:
            process = subprocess.Popen(
                [executable, "--browser", "--data-dir", temporary],
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 60
                session_file = directory / ".desktop-session.json"
                while not session_file.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Built desktop exited before startup")
                    time.sleep(0.1)
                session = json.loads(session_file.read_text())
                http = requests.Session()
                http.cookies.set("mpt_session", session["key"])
                base = session["url"]
                bootstrap = http.get(base + "/api/bootstrap", timeout=15)
                if not bootstrap.ok:
                    print("Bootstrap error:", bootstrap.text)
                bootstrap.raise_for_status()
                assert bootstrap.json()["ffmpeg_ready"]
                Image.new("RGB", (1280, 720), "#137d6f").save(directory / "scene.png")
                with wave.open(str(directory / "audio.wav"), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(b"\0\0" * 48000)
                assets = {}
                for bucket, filename in (
                    ("materials", "scene.png"),
                    ("audio", "audio.wav"),
                ):
                    with (directory / filename).open("rb") as source:
                        response = http.post(
                            base + "/api/assets/" + bucket,
                            files={"file": (filename, source)},
                            timeout=30,
                        )
                        response.raise_for_status()
                        assets[bucket] = response.json()["path"]
                response = http.post(
                    base + "/api/tasks",
                    json={
                        "kind": "video",
                        "params": {
                            "video_subject": "Desktop package smoke test",
                            "video_script": "A real local video.",
                            "video_source": "local",
                            "video_aspect": "16:9",
                            "video_terms": "",
                            "video_materials": [
                                {"provider": "local", "url": assets["materials"]}
                            ],
                            "custom_audio_file": assets["audio"],
                            "subtitle_enabled": False,
                            "bgm_type": "",
                        },
                    },
                    timeout=10,
                )
                response.raise_for_status()
                task_id = response.json()["id"]
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    detail = http.get(base + "/api/tasks/" + task_id, timeout=10).json()
                    if detail["status"] not in {"queued", "running", "publishing"}:
                        break
                    time.sleep(0.3)
                assert detail["status"] == "completed", detail
                video = next(
                    item
                    for item in detail["files"]
                    if item["name"].startswith("final-")
                    and item["name"].endswith(".mp4")
                )
                exported = http.get(base + video["url"] + "?download=true", timeout=15)
                exported.raise_for_status()
                assert len(exported.content) > 1000
                print(
                    f"PASS: packaged bootstrap, native resources, imports, spawn worker, real MP4 and export ({len(exported.content)} bytes)"
                )
            except Exception:
                log.flush()
                print((directory / "process.log").read_text(errors="replace"))
                raise
            finally:
                if process.poll() is None:
                    if (
                        hasattr(signal, "SIGINT")
                        and __import__("sys").platform != "win32"
                    ):
                        process.send_signal(signal.SIGINT)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
