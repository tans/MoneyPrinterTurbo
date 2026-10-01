"""Launch the DaisyUI desktop workspace: uv run --extra desktop python desktop.py."""

import argparse
import json
import multiprocessing
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from app.runtime_paths import application_dir, prepare_desktop_data


class NativeBridge:
    def __init__(self, service):
        self._service = service
        self._window = None
        self._legacy = None

    def pick_files(self, bucket):
        import webview

        types = {
            "materials": (
                "视频和图片 (*.mp4;*.mov;*.mkv;*.webm;*.jpg;*.jpeg;*.png;*.bmp)",
            ),
            "fonts": ("字幕字体 (*.ttf;*.otf;*.ttc)",),
            "audio": ("音频 (*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg)",),
        }
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=True,
            file_types=types.get(bucket, types["audio"]),
        )
        result = []
        for filename in paths or []:
            with open(filename, "rb") as source:
                result.append(
                    self._service.import_file(bucket, Path(filename).name, source)
                )
        return result

    def export_file(self, task_id, name):
        import webview

        source = self._service.artifact(task_id, name)
        paths = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=name
        )
        if not paths:
            return None
        destination = Path(paths[0])
        if destination.resolve() == source.resolve():
            return str(source)
        # Publish only a complete copy. Closing the app never leaves half an export.
        import shutil
        import tempfile

        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, delete=False
            ) as staged:
                temporary = Path(staged.name)
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, staged)
            temporary.replace(destination)
            return str(destination)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def open_data_directory(self):
        path = str(self._service.directory)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        return path

    def open_file(self, task_id, name):
        path = str(self._service.artifact(task_id, name))
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        return path

    def export_preset(self, payload):
        import webview
        from app.desktop.server import parse_preset

        payload = {
            "format": "mpt-desktop-preset",
            "version": 1,
            "params": parse_preset(payload),
        }
        for key in ("video_materials", "custom_audio_file", "bgm_file"):
            payload["params"].pop(key, None)
        paths = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename="mpt-preset.json"
        )
        if not paths:
            return None
        destination = Path(paths[0])
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(destination)

    def import_preset(self):
        import webview

        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("JSON 预设 (*.json)",)
        )
        if not paths:
            return None
        path = Path(paths[0])
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("预设最大 1 MB")
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def open_legacy_workspace(self):
        import webview

        if self._legacy and self._legacy.poll() is None:
            return "兼容工作台已打开"
        with self._service.jobs.condition:
            if self._service.store.active():
                raise ValueError("请等待桌面任务结束，再打开兼容工作台")
            self._service.legacy_active = True
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(application_dir() / "desktop.py"))
        command += ["--legacy-worker", str(port)]
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if sys.platform == "win32"
            else {}
        )
        try:
            self._legacy = subprocess.Popen(command, **options)
        except Exception:
            self._service.legacy_active = False
            raise
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._legacy.poll() is not None:
                self._service.legacy_active = False
                raise RuntimeError("兼容工作台启动失败，请查看运行日志")
            try:
                with urllib.request.urlopen(url + "/_stcore/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            self._shutdown()
            self._service.legacy_active = False
            raise RuntimeError("兼容工作台启动超时")
        legacy_window = webview.create_window(
            "完整兼容工作台 · MoneyPrinterTurbo", url=url, width=1280, height=900
        )

        def closed():
            self._shutdown()
            self._service.resume_from_legacy()
            try:
                self._window.evaluate_js("location.reload()")
            except Exception:
                pass  # The main window may already be closing.

        legacy_window.events.closed += closed
        return "已打开"

    def _shutdown(self):
        if self._legacy:
            from app.desktop.jobs import terminate_process_tree

            terminate_process_tree(self._legacy)


def main(argv=None):
    parser = argparse.ArgumentParser(description="MoneyPrinterTurbo DaisyUI 桌面工作台")
    parser.add_argument("--data-dir", help="用户数据目录，默认使用系统应用数据目录")
    parser.add_argument(
        "--browser", action="store_true", help="在浏览器调试同一界面，无需 GUI 运行环境"
    )
    parser.add_argument("--port", type=int, default=0, help="调试端口，默认自动分配")
    parser.add_argument("--debug", action="store_true", help="开启 WebView 开发者工具")
    parser.add_argument("--legacy-worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.legacy_worker:
        from streamlit.web.cli import main as streamlit_main

        sys.argv = [
            "streamlit",
            "run",
            str(application_dir() / "webui/Main.py"),
            "--server.address=127.0.0.1",
            f"--server.port={args.legacy_worker}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ]
        return streamlit_main()

    directory = prepare_desktop_data(args.data_dir)
    from filelock import FileLock, Timeout
    import uvicorn
    from app.desktop.server import create_app

    lock = FileLock(str(directory / "desktop.lock"))
    session_file = directory / ".desktop-session.json"
    try:
        lock.acquire(timeout=0)
    except Timeout:
        try:
            active = json.loads(session_file.read_text())
            request = urllib.request.Request(
                active["url"] + "/api/focus",
                method="POST",
                headers={"Cookie": "mpt_session=" + active["key"]},
            )
            urllib.request.urlopen(request, timeout=3).close()
        except (OSError, ValueError):
            print("MoneyPrinterTurbo 已在运行。")
        return 0

    app = None
    server = None
    bridge = None
    listener = None
    thread = None
    try:
        token = secrets.token_urlsafe(32)
        app = create_app(directory, token)
        listener = socket.socket()
        listener.bind(("127.0.0.1", args.port))
        listener.listen(128)
        port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="warning", access_log=False
            )
        )
        server.install_signal_handlers = lambda: None
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("桌面服务启动失败")
            time.sleep(0.05)
        session_file.write_text(json.dumps({"url": base_url, "key": token}))
        session_file.chmod(0o600)
        url = base_url + "/session?key=" + token
        if args.browser:
            print(f"Desktop workspace: {url}", flush=True)
            try:
                while thread.is_alive():
                    thread.join(timeout=0.5)
            except KeyboardInterrupt:
                pass
        else:
            import webview

            bridge = NativeBridge(app.state.service)
            window = webview.create_window(
                "MoneyPrinterTurbo",
                url,
                js_api=bridge,
                width=1440,
                height=960,
                min_size=(1000, 700),
                background_color="#f7f8fa",
                text_select=True,
            )
            bridge._window = window
            app.state.window = window

            def closing():
                active = bool(app.state.service.store.active())
                if active:
                    return window.create_confirmation_dialog(
                        "退出 MoneyPrinterTurbo",
                        "仍有任务在运行，退出会停止本地任务。远端生成或发布可能继续，确定退出吗？",
                    )
                return True

            window.events.closing += closing
            webview.start(debug=args.debug, private_mode=True)
    finally:
        if bridge:
            bridge._shutdown()
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=5)
        if app:
            app.state.service.shutdown()
        if listener:
            listener.close()
        session_file.unlink(missing_ok=True)
        lock.release()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
