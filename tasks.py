# -*- coding: utf-8 -*-
"""长任务注册表（v3.54）：状态/阶段进度/结果/取消边界/幂等。

设计：
- 任务进程内注册（单实例部署）；task_id 幂等键 = kind+目标摘要（重复创建返回原任务）。
- 进度按真实阶段记录（stages 列表 + 当前阶段 + 已完成阶段数），不伪造百分比。
- 取消边界：只在阶段切换点响应取消（阶段内不可中断的操作会先完成再停）。
- 结果与错误保留至进程结束；恢复/导入/名录更新等长操作逐步接入。
"""
import threading
import time
import uuid

_lock = threading.Lock()
_TASKS = {}


def create(kind, stages, run_fn, idem_key=None):
    """创建任务并立即在后台线程执行。run_fn(task) 在各阶段间调用 task.check_cancel()。"""
    with _lock:
        if idem_key:
            for t in _TASKS.values():
                if t["idem_key"] == idem_key and t["status"] in ("running", "pending"):
                    return dict(t)
        tid = uuid.uuid4().hex[:12]
        task = {"id": tid, "kind": kind, "status": "running", "stages": list(stages),
                "stage_index": 0, "progress_text": stages[0] if stages else "",
                "result": None, "error": None, "cancel_requested": False,
                "idem_key": idem_key, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "ended_at": None}
        _TASKS[tid] = task

    def runner():
        try:
            run_fn(task)
            if task["cancel_requested"]:
                task["status"] = "cancelled"
            else:
                task["status"] = "done"
                task["stage_index"] = len(task["stages"])
        except Exception as e:  # noqa: BLE001
            task["status"] = "failed"
            task["error"] = str(e)[:300]
        finally:
            task["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    threading.Thread(target=runner, daemon=True).start()
    return dict(task)


def get(tid):
    with _lock:
        t = _TASKS.get(tid)
        return dict(t) if t else None


def request_cancel(tid):
    with _lock:
        t = _TASKS.get(tid)
        if not t:
            return None
        t["cancel_requested"] = True
        return dict(t)


def advance(task, text=None):
    """阶段推进（由 run_fn 在每个阶段完成时调用）。"""
    with _lock:
        if task["cancel_requested"]:
            raise KeyboardInterrupt("任务已请求取消")
        task["stage_index"] = min(len(task["stages"]), task["stage_index"] + 1)
        if text:
            task["progress_text"] = text


def snapshot_list():
    with _lock:
        return [dict(t) for t in _TASKS.values()]
