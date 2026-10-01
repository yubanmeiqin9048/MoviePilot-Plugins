"""固定 Bearer API、权限、分页、删除与凭据契约测试。"""

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.plugins.subtitleassistant import SubtitleAssistant
from app.plugins.subtitleassistant.api import ApiController
from app.plugins.subtitleassistant.record import RecordCommitter
from app.plugins.subtitleassistant.schemas.candidate import (
    CandidateRecognition,
    CandidateRecognitionStatus,
    SubtitleCandidate,
)
from app.plugins.subtitleassistant.schemas.http.page import PageSize
from app.plugins.subtitleassistant.schemas.http.record import (
    BatchRecordDeleteRequest,
    BatchRetargetPreviewRequest,
    BatchRetargetSubmitRequest,
    RecordDeleteRequest,
    RetargetRequest,
)
from app.plugins.subtitleassistant.schemas.http.search import (
    ManualDownloadRequest,
    ManualSearchRequest,
    ManualSourceResult,
)
from app.plugins.subtitleassistant.schemas.http.source import CredentialUpdate
from app.plugins.subtitleassistant.schemas.record import (
    BatchRetargetPreview as DomainBatchRetargetPreview,
)
from app.plugins.subtitleassistant.schemas.record import (
    BatchRetargetPreviewItem as DomainBatchRetargetPreviewItem,
)
from app.plugins.subtitleassistant.schemas.record import (
    BatchRetargetResult as DomainBatchRetargetResult,
)
from app.plugins.subtitleassistant.schemas.record import (
    BatchRetargetResultItem as DomainBatchRetargetResultItem,
)
from app.plugins.subtitleassistant.schemas.record import (
    FileLocation,
    MatchRecord,
    RecordStatus,
    RetargetPreview,
    RetargetResult,
)
from app.plugins.subtitleassistant.schemas.search import (
    ManualSearchResult,
    ManualSourceView,
    ManualSubmitResult,
)
from app.plugins.subtitleassistant.schemas.source import SourcePlanEntry, SourceSearchResult, SubtitleSource
from app.plugins.subtitleassistant.schemas.target import MediaType, SearchTarget, SubtitleTarget
from app.plugins.subtitleassistant.schemas.task import SubtitleTask, TaskStatus
from app.api.dependencies.auth import (
    get_current_active_manage_user_async,
    get_current_active_superuser_async,
)


class _ApiStore:
    """为 API 控制器提供可观测的内存存储。"""

    def __init__(
        self,
        tasks: list[SubtitleTask] | None = None,
        records: list[MatchRecord] | None = None,
        calls: list[str] | None = None,
    ) -> None:
        """保存任务、记录及可选调用日志。"""

        self.tasks = {item.id: item.model_copy(deep=True) for item in tasks or []}
        self.records = {item.id: item.model_copy(deep=True) for item in records or []}
        self.calls = calls if calls is not None else []

    async def list_tasks(self) -> list[SubtitleTask]:
        """返回任务快照。"""

        return [item.model_copy(deep=True) for item in self.tasks.values()]

    async def get_task(self, task_id: str) -> SubtitleTask | None:
        """返回单个任务快照。"""

        task = self.tasks.get(task_id)
        return task.model_copy(deep=True) if task else None

    async def delete_task(self, task_id: str) -> bool:
        """删除任务并记录调用。"""

        self.calls.append(f"store.delete_task:{task_id}")
        return self.tasks.pop(task_id, None) is not None

    async def list_records(self) -> list[MatchRecord]:
        """返回记录快照。"""

        return [item.model_copy(deep=True) for item in self.records.values()]

    async def get_record(self, record_id: str) -> MatchRecord | None:
        """返回单个记录快照。"""

        record = self.records.get(record_id)
        return record.model_copy(deep=True) if record else None

    async def delete_record(self, record_id: str) -> bool:
        """删除记录并记录调用。"""

        self.calls.append(f"store.delete_record:{record_id}")
        return self.records.pop(record_id, None) is not None

    async def delete_record_if_match(self, expected: MatchRecord) -> bool:
        """仅删除仍保持确认版本的记录并记录调用。"""

        current = self.records.get(expected.id)
        if current is None:
            return False
        if (
            current.status is not expected.status
            or current.location is not expected.location
            or current.path != expected.path
            or current.updated_at != expected.updated_at
        ):
            return False
        self.calls.append(f"store.delete_record:{expected.id}")
        del self.records[expected.id]
        return True

    async def save_record(self, record: MatchRecord) -> None:
        """恢复记录快照并记录调用。"""

        self.calls.append(f"store.save_record:{record.id}")
        self.records[record.id] = record.model_copy(deep=True)

    async def list_source_statuses(self) -> list[Any]:
        """当前测试不提供来源状态。"""

        return []


class _ApiFileSystem:
    """记录 API 发起的可回滚字幕文件删除。"""

    def __init__(self, calls: list[str]) -> None:
        """绑定调用日志。"""

        self.calls = calls
        self.error: Exception | None = None

    async def stage_file_deletion(self, path: Path) -> Path | None:
        """暂存文件删除或抛出预设异常。"""

        self.calls.append(f"filesystem.stage:{path}")
        if self.error is not None:
            raise self.error
        return None

    async def commit_file_deletion(self, backup: Path | None) -> None:
        """提交文件删除并记录调用。"""

        self.calls.append(f"filesystem.commit:{backup}")

    async def rollback_file_deletion(self, original: Path, backup: Path | None) -> None:
        """回滚文件删除并记录调用。"""

        self.calls.append(f"filesystem.rollback:{original}:{backup}")

    async def plugin_file_path(self, path: str) -> Path:
        """把插件数据相对路径解析为测试中的完整路径。"""

        return Path("/plugin-data") / path


class _ApiTaskOperations:
    """通过任务能力 facade 暴露 API 测试所需的任务操作。"""

    def __init__(self, store: _ApiStore) -> None:
        """绑定任务持久化替身。"""

        self._store = store

    async def list_tasks(self) -> list[SubtitleTask]:
        """读取全部任务快照。"""

        return await self._store.list_tasks()

    async def get_task(self, task_id: str) -> SubtitleTask | None:
        """按标识读取任务快照。"""

        return await self._store.get_task(task_id)

    async def delete_task(self, task_id: str) -> bool:
        """删除任务快照。"""

        return await self._store.delete_task(task_id)


class _ApiRetargeting:
    """为批量改配 API 提供可编排的领域结果。"""

    def __init__(self) -> None:
        """初始化空结果与调用记录。"""

        self.preview_result: DomainBatchRetargetPreview | None = None
        self.batch_result: DomainBatchRetargetResult | None = None
        self.preview_calls: list[list[Any]] = []
        self.batch_calls: list[list[Any]] = []

    async def preview_batch(self, mappings: list[Any]) -> DomainBatchRetargetPreview:
        """返回预设批量预览结果。"""

        self.preview_calls.append(mappings)
        assert self.preview_result is not None
        return self.preview_result

    async def retarget_batch(self, mappings: list[Any]) -> DomainBatchRetargetResult:
        """返回预设批量执行结果。"""

        self.batch_calls.append(mappings)
        assert self.batch_result is not None
        return self.batch_result


class _ApiPlugin:
    """组合 API 控制器需要的轻量插件能力。"""

    def __init__(
        self,
        tasks: list[SubtitleTask] | None = None,
        records: list[MatchRecord] | None = None,
    ) -> None:
        """创建内存依赖与凭据调用记录。"""

        self.calls: list[str] = []
        self.store = _ApiStore(tasks, records, self.calls)
        self.coordinator = _ApiTaskOperations(self.store)
        self.filesystem = _ApiFileSystem(self.calls)
        self.record_committer = RecordCommitter(
            self.store,
            self.filesystem,
            records or [],
            ["srt"],
            ["assrt"],
        )
        self.record_catalog = self.record_committer.catalog()
        self.record_maintenance = _ApiRetargeting()
        self.credential_updates: list[tuple[SubtitleSource, dict[str, str]]] = []
        self.targets = SimpleNamespace(list_targets=AsyncMock())
        self.manual_search = SimpleNamespace(
            search=AsyncMock(),
            submit=AsyncMock(),
        )
        self.source_service = SimpleNamespace(
            statuses=AsyncMock(return_value=[]),
            default_queries=lambda source, context: (),
        )

    async def update_source_credentials(self, source: SubtitleSource, values: dict[str, str]) -> bool:
        """记录凭据更新并模拟配置完整。"""

        allowed = {
            SubtitleSource.OPENSUBTITLES: {"api_key", "username", "password"},
            SubtitleSource.ASSRT: {"token"},
        }[source]
        if set(values) - allowed:
            raise ValueError("请求包含不属于该字幕源的凭据字段")
        self.credential_updates.append((source, dict(values)))
        return True

    async def clear_source_credentials(self, source: SubtitleSource) -> bool:
        """记录凭据清除。"""

        self.calls.append(f"credentials.clear:{source.value}")
        return True


def _controller(plugin: _ApiPlugin) -> ApiController:
    """以显式 capability facade 创建 API 控制器。"""

    return ApiController(
        tasks=plugin.coordinator,
        records=plugin.record_catalog,
        maintenance=plugin.record_maintenance,
        filesystem=plugin.filesystem,
        targets=plugin.targets,
        search=plugin.manual_search,
        sources=plugin.source_service,
        update_credentials=plugin.update_source_credentials,
        clear_credentials=plugin.clear_source_credentials,
    )


def _task(
    task_id: str,
    status: TaskStatus,
    created_at: datetime,
    *,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    title: str = "测试媒体",
    season: int | None = None,
    episode: int | None = None,
    reason: str | None = None,
) -> SubtitleTask:
    """构造 API 列表测试任务。"""

    return SubtitleTask(
        id=task_id,
        media_title=title,
        year=2024,
        media_type=MediaType.TV,
        season=season,
        episode=episode,
        target_file_name=f"{task_id}.mkv",
        target_path=Path(f"/media/{task_id}.mkv"),
        status=status,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        reason_code="reason_code" if reason else None,
        reason_message=reason,
    )


def _record(
    record_id: str,
    status: RecordStatus,
    updated_at: datetime,
    *,
    title: str = "测试媒体",
    path: str | None = None,
) -> MatchRecord:
    """构造 API 列表与删除测试记录。"""

    return MatchRecord(
        id=record_id,
        subtitle_file_name=f"{record_id}.srt",
        format="SRT",
        media_title=title,
        year=2024,
        media_type=MediaType.TV,
        season=2,
        episode=3,
        status=status,
        source=SubtitleSource.ASSRT,
        location=FileLocation.MEDIA_DIRECTORY if status is RecordStatus.MATCHED else FileLocation.PLUGIN_DATA,
        path=Path(path or f"{status.value}/{record_id}.srt"),
        source_task_id="task-1",
        candidate_key=f"candidate:{record_id}",
        language="简体中文",
        created_at=updated_at - timedelta(minutes=1),
        updated_at=updated_at,
    )


def _delete_payload(record: MatchRecord, mode: str = "record_and_file") -> RecordDeleteRequest:
    """构造用户确认时提交的匹配记录删除快照。"""

    return RecordDeleteRequest(
        delete_mode=mode,  # type: ignore[arg-type]
        expected_status=record.status,
        expected_location=record.location,
        expected_path=str(record.path),
        expected_updated_at=record.updated_at,
    )


def _batch_delete_payload(
    records: list[MatchRecord],
    mode: str = "record_and_file",
) -> BatchRecordDeleteRequest:
    """构造一批使用相同删除模式的确认快照。"""

    return BatchRecordDeleteRequest.model_validate(
        {
            "delete_mode": mode,
            "items": [
                {
                    "record_id": record.id,
                    "expected_status": record.status,
                    "expected_location": record.location,
                    "expected_path": str(record.path),
                    "expected_updated_at": record.updated_at,
                }
                for record in records
            ],
        }
    )


def _retarget_target(history_id: int = 7) -> SearchTarget:
    """构造批量改配 API 响应使用的整理历史目标。"""

    target_path = f"/history/target-{history_id}.mkv"
    return SearchTarget(
        history_id=history_id,
        context=SubtitleTarget(
            title="目标影片",
            year=2024,
            media_type=MediaType.MOVIE,
            tmdb_id=100,
            target_path=Path(target_path),
            target_file_name=Path(target_path).name,
            target_storage="local",
        ),
        transferred_at=datetime(2026, 7, 24, tzinfo=UTC),
    )


def _dependency(endpoint: Any) -> Any:
    """读取端点下划线参数声明的 FastAPI 依赖。"""

    return inspect.signature(endpoint).parameters["_"].default.dependency


def test_get_api_returns_exactly_eighteen_fresh_bearer_routes_with_role_dependencies() -> None:
    """get_api 每次返回新的固定十八条路由并声明正确角色依赖。"""

    plugin = object.__new__(SubtitleAssistant)
    plugin._runtime = SimpleNamespace(get_api=_controller(_ApiPlugin()).routes)
    first = plugin.get_api()

    assert len(first) == 18
    assert {(item["path"], tuple(item["methods"])) for item in first} == {
        ("/tasks", ("GET",)),
        ("/tasks/{task_id}", ("GET",)),
        ("/tasks/{task_id}", ("DELETE",)),
        ("/records", ("GET",)),
        ("/records/batch-delete", ("POST",)),
        ("/records/{record_id}", ("GET",)),
        ("/records/{record_id}", ("DELETE",)),
        ("/targets", ("GET",)),
        ("/searches", ("POST",)),
        ("/searches/{session_id}/downloads", ("POST",)),
        ("/records/batch-retarget-preview", ("POST",)),
        ("/records/batch-retarget", ("POST",)),
        ("/records/{record_id}/retarget-preview", ("POST",)),
        ("/records/{record_id}/retarget", ("POST",)),
        ("/sources/status", ("GET",)),
        ("/sources/refresh", ("POST",)),
        ("/credentials/{source}", ("PUT",)),
        ("/credentials/{source}", ("DELETE",)),
    }
    assert all(item["auth"] == "bear" for item in first)
    for route in first:
        expected = (
            get_current_active_superuser_async
            if route["path"].startswith("/credentials/")
            else get_current_active_manage_user_async
        )
        assert _dependency(route["endpoint"]) is expected

    first[0]["path"] = "/api/v1/plugin/SubtitleAssistant/tasks"
    first[0].pop("auth")
    first[0]["methods"].append("PATCH")
    first[0]["dependencies"] = [object()]
    second = plugin.get_api()

    assert len(second) == 18
    assert second[0]["path"] == "/tasks"
    assert second[0]["auth"] == "bear"
    assert second[0]["methods"] == ["GET"]
    assert "dependencies" not in second[0]
    assert all(new is not old for new, old in zip(second, first, strict=True))


@pytest.mark.anyio
async def test_target_list_endpoint_returns_current_raw_history_page() -> None:
    """整理历史列表 API 透传宿主行并传递服务端搜索与总数。"""

    raw_rows = [
        SimpleNamespace(id=8, status=False, host_added_field={"kind": "failure"}),
        SimpleNamespace(id=8, status=True, dest="/media/duplicate.mkv", title=None),
    ]

    class _Targets:
        """返回预置宿主原始分页。"""

        async def list_targets(self, page: int, page_size: int, search: str | None = None) -> Any:
            """按原始参数返回当前页。"""

            assert (page, page_size, search) == (2, 25, "*.mkv")
            return SimpleNamespace(items=raw_rows, page=page, page_size=page_size, total=2)

    plugin = _ApiPlugin()
    plugin.targets = _Targets()
    controller = _controller(plugin)

    assert "search" in inspect.signature(controller.list_targets).parameters
    response = await controller.list_targets(page=2, page_size=PageSize.ITEMS_25, search="*.mkv", _=object())

    assert response.model_dump(mode="json") == {
        "items": [
            {"id": 8, "status": False, "host_added_field": {"kind": "failure"}},
            {"id": 8, "status": True, "dest": "/media/duplicate.mkv", "title": None},
        ],
        "page": 2,
        "page_size": 25,
        "total": 2,
    }


@pytest.mark.anyio
async def test_target_search_plans_delegate_to_source_facade_structured_plans() -> None:
    """整理目标的查询计划直接取自来源 facade 的结构化计划，不再手工复刻阈值。"""

    context = SubtitleTarget(
        title="元气少女缘结神",
        original_title="神様はじめました",
        english_title="Kamisama Kiss",
        year=2012,
        media_type=MediaType.TV,
        season=1,
        episode=2,
        tmdb_id=62741,
        imdb_id="tt2320220",
        target_path=Path("/media/元气少女缘结神.S01E02.mkv"),
        target_file_name="元气少女缘结神.S01E02.mkv",
        target_storage="local",
    )
    target = SearchTarget(
        history_id=3,
        context=context,
        transferred_at=datetime(2026, 7, 19, tzinfo=UTC),
    )
    entries = {
        SubtitleSource.MOVIEPILOT: [SourcePlanEntry(kind="filename", label="自定义词", query="ab", editable=True)],
        SubtitleSource.OPENSUBTITLES: [SourcePlanEntry(kind="id", label="媒体 ID", query="tt2320220", editable=False)],
        SubtitleSource.ASSRT: [
            SourcePlanEntry(kind="title", label="主标题", query="元气少女缘结神", editable=True),
            SourcePlanEntry(kind="fallback", label="英文名/原名", query="Kamisama Kiss", editable=True),
        ],
    }
    calls: list[tuple[SubtitleSource, SubtitleTarget]] = []

    def default_queries(source: SubtitleSource, host_context: SubtitleTarget) -> tuple[SourcePlanEntry, ...]:
        """记录来源并返回预置结构化计划。"""

        calls.append((source, host_context))
        return tuple(entries[source])

    class _ManualSearch:
        """返回固定人工搜索领域结果。"""

        async def search(
            self,
            history_id: int,
            custom_queries: dict[SubtitleSource, str | None],
        ) -> ManualSearchResult:
            """返回仅含目标的人工搜索结果。"""

            assert history_id == 3
            return ManualSearchResult(session_id=None, target=target, sources=[])

    plugin = _ApiPlugin()
    plugin.manual_search = _ManualSearch()
    plugin.source_service = SimpleNamespace(
        statuses=AsyncMock(return_value=[]),
        default_queries=default_queries,
    )

    response = await _controller(plugin).search_subtitles(
        ManualSearchRequest(target_history_id=3),
        _=object(),
    )

    plans = response.target.search_plans
    assert [plan.query for plan in plans[SubtitleSource.MOVIEPILOT]] == ["ab"]
    assert plans[SubtitleSource.MOVIEPILOT][0].kind == "filename"
    assert plans[SubtitleSource.OPENSUBTITLES][0].kind == "id"
    assert [plan.query for plan in plans[SubtitleSource.ASSRT]] == ["元气少女缘结神", "Kamisama Kiss"]
    assert {source for source, _ in calls} == set(SubtitleSource)
    assert all(host_context is context for _, host_context in calls)


def test_batch_retarget_requests_reject_more_than_one_hundred_mappings() -> None:
    """批量改配预览与提交请求都限制为一百条映射。"""

    preview_items = [{"record_id": f"record-{index}"} for index in range(101)]
    submit_items = [{"record_id": f"record-{index}", "target_history_id": index + 1} for index in range(101)]

    with pytest.raises(ValidationError):
        BatchRetargetPreviewRequest.model_validate({"items": preview_items})
    with pytest.raises(ValidationError):
        BatchRetargetSubmitRequest.model_validate({"items": submit_items})


@pytest.mark.anyio
async def test_batch_retarget_preview_returns_safe_per_item_results() -> None:
    """批量预览把自动建议、当前路径与逐项错误转换为 200 响应。"""

    target = _retarget_target()
    preview = RetargetPreview(
        target_history_id=target.history_id,
        history_target_path=target.context.target_path,
        target_path="/media/target-7.mkv",
        final_subtitle_path="/media/target-7.chi.zh-cn.srt",
        directory_available=True,
    )
    plugin = _ApiPlugin()
    plugin.record_maintenance.preview_result = DomainBatchRetargetPreview(
        items=[
            DomainBatchRetargetPreviewItem(
                record_id="record-one",
                current_subtitle_path=Path("/media/old-one.srt"),
                target_history_id=target.history_id,
                target=target,
                preview=preview,
            ),
            DomainBatchRetargetPreviewItem(
                record_id="record-two",
                current_subtitle_path=Path("staged/record-two.srt"),
                error_code="target_required",
                message="无法唯一确定整理历史目标，请手动选择",
            ),
        ]
    )
    payload = BatchRetargetPreviewRequest.model_validate(
        {"items": [{"record_id": "record-one"}, {"record_id": "record-two"}]}
    )

    response = await _controller(plugin).preview_batch_retarget_records(payload, _=object())

    assert response.executable is False
    assert response.items[0].current_subtitle_path == "/media/old-one.srt"
    assert response.items[0].target is not None
    assert response.items[0].target.history_id == target.history_id
    assert response.items[0].preview is not None
    assert response.items[0].preview.final_subtitle_path == preview.final_subtitle_path
    assert response.items[0].preview.target_path == preview.target_path
    assert response.items[1].error_code == "target_required"
    assert [item.record_id for item in plugin.record_maintenance.preview_calls[0]] == [
        "record-one",
        "record-two",
    ]
    assert all(item.target_history_id is None for item in plugin.record_maintenance.preview_calls[0])


@pytest.mark.anyio
async def test_batch_retarget_submit_returns_409_with_full_preflight() -> None:
    """批量提交预检失败返回完整 409 结果且不产生执行项。"""

    plugin = _ApiPlugin()
    preflight = DomainBatchRetargetPreview(
        items=[
            DomainBatchRetargetPreviewItem(
                record_id="record-one",
                current_subtitle_path=Path("/media/old-one.srt"),
                target_history_id=7,
                error_code="destination_conflict",
                message="预计最终字幕路径已存在",
            )
        ]
    )
    plugin.record_maintenance.batch_result = DomainBatchRetargetResult(
        preflight=preflight,
        items=[],
        started=False,
    )
    payload = BatchRetargetSubmitRequest.model_validate(
        {"items": [{"record_id": "record-one", "target_history_id": 7}]}
    )

    with pytest.raises(HTTPException) as exc_info:
        await _controller(plugin).retarget_batch_records(payload, _=object())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "batch_preflight_failed"
    assert exc_info.value.detail["executable"] is False
    assert exc_info.value.detail["items"][0]["current_subtitle_path"] == "/media/old-one.srt"
    assert exc_info.value.detail["items"][0]["error_code"] == "destination_conflict"
    assert len(plugin.record_maintenance.batch_calls) == 1


@pytest.mark.anyio
async def test_batch_retarget_submit_returns_partial_success_results() -> None:
    """批量执行开始后把成功与失败项共同转换为 200 响应。"""

    success_record = _record(
        "record-one",
        RecordStatus.MATCHED,
        datetime(2026, 7, 24, tzinfo=UTC),
        path="/media/record-one.chi.zh-cn.srt",
    )
    plugin = _ApiPlugin()
    plugin.record_maintenance.batch_result = DomainBatchRetargetResult(
        preflight=DomainBatchRetargetPreview(items=[]),
        items=[
            DomainBatchRetargetResultItem(
                record_id="record-one",
                target_history_id=7,
                result=RetargetResult(record=success_record),
            ),
            DomainBatchRetargetResultItem(
                record_id="record-two",
                target_history_id=8,
                result=RetargetResult(
                    error_code="retarget_failed",
                    message="改配目标失败",
                ),
            ),
        ],
        started=True,
    )
    payload = BatchRetargetSubmitRequest.model_validate(
        {
            "items": [
                {"record_id": "record-one", "target_history_id": 7},
                {"record_id": "record-two", "target_history_id": 8},
            ]
        }
    )

    response = await _controller(plugin).retarget_batch_records(payload, _=object())

    assert response.success_count == 1
    assert response.failure_count == 1
    assert response.items[0].success is True
    assert response.items[0].record is not None
    assert response.items[0].record.id == "record-one"
    assert response.items[1].success is False
    assert response.items[1].error_code == "retarget_failed"
    assert response.items[1].consistency_risk is False


@pytest.mark.anyio
async def test_task_page_size_query_parses_allowed_integer_values() -> None:
    """真实 HTTP 查询可解析允许的分页数值并拒绝其他值。"""

    controller = _controller(_ApiPlugin())
    app = FastAPI()
    app.get("/tasks")(controller.list_tasks)
    app.dependency_overrides[get_current_active_manage_user_async] = lambda: object()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        allowed = await client.get("/tasks", params={"page_size": 25})
        rejected = await client.get("/tasks", params={"page_size": 30})

    assert allowed.status_code == 200
    assert allowed.json()["page_size"] == 25
    assert rejected.status_code == 422


@pytest.mark.anyio
async def test_task_listing_applies_fixed_group_sort_search_status_and_pagination() -> None:
    """任务列表按固定分组排序，并在分页前完成全文和状态筛选。"""

    base = datetime(2025, 1, 1, tzinfo=UTC)
    sorted_tasks = [
        _task("processing-late", TaskStatus.PROCESSING, base, started_at=base + timedelta(minutes=2)),
        _task("terminal-old", TaskStatus.FAILED, base, finished_at=base + timedelta(minutes=3)),
        _task("queued-late", TaskStatus.QUEUED, base + timedelta(minutes=2)),
        _task("processing-early", TaskStatus.PROCESSING, base, started_at=base + timedelta(minutes=1)),
        _task("terminal-new", TaskStatus.SUCCESS, base, finished_at=base + timedelta(minutes=4)),
        _task("queued-early", TaskStatus.QUEUED, base + timedelta(minutes=1)),
    ]
    plugin = _ApiPlugin(tasks=sorted_tasks)
    controller = _controller(plugin)

    page = await controller.list_tasks(page=1, page_size=25, search=None, status=None, _=object())
    assert [item.id for item in page.items] == [
        "processing-early",
        "processing-late",
        "queued-early",
        "queued-late",
        "terminal-new",
        "terminal-old",
    ]

    searchable = _task(
        "searchable",
        TaskStatus.FAILED,
        base,
        finished_at=base,
        title="目标剧集",
        season=2,
        episode=3,
        reason="没有可用字幕",
    )
    plugin.store.tasks[searchable.id] = searchable
    filtered = await controller.list_tasks(
        page=1,
        page_size=25,
        search="s02e03",
        status=TaskStatus.FAILED,
        _=object(),
    )
    assert [item.id for item in filtered.items] == ["searchable"]

    pagination_tasks = [
        _task(f"page-{index:02d}", TaskStatus.QUEUED, base + timedelta(minutes=index)) for index in range(27)
    ]
    plugin.store.tasks = {item.id: item for item in pagination_tasks}
    second_page = await controller.list_tasks(
        page=2,
        page_size=25,
        search=None,
        status=None,
        _=object(),
    )
    assert second_page.total == 27
    assert second_page.page == 2
    assert second_page.page_size == 25
    assert [item.id for item in second_page.items] == ["page-25", "page-26"]


@pytest.mark.anyio
async def test_record_listing_sorts_updated_descending_then_filters_and_paginates() -> None:
    """匹配记录按更新时间倒序，并在分页前应用全文和状态筛选。"""

    base = datetime(2025, 1, 1, tzinfo=UTC)
    records = [
        _record(f"record-{index:02d}", RecordStatus.STAGED, base + timedelta(minutes=index)) for index in range(27)
    ]
    plugin = _ApiPlugin(records=records)
    controller = _controller(plugin)

    second_page = await controller.list_records(
        page=2,
        page_size=25,
        search=None,
        status=None,
        _=object(),
    )
    assert second_page.total == 27
    assert [item.id for item in second_page.items] == ["record-01", "record-00"]
    assert second_page.items[0].current_file_path == "/plugin-data/staged/record-01.srt"

    matched = _record(
        "matched-search",
        RecordStatus.MATCHED,
        base + timedelta(days=1),
        title="特定影片",
        path="/media/special/字幕.srt",
    )
    plugin.store.records[matched.id] = matched
    filtered = await controller.list_records(
        page=1,
        page_size=25,
        search="SPECIAL",
        status=RecordStatus.MATCHED,
        _=object(),
    )
    assert [item.id for item in filtered.items] == ["matched-search"]

    plugin_path_filtered = await controller.list_records(
        page=1,
        page_size=25,
        search="/PLUGIN-DATA/STAGED/RECORD-01.SRT",
        status=RecordStatus.STAGED,
        _=object(),
    )
    assert [item.id for item in plugin_path_filtered.items] == ["record-01"]


@pytest.mark.anyio
async def test_delete_endpoints_require_confirmation_and_allow_matched_record_deletion() -> None:
    """删除接口要求确认快照，并允许已匹配记录按选择删除。"""

    base = datetime(2025, 1, 1, tzinfo=UTC)
    active = _task("active", TaskStatus.PROCESSING, base, started_at=base)
    matched = _record("matched", RecordStatus.MATCHED, base)
    plugin = _ApiPlugin(tasks=[active], records=[matched])
    controller = _controller(plugin)

    with pytest.raises(HTTPException) as missing_task:
        await controller.delete_task("missing", _=object())
    assert missing_task.value.status_code == 404

    with pytest.raises(HTTPException) as active_task:
        await controller.delete_task(active.id, _=object())
    assert active_task.value.status_code == 409

    with pytest.raises(HTTPException) as missing_record:
        await controller.delete_record("missing", _=object(), payload=_delete_payload(matched))
    assert missing_record.value.status_code == 404

    response = await controller.delete_record(
        matched.id,
        _=object(),
        payload=_delete_payload(matched, "record_only"),
    )
    assert response.success
    assert matched.id not in plugin.store.records


@pytest.mark.anyio
async def test_delete_endpoint_rejects_record_only_for_plugin_data_records() -> None:
    """单条暂存记录不能只删除元数据而留下插件数据文件。"""

    staged = _record(
        "staged",
        RecordStatus.STAGED,
        datetime(2025, 1, 1, tzinfo=UTC),
        path="staged/staged.srt",
    )
    plugin = _ApiPlugin(records=[staged])

    with pytest.raises(HTTPException) as exc_info:
        await _controller(plugin).delete_record(
            staged.id,
            _=object(),
            payload=_delete_payload(staged, "record_only"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "delete_mode_not_allowed"
    assert staged.id in plugin.store.records
    assert plugin.calls == []


@pytest.mark.anyio
async def test_batch_delete_endpoint_executes_all_preflighted_matched_records() -> None:
    """专用批量删除端点返回逐条成功结果，不通过单条接口循环执行。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    first = _record("first", RecordStatus.MATCHED, now, path="/media/first.srt")
    second = _record("second", RecordStatus.MATCHED, now, path="/media/second.srt")
    plugin = _ApiPlugin(records=[first, second])

    response = await _controller(plugin).delete_records_batch(
        _batch_delete_payload([first, second], "record_only"),
        _=object(),
    )

    assert response.success_count == 2
    assert response.failure_count == 0
    assert response.not_executed_count == 0
    assert [item.status for item in response.items] == ["success", "success"]
    assert plugin.calls == ["store.delete_record:first", "store.delete_record:second"]
    assert not plugin.store.records


@pytest.mark.anyio
async def test_batch_delete_endpoint_rejects_entire_stale_or_ineligible_batch() -> None:
    """批量确认过期或包含暂存仅删记录时返回 409 且零执行。"""

    now = datetime(2025, 1, 1, tzinfo=UTC)
    matched = _record("matched", RecordStatus.MATCHED, now, path="/media/matched.srt")
    staged = _record("staged", RecordStatus.STAGED, now, path="staged/staged.srt")
    plugin = _ApiPlugin(records=[matched, staged])
    payload = _batch_delete_payload([matched, staged], "record_only")

    with pytest.raises(HTTPException) as exc_info:
        await _controller(plugin).delete_records_batch(payload, _=object())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "batch_preflight_failed"
    assert [item["error_code"] for item in exc_info.value.detail["items"]] == [
        None,
        "delete_mode_not_allowed",
    ]
    assert plugin.calls == []
    assert set(plugin.store.records) == {matched.id, staged.id}


@pytest.mark.anyio
async def test_delete_staged_record_removes_file_before_inventory_and_metadata() -> None:
    """暂存记录严格按文件、库存、元数据顺序删除。"""

    staged = _record(
        "staged",
        RecordStatus.STAGED,
        datetime(2025, 1, 1, tzinfo=UTC),
        path="staged/staged.srt",
    )
    plugin = _ApiPlugin(records=[staged])

    response = await _controller(plugin).delete_record(
        staged.id,
        _=object(),
        payload=_delete_payload(staged),
    )

    assert response.success
    assert plugin.calls == [
        "filesystem.stage:/plugin-data/staged/staged.srt",
        "store.delete_record:staged",
        "filesystem.commit:None",
    ]
    assert staged.id not in plugin.store.records


@pytest.mark.anyio
async def test_delete_record_file_failure_returns_500_and_preserves_metadata() -> None:
    """插件文件删除失败返回 500，且不移除库存或记录元数据。"""

    unmatched = _record(
        "unmatched",
        RecordStatus.UNMATCHED,
        datetime(2025, 1, 1, tzinfo=UTC),
    )
    plugin = _ApiPlugin(records=[unmatched])
    plugin.filesystem.error = OSError("disk error")

    with pytest.raises(HTTPException) as exc_info:
        await _controller(plugin).delete_record(
            unmatched.id,
            _=object(),
            payload=_delete_payload(unmatched),
        )

    assert exc_info.value.status_code == 500
    assert plugin.calls == ["filesystem.stage:/plugin-data/unmatched/unmatched.srt"]
    assert unmatched.id in plugin.store.records


def test_credential_payload_requires_known_nonempty_bounded_fields() -> None:
    """凭据模型拒绝全空、未知和超长字段，并清理有效值空白。"""

    for payload in ({}, {"token": "   "}, {"api_key": "key", "unknown": "secret"}):
        with pytest.raises(ValidationError):
            CredentialUpdate.model_validate(payload)
    with pytest.raises(ValidationError):
        CredentialUpdate(token="x" * 2049)

    payload = CredentialUpdate(api_key="  key  ", username=None, password=" password ")
    assert payload.cleaned() == {"api_key": "key", "password": "password"}


@pytest.mark.parametrize("request_model", [ManualSearchRequest, RetargetRequest])
def test_history_target_requests_reject_non_numeric_ids(request_model: type[Any]) -> None:
    """整理历史 ID 接受数字字符串并在请求校验层拒绝其他字符串。"""

    assert request_model(target_history_id="42").target_history_id == 42
    with pytest.raises(ValidationError):
        request_model(target_history_id="not-a-number")


def test_manual_source_status_preserves_partial_result() -> None:
    """人工来源 API 区分部分完成与限流。"""

    assert ManualSourceResult.model_validate({"source": "assrt", "status": "success"}).status == "success"
    assert ManualSourceResult.model_validate({"source": "assrt", "status": "partial"}).status == "partial"


def test_stop_service_does_not_clear_host_backed_manual_search_sessions() -> None:
    """插件停止不应无条件清空宿主可能共享的人工搜索会话缓存。"""

    # 宿主基类构造需要完整 Chain 运行上下文，单测不装配组合根，
    # 用 __new__ 绕过基类 __init__，仅初始化本用例触及的运行态。
    plugin = object.__new__(SubtitleAssistant)
    plugin._runtime = None
    manual_search = SimpleNamespace(clear_sessions=AsyncMock())
    plugin.manual_search = manual_search

    plugin.stop_service()

    manual_search.clear_sessions.assert_not_awaited()


@pytest.mark.anyio
async def test_manual_search_api_requires_recognition_status_without_internal_evidence() -> None:
    """人工候选 API 必填识别状态且不暴露内部识别或下载信息。"""

    target = _retarget_target()
    recognition = CandidateRecognition(
        candidate=SubtitleCandidate(
            candidate_key="assrt:42",
            source=SubtitleSource.ASSRT,
            name="目标影片字幕",
            file_name="target.srt",
            language="zh-CN",
            metadata={
                "native_name": "安全来源摘要",
                "recognition_reason": "不得公开",
                "opaque": "secret-handle",
            },
        ),
        status=CandidateRecognitionStatus.RECOGNIZED,
    )

    class _ManualSearch:
        """返回固定人工搜索响应。"""

        async def search(
            self,
            history_id: int,
            custom_queries: dict[SubtitleSource, str | None],
        ) -> ManualSearchResult:
            """返回包含一个已识别候选的安全结果。"""

            assert history_id == 7
            assert set(custom_queries) == set(SubtitleSource)
            return ManualSearchResult(
                session_id="session-1",
                target=target,
                sources=[
                    ManualSourceView(
                        run=SourceSearchResult(source=SubtitleSource.ASSRT, status="success"),
                        candidates=[recognition],
                    )
                ],
            )

    plugin = _ApiPlugin()
    search = _ManualSearch()
    plugin.manual_search = search
    plugin.source_service = SimpleNamespace(
        statuses=AsyncMock(return_value=[]),
        default_queries=lambda source, context: (),
    )

    response = await _controller(plugin).search_subtitles(
        ManualSearchRequest(target_history_id=7),
        _=object(),
    )

    payload = response.model_dump(mode="json")
    candidate = payload["sources"][0]["candidates"][0]
    assert candidate["recognition_status"] == "recognized"
    assert "format" not in candidate
    assert "hearing_impaired" not in candidate
    assert set(candidate) == {
        "candidate_key",
        "recognition_status",
        "source",
        "name",
        "file_name",
        "language",
        "package_scope",
        "season",
        "episode",
        "seasons",
        "episodes",
        "translation_type",
    }
    assert "recognition_reason" not in str(payload)
    assert "secret-handle" not in str(payload)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("submit_result", "status_code", "detail"),
    [
        (
            ManualSubmitResult(status="session_not_found"),
            404,
            {"code": "manual_search_session_expired", "message": "搜索会话已失效，请重新搜索"},
        ),
        (
            ManualSubmitResult(status="candidate_not_found"),
            404,
            {"code": "manual_search_candidate_unavailable", "message": "当前候选不可用，请选择其他候选"},
        ),
        (ManualSubmitResult(status="rejected"), 409, "插件当前不接受新任务"),
    ],
)
async def test_manual_download_api_maps_submit_domain_failures(
    submit_result: ManualSubmitResult,
    status_code: int,
    detail: object,
) -> None:
    """人工下载 API 只把提交领域失败映射为稳定 HTTP 状态。"""

    class _ManualSearch:
        """返回固定提交领域结果。"""

        async def submit(self, session_id: str, candidate_key: str) -> ManualSubmitResult:
            """记录请求并返回固定结果。"""

            assert session_id == "session-1"
            assert candidate_key == "candidate-1"
            return submit_result

    plugin = _ApiPlugin()
    plugin.manual_search = _ManualSearch()

    with pytest.raises(HTTPException) as exc_info:
        await _controller(plugin).download_search_candidate(
            "session-1",
            ManualDownloadRequest(candidate_key="candidate-1"),
            _=object(),
        )

    assert exc_info.value.status_code == status_code
    assert exc_info.value.detail == detail


@pytest.mark.anyio
async def test_manual_download_api_returns_coordinator_snapshot_without_store_readback() -> None:
    """人工下载 API 直接返回提交结果中的任务快照，不依赖协调器或 store 回读。"""

    task = _task("submitted", TaskStatus.QUEUED, datetime.now(UTC))

    class _ManualSearch:
        """返回任务协调器已持久化的任务快照。"""

        async def submit(self, session_id: str, candidate_key: str) -> ManualSubmitResult:
            """返回成功提交结果。"""

            assert session_id == "session-1"
            assert candidate_key == "candidate-1"
            return ManualSubmitResult(status="success", task=task)

    plugin = _ApiPlugin()
    plugin.manual_search = _ManualSearch()

    response = await _controller(plugin).download_search_candidate(
        "session-1",
        ManualDownloadRequest(candidate_key="candidate-1"),
        _=object(),
    )

    assert response.task_id == "submitted"
    assert response.task.id == "submitted"
    assert plugin.store.calls == []


@pytest.mark.anyio
async def test_manual_download_api_maps_unexpected_submit_error_to_500() -> None:
    """人工下载 API 将未预期提交异常收敛为 500。"""

    class _ManualSearch:
        """模拟提交过程中的非预期异常。"""

        async def submit(self, session_id: str, candidate_key: str) -> ManualSubmitResult:
            """抛出未预期异常。"""

            del session_id, candidate_key
            raise RuntimeError("secret internal detail")

    plugin = _ApiPlugin()
    plugin.manual_search = _ManualSearch()

    with pytest.raises(HTTPException) as exc_info:
        await _controller(plugin).download_search_candidate(
            "session-1",
            ManualDownloadRequest(candidate_key="candidate-1"),
            _=object(),
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "人工字幕任务提交失败"


@pytest.mark.anyio
async def test_credential_endpoint_rejects_cross_source_fields_and_never_echoes_secret() -> None:
    """来源专属字段错误返回 422，有效更新只返回配置状态。"""

    plugin = _ApiPlugin()
    controller = _controller(plugin)

    with pytest.raises(HTTPException) as opensubtitles_error:
        await controller.update_credentials(
            "opensubtitles",
            CredentialUpdate(token="secret-token"),
            _=object(),
        )
    assert opensubtitles_error.value.status_code == 422

    with pytest.raises(HTTPException) as assrt_error:
        await controller.update_credentials(
            "assrt",
            CredentialUpdate(api_key="secret-key"),
            _=object(),
        )
    assert assrt_error.value.status_code == 422

    response = await controller.update_credentials(
        "opensubtitles",
        CredentialUpdate(api_key=" key ", username=" user "),
        _=object(),
    )
    assert response.success
    assert response.data == {"configured": True}
    assert plugin.credential_updates == [(SubtitleSource.OPENSUBTITLES, {"api_key": "key", "username": "user"})]
    assert "key" not in str(response.model_dump()).casefold()
