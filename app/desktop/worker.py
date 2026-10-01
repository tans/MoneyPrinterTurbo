"""Run one immutable request in a disposable process, using the shared engine."""

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path


def config_fingerprint(snapshot):
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode()
    ).hexdigest()


def secret_values(snapshot):
    values = []
    for section in snapshot.values():
        for key, value in section.items():
            if any(
                word in key.lower() for word in ("key", "token", "password", "secret")
            ):
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, str) and len(item) >= 4:
                        values.append(item)
    return values


def redact(text, secrets):
    for value in secrets:
        text = text.replace(value, "***")
    return text


def worker_main(job, events):
    from loguru import logger
    from app.config import config

    snapshot = job["config"]
    for name, values in snapshot.items():
        section = getattr(config, name, None)
        if isinstance(section, dict):
            section.clear()
            section.update(values)
    # A desktop worker owns its state; it never requires an external Redis.
    config.app["enable_redis"] = False
    configured_ffmpeg = config.app.get("ffmpeg_path")
    if configured_ffmpeg:
        os.environ["IMAGEIO_FFMPEG_EXE"] = configured_ffmpeg

    secrets = secret_values(snapshot)
    logger.remove()
    logger.add(
        lambda message: events.put(
            {"kind": "log", "message": redact(str(message), secrets)}
        ),
        format="{time:HH:mm:ss} · {level} · {message}",
        colorize=False,
    )
    try:
        result = perform(job, events)
        events.put({"kind": "result", "data": result})
    except Exception as exc:
        logger.exception("desktop task failed")
        events.put(
            {
                "kind": "error",
                "message": redact(f"{type(exc).__name__}: {exc}", secrets),
            }
        )


def voice_catalog(provider):
    from app.config import config
    from app.services import voice

    if provider in {"azure-tts-v1", "azure-tts-v2"}:
        voices = voice.get_all_azure_voices()
        return [
            item
            for item in voices
            if voice.is_azure_v2_voice(item) == (provider == "azure-tts-v2")
        ]
    getters = {
        "siliconflow": voice.get_siliconflow_voices,
        "gemini": voice.get_gemini_voices,
        "mimo": voice.get_mimo_voices,
        "minimax": voice.get_minimax_voices,
        "chatterbox": voice.get_chatterbox_voices,
        "kokoro": voice.get_kokoro_voices,
        "fish_audio": voice.get_fish_audio_voices,
        "voxcpm": voice.get_voxcpm_voices,
        "none": lambda: ["none"],
        "elevenlabs": lambda: voice.get_elevenlabs_voices(
            config.elevenlabs.get("api_key", "")
        ),
    }
    if provider not in getters:
        raise ValueError("unsupported voice provider")
    return getters[provider]()


def serialize_submaker(sub_maker):
    return {
        "type": getattr(sub_maker, "type", None),
        "cues": [
            {
                "offset": round(c.start.total_seconds() * 10000000),
                "duration": round((c.end - c.start).total_seconds() * 10000000),
                "text": c.content,
            }
            for c in getattr(sub_maker, "cues", [])
        ],
        "offset": getattr(sub_maker, "offset", []),
        "subs": getattr(sub_maker, "subs", []),
        "duration": getattr(sub_maker, "duration", 0),
    }


def restore_submaker(data):
    from edge_tts import SubMaker

    maker = SubMaker()
    for cue in data.get("cues", []):
        maker.feed({"type": data.get("type") or "WordBoundary", **cue})
    maker.offset = data.get("offset", [])
    maker.subs = data.get("subs", [])
    maker.duration = data.get("duration", 0)
    return maker


def perform(job, events):
    from app.config import config
    from app.models import const
    from app.models.schema import VideoParams
    from app.services import llm, loomloom, state, task, voice
    from app.utils import utils

    class ReportingState(state.MemoryState):
        def update_task(self, task_id, *args, **kwargs):
            super().update_task(task_id, *args, **kwargs)
            events.put({"kind": "state", "data": self.get_task(task_id)})

        def patch_task(self, task_id, **kwargs):
            patched = super().patch_task(task_id, **kwargs)
            if patched:
                events.put({"kind": "state", "data": self.get_task(task_id)})
            return patched

    state.state = ReportingState()
    task_id = job["id"]
    params = VideoParams.model_validate(job["params"])
    kind = job["kind"]
    options = job.get("options", {})
    if kind == "voices":
        return {"voices": voice_catalog(options.get("provider", "azure-tts-v1"))}
    if kind == "connection":
        ok, message, elapsed = llm.test_connection()
        if not ok:
            raise RuntimeError(message)
        return {"elapsed": elapsed, "message": "模型连接成功"}
    if kind == "script":
        script = llm.generate_script(
            params.video_subject,
            params.video_language,
            params.paragraph_number,
            params.video_script_prompt,
            params.custom_system_prompt,
        )
        if not script or script.startswith("Error:"):
            raise RuntimeError(script or "未生成文案，请检查模型设置")
        return {"script": script}
    if kind == "terms":
        terms = llm.generate_terms(
            params.video_subject,
            params.video_script,
            match_script_order=params.match_materials_to_script,
        )
        if not terms:
            raise RuntimeError("未生成素材关键词")
        return {"terms": terms}
    if kind == "social":
        return llm.generate_social_metadata(
            params.video_subject,
            params.video_script,
            params.video_language,
            options.get("platform", "youtube_shorts"),
        )
    if kind == "loomloom_models":
        backend = loomloom.LoomLoomVideoBackend(
            loomloom.video_settings_from_mapping(config.app)
        )
        return asdict(backend.resolve_video_capability())
    if kind in {"loomloom_video_quote", "loomloom_script_quote"}:
        if kind == "loomloom_video_quote":
            backend = loomloom.LoomLoomVideoBackend(
                loomloom.video_settings_from_mapping(config.app)
            )
            capability = backend.resolve_video_capability()
            model_id = options.get("model_id") or capability.default_model_id
            if model_id not in {model.model_id for model in capability.models}:
                raise ValueError("所选视频模型不在当前账号可用列表中")
            batch = backend.prepare_video_batch(
                subject=params.video_subject,
                scene_prompts=options.get("scene_prompts") or [params.video_subject],
                model_id=model_id,
                aspect_ratio=params.video_aspect,
            )
        else:
            backend = loomloom.LoomLoomScriptBackend(
                loomloom.LoomLoomSettings.from_mapping(config.app)
            )
            batch = backend.prepare_script_batch(
                subject=params.video_subject,
                candidate_count=int(options.get("candidate_count", 3)),
                language=params.video_language,
                duration_seconds=int(options.get("duration_seconds", 60)),
                style=params.video_script_prompt,
            )
        return {
            "quote": asdict(backend.quote(batch)),
            "fingerprint": config_fingerprint(job["config"]),
            "options": options,
        }
    if kind == "loomloom_scripts":
        backend = loomloom.LoomLoomScriptBackend(
            loomloom.LoomLoomSettings.from_mapping(config.app)
        )
        quote = options["quote"]
        batch = loomloom.LoomLoomScriptBatch(input_rows=tuple(quote["input_rows"]))
        execution = backend.execute(
            batch,
            client_request_id=options["client_request_id"],
            listing_version_id=quote["listing_version_id"],
            confirm=True,
        )
        events.put({"kind": "state", "data": {"loomloom_run_id": execution.run_id}})
        backend.wait_for_run(execution.run_id)
        return asdict(backend.get_script_results(execution.run_id))

    tts_options = {}
    for name in ("voxcpm_reference_audio", "voxcpm_prompt_audio"):
        reference = options.get(name)
        if reference:
            file = Path(reference)
            tts_options[name] = voice.prepare_voxcpm_reference_audio(
                file.read_bytes(), file.suffix
            )
    tts_options["voxcpm_prompt_text"] = options.get("voxcpm_prompt_text", "")

    if kind == "preview":
        audio_file = str(Path(utils.task_dir(task_id)) / "audio.mp3")
        maker = voice.tts(
            text=params.video_script,
            voice_name=voice.parse_voice_name(params.voice_name),
            voice_rate=params.voice_rate,
            voice_volume=1.0,
            voice_file=audio_file,
            **tts_options,
        )
        if maker is None or not Path(audio_file).is_file():
            raise RuntimeError("配音试听失败，请检查音色和服务连接")
        return {
            "audio_file": audio_file,
            "duration": voice.get_audio_duration(audio_file),
            "sub_maker": serialize_submaker(maker),
            "fingerprint": config_fingerprint(job["config"]),
        }

    reusable_preview = None
    preview = options.get("preview")
    if preview:
        destination = str(Path(utils.task_dir(task_id)) / "audio.mp3")
        shutil.copyfile(preview["audio_file"], destination)
        reusable_preview = {
            "audio_file": destination,
            "duration": preview["duration"],
            "sub_maker": restore_submaker(preview["sub_maker"]),
            "script": params.video_script.strip(),
            "voice_name": params.voice_name,
            "voice_rate": params.voice_rate,
            "voice_volume": params.voice_volume,
        }
    confirmed = None
    if params.video_source == "loomloom" and kind in {"video", "materials"}:
        quote = options["quote"]
        confirmed = loomloom.LoomLoomConfirmedVideoRequest(
            settings=loomloom.video_settings_from_mapping(config.app),
            batch=loomloom.LoomLoomVideoBatch(tuple(quote["input_rows"])),
            listing_version_id=quote["listing_version_id"],
            client_request_id=options["client_request_id"],
        )
    result = task.start(
        task_id,
        params,
        stop_at=kind,
        voice_preview=reusable_preview,
        loomloom_video_request=confirmed,
        allow_server_file_input=True,
        **tts_options,
    )
    # Keep the worker alive until asynchronous publication finishes. A closing
    # window/cancel still terminates the entire owned process tree.
    task._cross_post_executor.shutdown(wait=True)
    final = state.state.get_task(task_id) or {}
    if final.get("state") == const.TASK_STATE_FAILED:
        raise RuntimeError(final.get("error", "视频生成失败"))
    videos = final.get("videos") or []
    if videos and Path(videos[0]).is_file():
        import subprocess

        poster = Path(utils.task_dir(task_id)) / "preview.jpg"
        try:
            subprocess.run(
                [
                    utils.get_ffmpeg_binary(),
                    "-nostdin",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    videos[0],
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    str(poster),
                ],
                check=True,
                timeout=15,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            poster.unlink(missing_ok=True)
    return {**(result or {}), **final}
