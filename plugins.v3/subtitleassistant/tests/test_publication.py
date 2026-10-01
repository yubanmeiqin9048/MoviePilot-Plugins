"""字幕落盘事件与宿主广播适配器测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from app.plugins.subtitleassistant.event import publication as publication_module
from app.plugins.subtitleassistant.event.publication import SubtitleEvents
from app.schemas.types import EventType
from app.plugins.subtitleassistant.schemas.event import SubtitleWrittenEvent, SubtitleWrittenOperation

pytestmark = pytest.mark.anyio


class _EventManager:
    """收集宿主广播调用的测试替身。"""

    def __init__(self, *, error: BaseException | None = None) -> None:
        """保存可选广播异常。"""

        self.error = error
        self.calls: list[tuple[EventType, dict[str, str | None]]] = []

    def send_event(self, event_type: EventType, data: dict[str, str | None]) -> None:
        """记录事件或抛出预设异常。"""

        if self.error is not None:
            raise self.error
        self.calls.append((event_type, data))


async def test_host_publisher_serializes_all_operations_and_fixed_action() -> None:
    """四种业务操作都广播同一宿主事件类型与固定动作。"""

    manager = _EventManager()
    publisher = SubtitleEvents(manager)

    for operation in SubtitleWrittenOperation:
        await publisher.publish(
            SubtitleWrittenEvent(
                plugin_id="SubtitleAssistant",
                operation=operation,
                task_id=None if operation is SubtitleWrittenOperation.RETARGET else "task-1",
                record_id="record-1",
                target_path=Path("/media/video.mkv"),
                subtitle_path=Path("/media/video.chi.zh-cn.srt"),
            )
        )

    assert [event_type for event_type, _payload in manager.calls] == [EventType.PluginAction] * 4
    assert [payload["action"] for _event_type, payload in manager.calls] == ["subtitle_written"] * 4
    assert [payload["operation"] for _event_type, payload in manager.calls] == [
        operation.value for operation in SubtitleWrittenOperation
    ]
    assert manager.calls[-1][1]["task_id"] is None
    assert manager.calls[0][1] == {
        "plugin_id": "SubtitleAssistant",
        "action": "subtitle_written",
        "operation": "automatic_candidate",
        "task_id": "task-1",
        "record_id": "record-1",
        "target_path": "/media/video.mkv",
        "subtitle_path": "/media/video.chi.zh-cn.srt",
    }


async def test_host_publisher_swallow_broadcast_failure_and_logs_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主入队失败只记录异常类型，不泄漏事件内容或改变调用结果。"""

    messages: list[str] = []
    monkeypatch.setattr(publication_module, "logger", SimpleNamespace(warning=messages.append))
    publisher = SubtitleEvents(_EventManager(error=RuntimeError("secret path")))

    await publisher.publish(
        SubtitleWrittenEvent(
            plugin_id="SubtitleAssistant",
            operation=SubtitleWrittenOperation.RETARGET,
            task_id=None,
            record_id="record-1",
            target_path=Path("/private/media.mkv"),
            subtitle_path=Path("/private/media.chi.zh-cn.srt"),
        )
    )

    assert len(messages) == 1
    assert "异常类型为 RuntimeError" in messages[0]
    assert "secret path" not in messages[0]
    assert "/private" not in messages[0]
