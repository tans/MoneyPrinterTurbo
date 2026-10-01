"""Authenticated loopback API for the desktop workspace; no remote dependencies for UI."""

import copy
import hmac
import json
import mimetypes
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.desktop.jobs import JobManager
from app.desktop.store import ACTIVE, TaskStore
from app.desktop.worker import config_fingerprint
from app.runtime_paths import application_dir


KINDS = {
    "video",
    "materials",
    "audio",
    "subtitle",
    "script",
    "terms",
    "preview",
    "connection",
    "voices",
    "social",
    "loomloom_models",
    "loomloom_video_quote",
    "loomloom_script_quote",
    "loomloom_scripts",
}
PAID_SOURCES = {
    "wavespeed",
    "volcengine_seedance",
    "ofox",
    "metaso_minimax",
    "muapi",
    "openai_image",
    "loomloom",
}
SECTIONS = (
    "app",
    "ui",
    "azure",
    "siliconflow",
    "minimax_tts",
    "elevenlabs",
    "chatterbox",
    "kokoro",
    "fish_audio",
    "voxcpm",
    "whisper",
    "proxy",
)
SOURCES = [
    "pexels",
    "pixabay",
    "coverr",
    "local",
    "wavespeed",
    "volcengine_seedance",
    "ofox",
    "metaso_minimax",
    "muapi",
    "openai_image",
    "loomloom",
]
TTS_PROVIDERS = [
    "azure-tts-v1",
    "azure-tts-v2",
    "siliconflow",
    "gemini",
    "mimo",
    "minimax",
    "elevenlabs",
    "chatterbox",
    "kokoro",
    "fish_audio",
    "voxcpm",
    "none",
]


class JobRequest(BaseModel):
    kind: str = "video"
    params: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)
    confirm_charge: bool = False
    confirm_publish: bool = False
    quote_task_id: str | None = None
    preview_task_id: str | None = None


def inside(directory, filename):
    target = (Path(directory) / filename).resolve()
    if not target.is_relative_to(Path(directory).resolve()) or not target.is_file():
        raise ValueError("文件不存在或不在允许的目录内")
    return target


def parse_preset(payload):
    """Portable styles and text only; local file references never cross devices."""
    from app.models.schema import VideoParams

    if not isinstance(payload, dict) or len(json.dumps(payload)) > 1024 * 1024:
        raise ValueError("预设必须是小于 1 MB 的 JSON 对象")
    if (
        payload.get("format") != "mpt-desktop-preset"
        and payload.get("schema") != "moneyprinterturbo.settings-preset"
    ) or payload.get("version") != 1:
        raise ValueError("不支持的预设格式或版本")
    if not isinstance(payload.get("params"), dict):
        raise ValueError("预设缺少参数对象")
    params = {
        key: value
        for key, value in payload["params"].items()
        if key not in {"video_materials", "custom_audio_file", "bgm_file"}
    }
    if params.get("bgm_type") in {"custom", "preset"}:
        params["bgm_type"] = ""
    return VideoParams.model_validate({"video_subject": "", **params}).model_dump(
        mode="json"
    )


class DesktopService:
    def __init__(self, directory, worker=None):
        from app.config import config
        import toml

        self.directory = Path(directory)
        self.config = config
        self.defaults = toml.load(application_dir() / "config.example.toml")
        self.store = TaskStore(self.directory / "desktop.sqlite3")
        self.jobs = JobManager(self.store, **({"worker": worker} if worker else {}))
        self.legacy_active = False

    def snapshot(self):
        with self.config.runtime_config_lock():
            return {
                name: copy.deepcopy(dict(getattr(self.config, name)))
                for name in SECTIONS
            }

    def bootstrap(self):
        from app import __version__
        from app.models.schema import VideoParams
        from app.models.llm_provider import LLM_PROVIDER_REGISTRY
        from app.services import voice
        from app.utils import utils

        params = VideoParams(
            video_subject="", voice_name="zh-CN-XiaoxiaoNeural-Female"
        ).model_dump(mode="json")
        preferences = self.config.ui.get("desktop_params", {})
        if isinstance(preferences, dict):
            params.update(
                {key: value for key, value in preferences.items() if key in params}
            )
        fonts = self.assets("fonts")
        if fonts and params["font_name"] not in {item["name"] for item in fonts}:
            params["font_name"] = fonts[0]["name"]
        schema = VideoParams.model_json_schema()
        schema["description"] = ""
        return {
            "version": __version__,
            "params": params,
            "schema": schema,
            "providers": [asdict(spec) for spec in LLM_PROVIDER_REGISTRY],
            "sources": SOURCES,
            "tts_providers": TTS_PROVIDERS,
            "voices": voice.get_all_azure_voices(["zh-CN", "en-US"]),
            "fonts": fonts,
            "music": self.assets("music"),
            "settings": self.snapshot(),
            "data_directory": str(self.directory),
            "ffmpeg_ready": utils.check_ffmpeg_ready(),
            "native": False,
        }

    def settings(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(SECTIONS):
            raise ValueError("未知配置分组")
        if len(json.dumps(changes)) > 1024 * 1024:
            raise ValueError("配置过大")
        with self.config.runtime_config_lock():
            if self.legacy_active:
                raise ValueError("请先关闭兼容工作台，再修改桌面设置")
            for name, values in changes.items():
                if not isinstance(values, dict):
                    raise ValueError("配置分组必须是对象")
                current = getattr(self.config, name)
                allowed = set(current) | set(self.defaults.get(name, {}))
                # Registry fields and optional commented provider settings remain editable.
                if name == "app":
                    allowed |= {
                        key for key in values if isinstance(key, str) and len(key) < 128
                    }
                if name == "proxy":
                    allowed |= {"http", "https"}
                if name == "ui":
                    allowed |= {"desktop_params", "tts_server"}
                if set(values) - allowed:
                    raise ValueError(f"未知配置字段：{name}")
                for key, value in values.items():
                    if key != "desktop_params" and not isinstance(
                        value, (str, bool, int, float, list)
                    ):
                        raise ValueError(f"配置值类型错误：{key}")
            for name, values in changes.items():
                getattr(self.config, name).update(values)
            self.config.save_config()
        return self.snapshot()

    def submit(self, body):
        from app.models.schema import VideoParams

        if body.kind not in KINDS:
            raise ValueError("未知任务类型")
        params = VideoParams.model_validate(
            {"video_subject": "", **body.params}
        ).model_dump(mode="json")
        if body.kind not in {"connection", "voices", "loomloom_models"} and not (
            params["video_subject"].strip() or params["video_script"].strip()
        ):
            raise ValueError("请填写主题或文案")
        if (
            body.kind in {"preview", "terms", "subtitle", "audio"}
            and not params["video_script"].strip()
        ):
            raise ValueError("请先生成或填写文案")
        if (
            not params["video_count"]
            or not params["n_threads"]
            or not 1 <= params["video_count"] <= 20
            or not 1 <= params["n_threads"] <= 16
        ):
            raise ValueError("视频数量需为 1–20，编码线程需为 1–16")
        options = copy.deepcopy(body.options)
        # Private execution metadata comes only from validated server-side records.
        for name in ("config", "quote", "preview", "client_request_id"):
            options.pop(name, None)
        snapshot = self.snapshot()
        fingerprint = config_fingerprint(snapshot)
        if body.kind in {"video", "materials"}:
            if (
                params["video_source"] in PAID_SOURCES
                or params["bgm_type"] in {"sonilo", "elevenlabs"}
            ) and not body.confirm_charge:
                raise ValueError("请确认生成素材或配乐可能产生的服务费用")
            if params["video_source"] == "local" and not params["video_materials"]:
                raise ValueError("请选择至少一份本地素材")
        if (
            body.kind == "video"
            and snapshot["app"].get("upload_post_enabled")
            and snapshot["app"].get("upload_post_auto_upload")
            and not body.confirm_publish
        ):
            raise ValueError("请确认生成后自动发布到已配置平台")
        for value in [
            params.get("custom_audio_file"),
            params.get("bgm_file"),
            *(entry.get("url") for entry in params.get("video_materials") or []),
            options.get("voxcpm_reference_audio"),
            options.get("voxcpm_prompt_audio"),
        ]:
            if value:
                self.owned_file(value)
        needs_quote = (
            params["video_source"] == "loomloom" and body.kind in {"video", "materials"}
        ) or body.kind == "loomloom_scripts"
        if needs_quote:
            if not body.confirm_charge or not body.quote_task_id:
                raise ValueError("请先获取报价并确认费用")
            quoted = self.store.get(body.quote_task_id)
            expected = (
                "loomloom_script_quote"
                if body.kind == "loomloom_scripts"
                else "loomloom_video_quote"
            )
            if (
                not quoted
                or quoted["kind"] != expected
                or quoted["status"] != "completed"
            ):
                raise ValueError("报价不可用，请重新询价")
            if (
                quoted["params"] != params
                or quoted["payload"].get("fingerprint") != fingerprint
                or quoted["payload"].get("options", {}) != options
            ):
                raise ValueError("参数或账号设置已变化，请重新询价")
            if quoted["payload"].get("used_by"):
                raise ValueError("该报价已提交，请重新询价")
            options["quote"] = quoted["payload"]["quote"]
            options["client_request_id"] = body.quote_task_id
        if body.preview_task_id:
            preview = self.store.get(body.preview_task_id)
            keys = ("video_script", "voice_name", "voice_rate", "voice_volume")
            if (
                preview
                and preview["kind"] == "preview"
                and preview["status"] == "completed"
                and (
                    all(preview["params"].get(key) == params.get(key) for key in keys)
                    and preview["payload"].get("fingerprint") == fingerprint
                    and not any(
                        options.get(key)
                        for key in ("voxcpm_reference_audio", "voxcpm_prompt_audio")
                    )
                )
            ):
                options["preview"] = preview["payload"]
        with self.jobs.condition:
            if self.legacy_active:
                raise ValueError("请先关闭兼容工作台，再提交桌面任务")
            if needs_quote and self.store.get(body.quote_task_id)["payload"].get(
                "used_by"
            ):
                raise ValueError("该报价已提交")
            result = self.jobs.submit(body.kind, params, snapshot, options)
            if needs_quote:
                self.store.update(body.quote_task_id, used_by=result["id"])
        return result

    def resume_from_legacy(self):
        with self.config.runtime_config_lock():
            loaded = self.config.load_config()
            for name in SECTIONS:
                section = getattr(self.config, name)
                section.clear()
                section.update(loaded.get(name, self.defaults.get(name, {})))
            self.legacy_active = False

    def owned_file(self, filename):
        path = Path(filename).resolve()
        if not path.is_relative_to(self.directory.resolve()) or not path.is_file():
            raise ValueError("请通过素材库导入文件")
        return path

    def folder(self, bucket):
        folders = {
            "materials": "storage/local_videos",
            "audio": "storage/desktop_audio",
            "reference": "storage/desktop_reference",
            "fonts": "resource/fonts",
            "music": "resource/songs",
        }
        if bucket not in folders:
            raise ValueError("未知素材类型")
        directory = self.directory / folders[bucket]
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def assets(self, bucket):
        return [
            {
                "name": p.name,
                "path": str(p),
                "size": p.stat().st_size,
                "url": f"/api/assets/{bucket}/{quote(p.name, safe='')}",
            }
            for p in sorted(self.folder(bucket).iterdir())
            if p.is_file() and not p.name.startswith(".")
        ]

    def import_file(self, bucket, name, source):
        from app.services import material_upload

        if bucket == "materials":
            stored = material_upload.save_material_upload(name, source)
            # Preserve a readable label without changing the material validator.
            current = self.folder(bucket) / stored
            target = current.with_name(
                f"{current.stem[:8]}-{material_upload.sanitize_material_filename(name)}"
            )
            current.rename(target)
            return {"name": target.name, "path": str(target)}
        name = Path(name.replace("\\", "/")).name
        if (
            not name
            or len(name) > 200
            or any(char in name for char in '<>:"/\\|?*\x00')
        ):
            raise ValueError("无效文件名")
        suffix = Path(name).suffix.lower()
        allowed = {
            "fonts": {".ttf", ".otf", ".ttc"},
            "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
        }
        if suffix not in allowed.get(bucket, allowed["audio"]):
            raise ValueError("不支持的文件类型")
        destination = self.folder(bucket) / (uuid4().hex[:8] + "-" + Path(name).name)
        count = 0
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, delete=False
            ) as staged:
                temporary = Path(staged.name)
                while chunk := source.read(1024 * 1024):
                    count += len(chunk)
                    if count > 100 * 1024 * 1024:
                        raise ValueError("单个文件最大 100 MB")
                    staged.write(chunk)
            if not count:
                raise ValueError("文件为空")
            if bucket == "fonts":
                from PIL import ImageFont

                try:
                    ImageFont.truetype(str(temporary), 24)
                except OSError as exc:
                    raise ValueError("字体文件无效") from exc
            temporary.replace(destination)
        finally:
            if "temporary" in locals():
                temporary.unlink(missing_ok=True)
        return {"name": destination.name, "path": str(destination)}

    def artifact(self, task_id, name):
        if not self.store.get(task_id):
            raise ValueError("任务不存在")
        return inside(self.directory / "storage/tasks" / task_id, name)

    def task_detail(self, task_id):
        record = self.store.get(task_id)
        if not record:
            raise ValueError("任务不存在")
        directory = self.directory / "storage/tasks" / task_id
        record["files"] = [
            {
                "name": p.name,
                "size": p.stat().st_size,
                "url": f"/api/tasks/{task_id}/files/{quote(p.name, safe='')}",
                "mime": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            }
            for p in sorted(directory.glob("*"))
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix in {".mp4", ".mp3", ".wav", ".srt", ".json", ".jpg"}
        ]
        record["logs"] = self.store.logs(task_id)
        return record

    def delete_task(self, task_id):
        record = self.store.get(task_id)
        if not record:
            raise ValueError("任务不存在")
        if record["status"] in ACTIVE:
            raise ValueError("请先取消正在运行的任务")
        directory = self.directory / "storage/tasks" / task_id
        if directory.exists():
            shutil.rmtree(directory)
        self.store.delete(task_id)

    def shutdown(self):
        self.jobs.shutdown()
        self.store.close()


def create_app(directory, token, worker=None):
    service = DesktopService(directory, worker=worker)
    app = FastAPI(
        title="MoneyPrinterTurbo Desktop",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service

    @app.middleware("http")
    async def session_guard(request, call_next):
        if request.url.path not in {"/session", "/health"}:
            if not hmac.compare_digest(request.cookies.get("mpt_session", ""), token):
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {"detail": "桌面会话已失效，请重新打开程序"}, status_code=401
                )
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                from fastapi.responses import JSONResponse

                return JSONResponse({"detail": "拒绝跨来源请求"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # pywebview's evaluate_js / native bridge evaluates injected callbacks.
        # Scripts still load exclusively from our authenticated local origin.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/session")
    def session(key: str = ""):
        if not hmac.compare_digest(key, token):
            raise HTTPException(401, "无效桌面会话")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("mpt_session", token, httponly=True, samesite="strict")
        return response

    @app.get("/api/bootstrap")
    def bootstrap():
        return service.bootstrap()

    @app.post("/api/focus")
    def focus():
        window = getattr(app.state, "window", None)
        if window:
            window.restore()
            window.show()
        return {"ok": True}

    @app.put("/api/settings")
    async def settings(request: Request):
        return service.settings(await request.json())

    @app.post("/api/preset")
    async def preset(request: Request):
        return parse_preset(await request.json())

    @app.post("/api/tasks")
    def submit(body: JobRequest):
        return service.submit(body)

    @app.get("/api/tasks")
    def tasks():
        return service.store.list()

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str):
        return service.task_detail(task_id)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        service.jobs.cancel(task_id)
        return service.store.get(task_id)

    @app.delete("/api/tasks/{task_id}")
    def delete(task_id: str):
        service.delete_task(task_id)
        return {"ok": True}

    @app.get("/api/tasks/{task_id}/files/{name}")
    def task_file(task_id: str, name: str, download: bool = False):
        path = service.artifact(task_id, name)
        return FileResponse(path, filename=name if download else None)

    @app.get("/api/assets/{bucket}")
    def assets(bucket: str):
        return service.assets(bucket)

    @app.get("/api/assets/{bucket}/{name}")
    def asset(bucket: str, name: str):
        return FileResponse(inside(service.folder(bucket), name))

    @app.post("/api/assets/{bucket}")
    def upload(bucket: str, file: UploadFile = File(...)):
        return service.import_file(bucket, file.filename or "file", file.file)

    @app.delete("/api/assets/{bucket}/{name}")
    def remove_asset(bucket: str, name: str):
        if service.store.active():
            raise ValueError("任务运行期间暂时不能删除素材")
        inside(service.folder(bucket), name).unlink()
        return {"ok": True}

    @app.get("/api/cache")
    def cache():
        from app.services.cache_manager import get_video_cache_stats

        return asdict(get_video_cache_stats())

    @app.post("/api/cache/clean")
    def clean_cache():
        from app.services.cache_manager import clean_video_cache

        if service.store.active():
            raise ValueError("请等待生成任务结束后清理缓存")
        return asdict(clean_video_cache(max_age_days=30))

    web = application_dir() / "desktop/ui"

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    app.mount("/static", StaticFiles(directory=web), name="static")
    return app
