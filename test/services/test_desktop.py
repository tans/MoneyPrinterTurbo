"""Desktop boundaries: persistence, process ownership, consent and local files."""

import copy
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import config
from app.desktop.jobs import JobManager
from app.desktop.server import JobRequest, SECTIONS, create_app, parse_preset
from app.desktop.store import TaskStore
from app.desktop.worker import (
    config_fingerprint,
    redact,
    restore_submaker,
    secret_values,
    serialize_submaker,
)
from app import runtime_paths


class HeldJobs:
    """Hold execution to inspect the authenticated submission boundary."""

    def __init__(self, store, **kwargs):
        self.store = store
        self.condition = threading.Condition(threading.RLock())
        self.requests = []

    def submit(self, kind, params, snapshot, options):
        task = self.store.create(kind, params)
        self.requests.append((snapshot, options))
        return task

    def cancel(self, task_id):
        self.store.update(task_id, "cancelled")

    def shutdown(self):
        pass


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from app.desktop import server

    previous = {name: copy.deepcopy(dict(getattr(config, name))) for name in SECTIONS}
    monkeypatch.setenv("MPT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "config_file", str(tmp_path / "config.toml"))
    monkeypatch.setattr(config, "root_dir", str(tmp_path))
    monkeypatch.setattr(server, "JobManager", HeldJobs)
    app = create_app(tmp_path, "test-session-token")
    client = TestClient(app)
    client.get("/session?key=test-session-token")
    yield app.state.service, client
    client.close()
    app.state.service.shutdown()
    for name, values in previous.items():
        getattr(config, name).clear()
        getattr(config, name).update(values)


def params(**changes):
    return {
        "video_subject": "desktop test",
        "video_script": "A complete script.",
        **changes,
    }


def success_worker(job, events):
    events.put({"kind": "log", "message": "worker started"})
    events.put({"kind": "state", "data": {"task_id": job["id"], "progress": 35}})
    events.put({"kind": "result", "data": {"config_value": job["config"].get("value")}})


def slow_worker(job, events):
    events.put({"kind": "state", "data": {"progress": 10}})
    time.sleep(30)


def failure_worker(job, events):
    events.put({"kind": "error", "message": "service rejected request"})


def crash_worker(job, events):
    os._exit(7)


def await_status(store, task_id, statuses, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get(task_id)
        if record["status"] in statuses:
            return record
        time.sleep(0.03)
    raise AssertionError(store.get(task_id))


def test_data_directory_is_writable_and_upgrade_preserves_user_files(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MPT_DATA_DIR", "")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.example.toml").write_text('[app]\nllm_provider="openai"\n')
    (bundle / "resource/fonts").mkdir(parents=True)
    (bundle / "resource/fonts/font.ttf").write_bytes(b"font-version-1")
    monkeypatch.setattr(runtime_paths, "application_dir", lambda: bundle)
    target = runtime_paths.prepare_desktop_data(str(tmp_path / "profile"))
    (target / "config.toml").write_text("user configuration")
    (target / "resource/fonts/font.ttf").write_bytes(b"user font")
    (bundle / "resource/fonts/font.ttf").write_bytes(b"font-version-2")
    assert runtime_paths.prepare_desktop_data(str(target)) == target
    assert runtime_paths.data_dir() == target
    assert (target / "config.toml").read_text() == "user configuration"
    assert (target / "resource/fonts/font.ttf").read_bytes() == b"user font"
    from app.utils import utils

    assert utils.storage_dir() == str(target / "storage")
    assert utils.model_dir() == str(target / "models")


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("win32", "local/MoneyPrinterTurbo"),
        ("darwin", "Library/Application Support/MoneyPrinterTurbo"),
        ("linux", "xdg/MoneyPrinterTurbo"),
    ],
)
def test_default_profile_paths(platform, expected, tmp_path, monkeypatch):
    monkeypatch.delenv("MPT_DATA_DIR", raising=False)
    monkeypatch.setattr(runtime_paths.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert runtime_paths.desktop_data_dir() == tmp_path / expected


def test_store_restart_recovers_all_active_tasks_without_reexecuting(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = TaskStore(path)
    old = store.create("video", params())
    store.update(old["id"], "publishing", progress=100, remote_run_id="remote-1")
    for _ in range(105):
        done = store.create("script", params())
        store.update(done["id"], "completed")
    assert len(store.list()) == 100
    assert store.active()[0]["id"] == old["id"]
    store.log(old["id"], "saved log")
    store.close()
    reopened = TaskStore(path)
    reopened.recover()
    assert reopened.get(old["id"])["status"] == "interrupted"
    assert reopened.get(old["id"])["payload"]["remote_run_id"] == "remote-1"
    assert reopened.logs(old["id"]) == ["saved log"]
    reopened.delete(old["id"])
    assert reopened.get(old["id"]) is None
    reopened.close()


def test_serial_workers_use_submission_snapshot(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    manager = JobManager(store, worker=success_worker)
    snapshot = {"value": "first"}
    try:
        one = manager.submit("script", params(), snapshot)
        snapshot["value"] = "second"
        two = manager.submit("script", params(), snapshot)
        assert (
            await_status(store, one["id"], {"completed"})["payload"]["config_value"]
            == "first"
        )
        assert (
            await_status(store, two["id"], {"completed"})["payload"]["config_value"]
            == "second"
        )
        assert store.logs(one["id"]) == ["worker started"]
    finally:
        manager.shutdown()
        store.close()


@pytest.mark.parametrize(
    "worker,message", [(failure_worker, "service rejected"), (crash_worker, "7")]
)
def test_failure_and_abrupt_exit_become_durable_terminal_states(
    tmp_path, worker, message
):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    manager = JobManager(store, worker=worker)
    try:
        task = manager.submit("video", params(), {})
        record = await_status(store, task["id"], {"failed"})
        assert message in record["payload"]["error"]
    finally:
        manager.shutdown()
        store.close()


def test_cancel_running_and_queued_jobs_and_shutdown_are_bounded(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    manager = JobManager(store, worker=slow_worker)
    try:
        one = manager.submit("video", params(), {})
        await_status(store, one["id"], {"running"})
        two = manager.submit("video", params(), {})
        manager.cancel(two["id"])
        manager.cancel(one["id"])
        assert store.get(two["id"])["status"] == "cancelled"
        assert store.get(one["id"])["status"] == "cancelled"
        with pytest.raises(ValueError):
            manager.cancel(one["id"])
        started = time.monotonic()
        manager.shutdown()
        assert time.monotonic() - started < 8
        assert not manager.thread.is_alive()
        with pytest.raises(ValueError):
            manager.submit("video", params(), {})
    finally:
        manager.shutdown()
        store.close()


@pytest.mark.parametrize("operation", ["poll", "recv"])
def test_windows_pipe_disconnect_is_eof_but_unrelated_io_errors_are_reported(operation):
    from unittest.mock import MagicMock
    from app.desktop.jobs import PIPE_CLOSED, read_event

    connection = MagicMock()
    connection.poll.return_value = True
    disconnected = OSError("The pipe has been ended")
    disconnected.winerror = 109
    getattr(connection, operation).side_effect = disconnected
    assert read_event(connection, 0) is PIPE_CLOSED
    getattr(connection, operation).side_effect = PermissionError("access denied")
    with pytest.raises(PermissionError):
        read_event(connection, 0)


def test_session_authentication_and_origin_guard(workspace):
    service, client = workspace
    client.cookies.clear()
    assert client.get("/health").status_code == 200
    assert client.get("/api/bootstrap").status_code == 401
    assert client.get("/session?key=wrong").status_code == 401
    assert client.get("/session?key=test-session-token").status_code == 200
    assert (
        client.put(
            "/api/settings", json={}, headers={"Origin": "https://untrusted.test"}
        ).status_code
        == 403
    )
    response = client.get("/")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/static/app.css").status_code == 200
    assert "config.toml" not in client.get("/static/config.toml").text
    bootstrap = client.get("/api/bootstrap").json()
    assert bootstrap["schema"]["properties"]["video_subject"]
    assert bootstrap["data_directory"] == str(service.directory)


def test_settings_validation_is_atomic_and_keys_never_enter_task_records(workspace):
    service, client = workspace
    assert client.put("/api/settings", json={"unknown": {}}).status_code == 400
    original = config.app["llm_provider"]
    assert (
        client.put(
            "/api/settings",
            json={"app": {"llm_provider": "openai"}, "azure": {"bad": {}}},
        ).status_code
        == 400
    )
    assert config.app["llm_provider"] == original
    assert (
        client.put(
            "/api/settings", json={"app": {"openai_api_key": "private-test-credential"}}
        ).status_code
        == 200
    )
    response = client.post("/api/tasks", json={"kind": "script", "params": params()})
    assert response.status_code == 200
    record = response.json()
    assert "private-test-credential" not in json.dumps(record)
    assert (
        service.jobs.requests[-1][0]["app"]["openai_api_key"]
        == "private-test-credential"
    )
    assert (service.directory / "config.toml").is_file()


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "unknown", "params": params()},
        {"kind": "video", "params": {}},
        {"kind": "preview", "params": {"video_subject": "topic"}},
        {"kind": "video", "params": params(n_threads=None)},
        {"kind": "video", "params": params(video_count=21)},
        {"kind": "video", "params": params(video_source="local")},
        {"kind": "video", "params": params(video_source="wavespeed")},
        {"kind": "video", "params": params(bgm_type="sonilo")},
        {"kind": "video", "params": params(custom_audio_file="/etc/passwd")},
        {
            "kind": "video",
            "params": params(video_source="loomloom"),
            "confirm_charge": True,
        },
    ],
)
def test_invalid_and_unconfirmed_jobs_are_rejected(workspace, body):
    service, client = workspace
    assert client.post("/api/tasks", json=body).status_code == 400
    assert service.store.list() == []


def test_auto_publication_requires_explicit_consent(workspace):
    service, client = workspace
    service.settings(
        {"app": {"upload_post_enabled": True, "upload_post_auto_upload": True}}
    )
    body = {"kind": "video", "params": params()}
    assert client.post("/api/tasks", json=body).status_code == 400
    assert (
        client.post("/api/tasks", json={**body, "confirm_publish": True}).status_code
        == 200
    )


def test_legacy_workspace_pauses_writes_and_reloads_config_when_closed(workspace):
    service, client = workspace
    service.settings({"app": {"openai_api_key": "before"}})
    service.legacy_active = True
    assert (
        client.put(
            "/api/settings", json={"app": {"openai_api_key": "clobber"}}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/tasks", json={"kind": "script", "params": params()}
        ).status_code
        == 400
    )
    import toml

    path = service.directory / "config.toml"
    changed = toml.load(path)
    changed["app"]["openai_api_key"] = "edited-in-legacy"
    path.write_text(toml.dumps(changed), encoding="utf-8")
    service.resume_from_legacy()
    assert not service.legacy_active
    assert service.snapshot()["app"]["openai_api_key"] == "edited-in-legacy"
    assert (
        client.post(
            "/api/tasks", json={"kind": "script", "params": params()}
        ).status_code
        == 200
    )


def test_quote_binds_inputs_and_claim_is_durable(workspace):
    service, client = workspace
    body = JobRequest(
        kind="loomloom_video_quote",
        params=params(video_source="loomloom"),
        options={"model_id": "model-1"},
    )
    quoted = service.submit(body)
    service.store.update(
        quoted["id"],
        "completed",
        quote={"input_rows": [{}], "listing_version_id": "listing-1"},
        fingerprint=config_fingerprint(service.snapshot()),
        options=body.options,
    )
    request = {
        "kind": "video",
        "params": body.params,
        "options": body.options,
        "quote_task_id": quoted["id"],
        "confirm_charge": True,
    }
    assert (
        client.post(
            "/api/tasks", json={**request, "options": {"model_id": "changed"}}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/tasks",
            json={
                **request,
                "params": params(video_source="loomloom", video_script="changed"),
            },
        ).status_code
        == 400
    )
    submitted = client.post("/api/tasks", json=request)
    assert submitted.status_code == 200
    assert (
        service.store.get(quoted["id"])["payload"]["used_by"] == submitted.json()["id"]
    )
    assert client.post("/api/tasks", json=request).status_code == 400
    assert service.jobs.requests[-1][1]["client_request_id"] == quoted["id"]


def test_preview_is_only_reused_for_matching_script_and_configuration(workspace):
    service, client = workspace
    preview = service.submit(JobRequest(kind="preview", params=params()))
    service.store.update(
        preview["id"],
        "completed",
        fingerprint=config_fingerprint(service.snapshot()),
        audio_file="preview.mp3",
    )
    body = {"kind": "video", "params": params(), "preview_task_id": preview["id"]}
    assert client.post("/api/tasks", json=body).status_code == 200
    assert service.jobs.requests[-1][1]["preview"]["audio_file"] == "preview.mp3"
    assert (
        client.post(
            "/api/tasks", json={**body, "params": params(video_script="different")}
        ).status_code
        == 200
    )
    assert "preview" not in service.jobs.requests[-1][1]
    service.settings({"app": {"openai_api_key": "changed-account"}})
    client.post("/api/tasks", json=body)
    assert "preview" not in service.jobs.requests[-1][1]


def test_material_import_preview_and_deletion_respect_task_ownership(workspace):
    service, client = workspace
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "#137d6f").save(image, "PNG")
    uploaded = client.post(
        "/api/assets/materials",
        files={"file": ("sample #1.png", image.getvalue(), "image/png")},
    )
    assert uploaded.status_code == 200
    listed = client.get("/api/assets/materials").json()
    assert "%23" in listed[0]["url"]
    assert client.get(listed[0]["url"]).content == image.getvalue()
    assert (
        client.post(
            "/api/assets/fonts", files={"file": ("bad.ttf", b"invalid font")}
        ).status_code
        == 400
    )
    task = service.submit(
        JobRequest(
            kind="video",
            params=params(
                video_source="local",
                video_materials=[{"provider": "local", "url": uploaded.json()["path"]}],
            ),
        )
    )
    assert client.delete(listed[0]["url"]).status_code == 400
    assert client.post("/api/cache/clean").status_code == 400
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 400
    client.post(f"/api/tasks/{task['id']}/cancel")
    assert client.delete(listed[0]["url"]).status_code == 200
    assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
    assert client.get(f"/api/tasks/{task['id']}").status_code == 400


def test_artifacts_cannot_escape_task_directory(workspace):
    service, client = workspace
    task = service.store.create("video", params())
    directory = service.directory / "storage/tasks" / task["id"]
    directory.mkdir(parents=True)
    (directory / "final-1.mp4").write_bytes(b"video")
    outside = service.directory / "private.txt"
    outside.write_text("private")
    try:
        (directory / "symlink.mp4").symlink_to(outside)
    except OSError:
        pytest.skip("This platform does not permit symlink creation")
    assert (
        client.get(f"/api/tasks/{task['id']}/files/final-1.mp4?download=true").content
        == b"video"
    )
    assert client.get(f"/api/tasks/{task['id']}/files/symlink.mp4").status_code == 400
    with pytest.raises(ValueError):
        service.artifact(task["id"], "../../../private.txt")
    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["files"] and detail["logs"] == []


def test_portable_presets_strip_machine_paths_and_validate_values():
    payload = {
        "format": "mpt-desktop-preset",
        "version": 1,
        "params": params(
            custom_audio_file="/private/audio.mp3",
            bgm_type="custom",
            bgm_file="/private/music.mp3",
            video_materials=[{"url": "/private/video.mp4"}],
        ),
    }
    restored = parse_preset(payload)
    assert (
        not restored["custom_audio_file"]
        and not restored["video_materials"]
        and not restored["bgm_file"]
    )
    assert restored["bgm_type"] == ""
    with pytest.raises(ValueError):
        parse_preset({"format": "credentials", "version": 1, "params": {}})
    with pytest.raises(ValueError):
        parse_preset({**payload, "params": params(paragraph_number=99)})


def test_subtitle_timing_round_trip_and_credential_redaction():
    from edge_tts import SubMaker

    maker = SubMaker()
    maker.feed(
        {
            "type": "WordBoundary",
            "offset": 10000000,
            "duration": 5000000,
            "text": "hello",
        }
    )
    restored = restore_submaker(serialize_submaker(maker))
    assert restored.cues == maker.cues
    snapshot = {
        "app": {
            "api_keys": ["private-key-a", "private-key-b"],
            "api_token": "private-token",
            "model": "visible-model",
        }
    }
    assert (
        redact(
            "private-key-a private-key-b private-token visible-model",
            secret_values(snapshot),
        )
        == "*** *** *** visible-model"
    )


@pytest.mark.parametrize("kind", ["script", "terms", "connection", "social", "voices"])
def test_worker_dispatches_to_existing_services_and_reports_errors(kind, monkeypatch):
    from app.desktop import worker
    from app.services import llm, state
    from queue import Queue

    monkeypatch.setattr(state, "state", state.state)

    monkeypatch.setattr(llm, "generate_script", lambda *args: "Generated script")
    monkeypatch.setattr(llm, "generate_terms", lambda *args, **kwargs: ["one", "two"])
    monkeypatch.setattr(llm, "test_connection", lambda: (True, "connected", 0.12))
    monkeypatch.setattr(
        llm, "generate_social_metadata", lambda *args: {"title": "Title"}
    )
    job = {
        "id": "worker-test",
        "kind": kind,
        "params": params(),
        "config": {},
        "options": {"provider": "none"},
    }
    result = worker.perform(job, Queue())
    assert (
        result
        == {
            "script": {"script": "Generated script"},
            "terms": {"terms": ["one", "two"]},
            "connection": {"elapsed": 0.12, "message": "模型连接成功"},
            "social": {"title": "Title"},
            "voices": {"voices": ["none"]},
        }[kind]
    )
    if kind == "script":
        monkeypatch.setattr(llm, "generate_script", lambda *args: "Error: rejected")
        with pytest.raises(RuntimeError, match="rejected"):
            worker.perform(job, Queue())
    if kind == "terms":
        monkeypatch.setattr(llm, "generate_terms", lambda *args, **kwargs: [])
        with pytest.raises(RuntimeError):
            worker.perform(job, Queue())
    if kind == "connection":
        monkeypatch.setattr(
            llm, "test_connection", lambda: (False, "bad credentials", 0.1)
        )
        with pytest.raises(RuntimeError, match="bad credentials"):
            worker.perform(job, Queue())


def test_preview_preserves_real_timing_for_later_reuse(tmp_path, monkeypatch):
    from app.desktop import worker
    from app.services import voice, state
    from edge_tts import SubMaker
    from queue import Queue

    monkeypatch.setattr(state, "state", state.state)

    monkeypatch.setenv("MPT_DATA_DIR", str(tmp_path))
    maker = SubMaker()
    maker.feed({"type": "WordBoundary", "offset": 0, "duration": 2500000, "text": "hi"})

    def tts(**kwargs):
        assert kwargs["voice_volume"] == 1
        Path(kwargs["voice_file"]).write_bytes(b"preview-audio")
        return maker

    monkeypatch.setattr(voice, "tts", tts)
    monkeypatch.setattr(voice, "get_audio_duration", lambda filename: 0.25)
    job = {
        "id": "preview-test",
        "kind": "preview",
        "params": params(),
        "config": {},
        "options": {},
    }
    result = worker.perform(job, Queue())
    assert Path(result["audio_file"]).read_bytes() == b"preview-audio"
    assert worker.restore_submaker(result["sub_maker"]).cues == maker.cues
    assert result["duration"] == 0.25
    monkeypatch.setattr(voice, "tts", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="试听失败"):
        worker.perform(job, Queue())


def test_audio_stage_does_not_require_video_quote_and_engine_failure_is_terminal(
    tmp_path, monkeypatch
):
    from app.desktop import worker
    from app.services import task, state
    from app.models import const
    from queue import Queue
    from unittest.mock import MagicMock

    monkeypatch.setenv("MPT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task, "_cross_post_executor", MagicMock())
    original = state.state

    def start(task_id, params, **kwargs):
        assert kwargs["loomloom_video_request"] is None
        state.state.update_task(task_id, state=const.TASK_STATE_COMPLETE, progress=100)
        return {"audio_file": "audio.mp3"}

    monkeypatch.setattr(task, "start", start)
    job = {
        "id": "audio-test",
        "kind": "audio",
        "params": params(video_source="loomloom"),
        "config": {},
        "options": {},
    }
    events = Queue()
    try:
        result = worker.perform(job, events)
        assert result["audio_file"] == "audio.mp3"
        assert events.get()["data"]["progress"] == 100

        def fail(task_id, params, **kwargs):
            state.state.update_task(
                task_id, state=const.TASK_STATE_FAILED, error="generation failed"
            )

        monkeypatch.setattr(task, "start", fail)
        with pytest.raises(RuntimeError, match="generation failed"):
            worker.perform(job, events)
    finally:
        state.state = original


def test_native_bridge_import_export_uses_dialog_selection_and_validated_artifacts(
    workspace, tmp_path, monkeypatch
):
    import desktop
    from unittest.mock import MagicMock

    service, _ = workspace
    record = service.store.create("video", params())
    folder = service.directory / "storage/tasks" / record["id"]
    folder.mkdir(parents=True)
    (folder / "final-1.mp4").write_bytes(b"complete-video")
    bridge = desktop.NativeBridge(service)
    bridge._window = MagicMock()
    destination = tmp_path / "exported.mp4"
    bridge._window.create_file_dialog.return_value = (str(destination),)
    assert bridge.export_file(record["id"], "final-1.mp4") == str(destination)
    assert destination.read_bytes() == b"complete-video"
    bridge._window.create_file_dialog.return_value = None
    assert bridge.export_file(record["id"], "final-1.mp4") is None
    with pytest.raises(ValueError):
        bridge.export_file(record["id"], "../../config.toml")
    destination = tmp_path / "preset.json"
    bridge._window.create_file_dialog.return_value = (str(destination),)
    assert bridge.export_preset(
        {"format": "mpt-desktop-preset", "version": 1, "params": params()}
    )
    assert bridge.import_preset()["params"]["video_subject"] == "desktop test"
    assert "video_materials" not in json.loads(destination.read_text())["params"]
