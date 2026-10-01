"""Serial scheduling with process isolation, durable progress and bounded shutdown."""

import copy
import multiprocessing
import threading

from app.desktop.store import ACTIVE
from app.desktop.worker import worker_main


def terminate_process_tree(process):
    if not process:
        return
    try:
        pid = process.pid
    except ValueError:
        return  # Scheduler has already closed a finished multiprocessing handle.
    if not pid:
        return
    import psutil

    try:
        parent = psutil.Process(pid)
        descendants = parent.children(recursive=True)
        for child in reversed(descendants):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
        # Only multiprocessing/Popen may reap its direct child. psutil.wait()
        # would steal waitpid(), leaving multiprocessing.is_alive() stuck True.
        _, alive = psutil.wait_procs(descendants, timeout=2)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    try:
        if hasattr(process, "join"):
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        else:
            process.wait(timeout=3)
    except (ValueError, ProcessLookupError):
        pass
    except Exception:
        process.kill()


class EventWriter:
    def __init__(self, connection):
        self.connection = connection
        self.lock = threading.Lock()

    def put(self, event):
        with self.lock:
            self.connection.send(event)


def worker_entry(worker, job, connection):
    try:
        worker(job, EventWriter(connection))
    finally:
        connection.close()


class JobManager:
    def __init__(self, store, worker=worker_main):
        self.store = store
        self.worker = worker
        self.context = multiprocessing.get_context("spawn")
        self.condition = threading.Condition(threading.RLock())
        self.pending = []
        self.active = None
        self.cancelled = set()
        self.stopping = False
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="desktop-scheduler"
        )
        self.store.recover()
        self.thread.start()

    def submit(self, kind, params, config, options=None):
        with self.condition:
            if self.stopping:
                raise ValueError("程序正在退出")
            if len(self.pending) >= 100:
                raise ValueError("任务队列已满，请等待当前任务完成")
            task = self.store.create(kind, params)
            self.pending.append(
                {
                    "id": task["id"],
                    "kind": kind,
                    "params": copy.deepcopy(params),
                    "config": copy.deepcopy(config),
                    "options": copy.deepcopy(options or {}),
                }
            )
            self.condition.notify_all()
            return task

    def cancel(self, task_id):
        with self.condition:
            task = self.store.get(task_id)
            if not task or task["status"] not in ACTIVE:
                raise ValueError("任务已结束")
            self.cancelled.add(task_id)
            self.pending = [job for job in self.pending if job["id"] != task_id]
            process = (
                self.active[1] if self.active and self.active[0] == task_id else None
            )
            self.store.update(
                task_id,
                "cancelled",
                error="任务已取消；已提交的远端生成或发布可能仍在进行。",
            )
        if process:
            terminate_process_tree(process)

    def _apply_event(self, task_id, event):
        with self.condition:
            if task_id in self.cancelled or self.stopping:
                return
            if event["kind"] == "log":
                self.store.log(task_id, event["message"])
            elif event["kind"] == "state":
                data = {
                    key: value
                    for key, value in event["data"].items()
                    if key not in {"task_id", "status"}
                }
                publishing = data.get("cross_post_state") in {"pending", "processing"}
                self.store.update(task_id, "publishing" if publishing else None, **data)
            elif event["kind"] == "result":
                data = {
                    key: value
                    for key, value in event["data"].items()
                    if key not in {"task_id", "status"}
                }
                self.store.update(task_id, "completed", **{**data, "progress": 100})
            elif event["kind"] == "error":
                self.store.update(task_id, "failed", error=event["message"])

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.stopping)
                if self.stopping:
                    return
                job = self.pending.pop(0)
                events, sender = self.context.Pipe(duplex=False)
                process = self.context.Process(
                    target=worker_entry, args=(self.worker, job, sender)
                )
                self.active = (job["id"], process)
                self.store.update(job["id"], "running", progress=0)
                try:
                    process.start()
                    sender.close()
                except Exception as exc:
                    self.store.update(
                        job["id"], "failed", error=f"无法启动生成进程：{exc}"
                    )
                    self.active = None
                    events.close()
                    sender.close()
                    continue
            try:
                while process.is_alive():
                    if events.poll(0.2):
                        try:
                            self._apply_event(job["id"], events.recv())
                        except EOFError:
                            break
                process.join(timeout=1)
                while events.poll():
                    try:
                        self._apply_event(job["id"], events.recv())
                    except EOFError:
                        break
                with self.condition:
                    task = self.store.get(job["id"])
                    if task and task["status"] in ACTIVE:
                        self.store.update(
                            job["id"],
                            "interrupted" if self.stopping else "failed",
                            error=f"生成进程意外结束（退出码 {process.exitcode}）",
                        )
            except Exception as exc:
                self.store.update(job["id"], "failed", error=f"任务监控失败：{exc}")
                terminate_process_tree(process)
            finally:
                events.close()
                with self.condition:
                    self.active = None
                    self.cancelled.discard(job["id"])
                process.close()

    def shutdown(self):
        with self.condition:
            self.stopping = True
            process = self.active[1] if self.active else None
            for job in self.pending:
                self.store.update(
                    job["id"], "interrupted", error="程序退出，排队任务未执行"
                )
            self.pending.clear()
            self.condition.notify_all()
        if process:
            terminate_process_tree(process)
        self.thread.join(timeout=6)
