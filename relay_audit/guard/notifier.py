"""Independent bounded notification channel; never writes into model responses."""

import asyncio
import inspect
import sys


class Notifier:
    def __init__(self, config=None, store=None, sink=None):
        config = config or {}
        self.config = config.get("notifications", config)
        self.store = store
        self.sink = sink
        self.queue = asyncio.Queue(maxsize=max(1, min(int(self.config.get("queue_size", 64)), 1024)))
        self.failures = 0
        self.dropped = 0
        self.delivered = 0
        self._task = None

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="guard-notifier")

    async def notify(self, event):
        await self.start()
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            await self._record_failure("notification_queue_full")
            return False

    async def emit(self, event):
        return await self.notify(event)

    async def _record_failure(self, reason):
        if self.store is not None:
            try:
                await asyncio.to_thread(self.store.incident, "monitoring_degraded",
                                        {"reason": reason, "identity_status": "unverified"}, "", reason)
            except Exception:
                # No user or provider-controlled exception text is printed.
                self.failures += 1

    async def _worker(self):
        while True:
            event = await self.queue.get()
            try:
                messages = {
                    "declared_model_mismatch": "声明模型不一致；真实模型身份未验证",
                    "protocol_or_capability_error": "观察到协议或能力兼容错误；真实模型身份未验证",
                    "service_degradation": "可比请求的服务表现发生变化；真实模型身份未验证",
                    "behavioral_drift_suspected": "后台复核支持疑似行为变化；真实模型身份未验证",
                    "monitoring_degraded": "监测已降级；不能据此判断端点正常",
                    "recovered": "当前可比观察已恢复；真实模型身份未验证",
                    "probe_review": "独立复核已记录；真实模型身份未验证",
                }
                text = "[AgentGuard] " + messages.get(event.get("event_type"), "监测事件；真实模型身份未验证") + "。证据已保存在本地，当前任务继续运行。"
                if self.config.get("terminal", True):
                    if self.sink is None:
                        await asyncio.to_thread(print, text, file=sys.stderr, flush=True)
                    else:
                        result = await asyncio.to_thread(self.sink, text)
                        if inspect.isawaitable(result):
                            await result
                if self.config.get("system", False):
                    # Fixed source; the notice is an argv string, never AppleScript code.
                    script = 'on run argv\n display notification (item 1 of argv) with title "RelayAudit AgentGuard"\nend run'
                    process = await asyncio.create_subprocess_exec("osascript", "-e", script, text,
                                                                   stdout=asyncio.subprocess.DEVNULL,
                                                                   stderr=asyncio.subprocess.DEVNULL)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    if process.returncode:
                        raise RuntimeError("notification_unavailable")
                self.delivered += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1
                await self._record_failure("notification_failed")
            finally:
                self.queue.task_done()

    def snapshot(self):
        return {"delivered": self.delivered, "failures": self.failures, "dropped": self.dropped,
                "queue_size": self.queue.qsize()}

    async def close(self):
        if self._task is not None:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=1)
            except asyncio.TimeoutError:
                pass
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
