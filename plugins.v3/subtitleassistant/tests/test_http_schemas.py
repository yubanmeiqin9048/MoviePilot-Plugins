"""HTTP schema 所有者、投影边界与严格校验契约测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import ValidationError

from app.plugins.subtitleassistant.schemas.http import __all__ as http_exports
from app.plugins.subtitleassistant.schemas.http import page, record, search, source, target, task
from app.plugins.subtitleassistant.schemas.http.base import ApiModel
from app.plugins.subtitleassistant.schemas.record import MatchRecord

_TASK_LIST_FIELDS = {
    "id",
    "trigger",
    "media_title",
    "year",
    "media_type",
    "season",
    "episode",
    "target_file_name",
    "target_path",
    "target_history_id",
    "history_target_path",
    "status",
    "reason_code",
    "reason_message",
    "created_at",
    "started_at",
    "finished_at",
    "duration_ms",
}
_RECORD_LIST_FIELDS = {
    "id",
    "subtitle_file_name",
    "format",
    "size",
    "media_title",
    "year",
    "media_type",
    "season",
    "episode",
    "status",
    "source",
    "package_scope",
    "location",
    "path",
    "current_file_path",
    "target_history_id",
    "history_target_path",
    "target_path",
    "created_at",
    "updated_at",
    "consumed_at",
}


def test_all_http_model_field_shapes_are_explicitly_locked() -> None:
    """所有 HTTP 公共模型的字段集合与现有接口契约保持一致。"""

    expected = {
        task.TaskListItem: _TASK_LIST_FIELDS,
        task.TaskDetail: _TASK_LIST_FIELDS
        | {
            "tmdb_id",
            "imdb_id",
            "target_storage",
            "matched_path_mapping",
            "target_file_exists",
        },
        task.TaskPage: {"items", "total", "page", "page_size"},
        record.RecordListItem: _RECORD_LIST_FIELDS,
        record.RecordDetail: _RECORD_LIST_FIELDS
        | {
            "canonical_identity_type",
            "canonical_identity_value",
            "tmdb_id",
            "imdb_id",
            "matched_path_mapping",
            "target_file_exists",
            "final_subtitle_path",
            "source_task_id",
            "consumed_task_id",
            "candidate_key",
            "candidate_name",
            "logical_source_path",
            "file_attribution_method",
            "unmatched_reason",
            "language",
            "translation_type",
            "staged_at",
            "retarget_history",
        },
        record.RecordPage: {"items", "total", "page", "page_size"},
        record.RecordDeleteRequest: {
            "delete_mode",
            "expected_status",
            "expected_location",
            "expected_path",
            "expected_updated_at",
        },
        record.BatchRecordDeleteConfirmation: {
            "record_id",
            "expected_status",
            "expected_location",
            "expected_path",
            "expected_updated_at",
        },
        record.BatchRecordDeleteRequest: {"delete_mode", "items"},
        record.BatchRecordDeletePreflightItem: {"record_id", "executable", "error_code", "message"},
        record.BatchRecordDeleteResultItem: {
            "record_id",
            "status",
            "error_code",
            "message",
            "consistency_risk",
        },
        record.BatchRecordDeleteResponse: {"success_count", "failure_count", "not_executed_count", "items"},
        record.RetargetRequest: {"target_history_id"},
        record.RetargetPreviewResponse: {
            "target_history_id",
            "history_target_path",
            "target_path",
            "final_subtitle_path",
            "directory_available",
            "directory_error",
        },
        record.BatchRetargetPreviewMapping: {"record_id", "target_history_id"},
        record.BatchRetargetPreviewRequest: {"items"},
        record.BatchRetargetSubmitMapping: {"record_id", "target_history_id"},
        record.BatchRetargetSubmitRequest: {"items"},
        record.BatchRetargetPreviewItem: {
            "record_id",
            "current_subtitle_path",
            "target_history_id",
            "target",
            "preview",
            "executable",
            "error_code",
            "message",
        },
        record.BatchRetargetPreviewResponse: {"executable", "items"},
        record.BatchRetargetResultItem: {
            "record_id",
            "target_history_id",
            "success",
            "error_code",
            "message",
            "consistency_risk",
            "record",
        },
        record.BatchRetargetResponse: {"success_count", "failure_count", "items"},
        search.SearchPlanItem: {"kind", "label", "query", "editable"},
        search.ManualSearchRequest: {
            "target_history_id",
            "moviepilot_keyword",
            "opensubtitles_keyword",
            "assrt_keyword",
        },
        search.ManualCandidateItem: {
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
        },
        search.ManualSourceResult: {
            "source",
            "status",
            "default_plans",
            "matched_query",
            "candidate_count",
            "cache_hit",
            "duration_ms",
            "error_code",
            "error_summary",
            "retry_after_seconds",
            "candidates",
        },
        search.ManualSearchResponse: {"session_id", "target", "sources"},
        search.ManualDownloadRequest: {"candidate_key"},
        search.ManualDownloadResponse: {"task_id", "task"},
        target.TargetListItem: {
            "history_id",
            "media_title",
            "year",
            "media_type",
            "season",
            "episode",
            "tmdb_id",
            "imdb_id",
            "target_file_name",
            "target_path",
            "organized_at",
            "search_plans",
        },
        target.TargetPage: {"items", "page", "page_size", "total"},
        source.SourceStatusItem: {
            "source",
            "enabled",
            "configured",
            "health",
            "last_checked_at",
            "last_success_at",
            "last_error_at",
            "last_error_summary",
            "last_duration_ms",
            "details",
        },
        source.CredentialUpdate: {"api_key", "username", "password", "token"},
    }

    assert len(expected) == 33
    for model, fields in expected.items():
        assert set(model.model_fields) == fields, model.__name__


def test_http_leaf_modules_declare_only_their_owned_public_models() -> None:
    """六个 HTTP 叶 module 显式声明精确公开集合，根入口不扁平导出。"""

    assert http_exports == []
    assert page.__all__ == ["PageSize"]
    assert task.__all__ == ["TaskDetail", "TaskListItem", "TaskPage"]
    assert record.__all__ == [
        "BatchRecordDeleteConfirmation",
        "BatchRecordDeletePreflightItem",
        "BatchRecordDeleteRequest",
        "BatchRecordDeleteResponse",
        "BatchRecordDeleteResultItem",
        "BatchRetargetPreviewItem",
        "BatchRetargetPreviewMapping",
        "BatchRetargetPreviewRequest",
        "BatchRetargetPreviewResponse",
        "BatchRetargetResponse",
        "BatchRetargetResultItem",
        "BatchRetargetSubmitMapping",
        "BatchRetargetSubmitRequest",
        "RecordDeleteRequest",
        "RecordDetail",
        "RecordListItem",
        "RecordPage",
        "RetargetPreviewResponse",
        "RetargetRequest",
    ]
    assert search.__all__ == [
        "ManualCandidateItem",
        "ManualDownloadRequest",
        "ManualDownloadResponse",
        "ManualSearchRequest",
        "ManualSearchResponse",
        "ManualSourceResult",
        "SearchPlanItem",
    ]
    assert target.__all__ == ["TargetListItem", "TargetPage"]
    assert source.__all__ == ["CredentialUpdate", "SourceStatusItem"]
    assert "ApiModel" not in record.__all__
    assert not hasattr(record, "DeleteRecordRequest")


def test_search_plan_item_is_a_strict_http_model() -> None:
    """人工搜索计划项从 TypedDict 迁为严格 Pydantic 且保持字段形状。"""

    item = search.SearchPlanItem(
        kind="id",
        label="TMDB ID",
        query=None,
        editable=False,
    )

    assert isinstance(item, ApiModel)
    assert item.model_dump() == {
        "kind": "id",
        "label": "TMDB ID",
        "query": None,
        "editable": False,
    }
    with pytest.raises(ValidationError):
        search.SearchPlanItem(
            kind="id",
            label="TMDB ID",
            query=None,
            editable=False,
            internal_handle="never-expose",
        )


def test_http_models_are_independent_projections_with_no_attribute_expansion() -> None:
    """HTTP 模型不继承持久化模型，也不会把来源对象的额外字段带入响应。"""

    assert not issubclass(record.RecordListItem, MatchRecord)
    assert set(record.RecordListItem.model_fields) == {
        "id",
        "subtitle_file_name",
        "format",
        "size",
        "media_title",
        "year",
        "media_type",
        "season",
        "episode",
        "status",
        "source",
        "package_scope",
        "location",
        "path",
        "current_file_path",
        "target_history_id",
        "history_target_path",
        "target_path",
        "created_at",
        "updated_at",
        "consumed_at",
    }

    response = source.SourceStatusItem.model_validate(
        SimpleNamespace(
            source="assrt",
            enabled=True,
            configured=True,
            health="healthy",
            last_checked_at=datetime(2026, 8, 18, tzinfo=UTC),
            last_success_at=None,
            last_error_at=None,
            last_error_summary=None,
            last_duration_ms=12,
            details={"region": "cn"},
            api_key="must-not-leak",
        )
    )

    assert "api_key" not in response.model_dump()


def test_record_delete_and_manual_search_requests_keep_explicit_validation_rules() -> None:
    """删除确认、批量边界与人工搜索历史 ID 继续由 HTTP 所有者校验。"""

    payload = record.RecordDeleteRequest.model_validate(
        {
            "delete_mode": "record_and_file",
            "expected_status": "staged",
            "expected_location": "plugin_data",
            "expected_path": "staged/example.srt",
            "expected_updated_at": "2026-08-18T00:00:00Z",
        }
    )
    assert payload.expected_path == "staged/example.srt"
    assert payload.expected_updated_at.tzinfo is UTC

    item = {
        "record_id": "record-1",
        "expected_status": "matched",
        "expected_location": "media_directory",
        "expected_path": "/media/example.srt",
        "expected_updated_at": "2026-08-18T00:00:00Z",
    }
    with pytest.raises(ValidationError):
        record.BatchRecordDeleteRequest.model_validate({"delete_mode": "record_only", "items": [item, item]})

    assert search.ManualSearchRequest(target_history_id="42").target_history_id == 42
    with pytest.raises(ValidationError):
        search.ManualSearchRequest(target_history_id="not-a-number")


def test_search_source_and_credential_defaults_are_safe() -> None:
    """人工搜索与来源模型保留安全默认值，凭据只接受非空更新字段。"""

    result = search.ManualSourceResult.model_validate({"source": "assrt", "status": "success"})
    assert result.default_plans == []
    assert result.candidates == []
    assert result.matched_query is None
    assert result.cache_hit is False
    assert search.ManualSourceResult.model_validate({"source": "assrt", "status": "partial"}).status == "partial"

    update = source.CredentialUpdate(api_key="  placeholder  ")
    assert update.cleaned() == {"api_key": "placeholder"}
    with pytest.raises(ValidationError):
        source.CredentialUpdate.model_validate({"token": "   "})


def test_nested_record_projections_keep_defaults_and_constraints() -> None:
    """记录响应中的改配嵌套投影保留原默认值和校验边界。"""

    common = {
        "id": "record-1",
        "subtitle_file_name": "example.srt",
        "format": "srt",
        "size": 12,
        "media_title": "Example",
        "year": 2026,
        "media_type": "movie",
        "season": None,
        "episode": None,
        "status": "matched",
        "source": "assrt",
        "package_scope": "episode",
        "location": "media_directory",
        "path": "/media/example.srt",
        "target_history_id": None,
        "history_target_path": None,
        "target_path": "/media/example.mkv",
        "created_at": "2026-08-18T00:00:00Z",
        "updated_at": "2026-08-18T00:00:00Z",
        "consumed_at": None,
        "canonical_identity_type": "tmdb",
        "canonical_identity_value": "1",
        "tmdb_id": 1,
        "imdb_id": None,
        "matched_path_mapping": None,
        "target_file_exists": True,
        "final_subtitle_path": "/media/example.chi.zh-cn.srt",
        "source_task_id": "task-1",
        "consumed_task_id": None,
        "candidate_key": "candidate-1",
        "candidate_name": "Example subtitle",
        "logical_source_path": "example.srt",
        "file_attribution_method": "direct_file",
        "unmatched_reason": None,
        "language": "zh-CN",
        "translation_type": "human",
        "staged_at": None,
        "retarget_history": [
            {
                "new_target_path": "/media/new-example.mkv",
                "new_matched_path_mapping": {
                    "source_prefix": "/history",
                    "target_prefix": "/media",
                },
                "old_subtitle_path": "/media/example.srt",
                "new_subtitle_path": "/media/new-example.chi.zh-cn.srt",
            }
        ],
    }

    detail = record.RecordDetail.model_validate(common)
    history = detail.retarget_history[0]
    assert history.old_target_history_id is None
    assert history.new_matched_path_mapping is not None
    assert history.new_matched_path_mapping.source_prefix == "/history"
    assert history.new_matched_path_mapping.target_prefix == "/media"
    assert history.operated_at.tzinfo is UTC
    assert detail.unmatched_reason is None

    with pytest.raises(ValidationError):
        record.RecordDetail.model_validate(
            {
                **common,
                "retarget_history": [
                    {
                        "new_target_path": "/media/new-example.mkv",
                        "old_subtitle_path": "/media/example.srt",
                        "new_subtitle_path": "/media/new-example.chi.zh-cn.srt",
                        "opaque": object(),
                    }
                ],
            }
        )


def test_target_page_keeps_page_size_enum_and_raw_row_values() -> None:
    """整理历史页继续使用固定分页枚举，同时保留宿主原始行的开放值。"""

    page = target.TargetPage.model_validate({"items": [{"opaque": object()}], "page": 1, "page_size": 25, "total": 1})
    assert page.page_size.value == 25
    assert page.items[0]["opaque"] is not None
    with pytest.raises(ValidationError):
        target.TargetPage.model_validate({"items": [], "page": 1, "page_size": 30})


def test_manual_source_details_are_strictly_typed() -> None:
    """人工来源结果拒绝未声明的详情字段。"""

    with pytest.raises(ValidationError):
        search.ManualSourceResult.model_validate(
            {
                "source": "assrt",
                "status": "success",
                "details": {"cache": [{"query": "x", "state": "hit"}]},
            }
        )

    with pytest.raises(ValidationError):
        search.ManualCandidateItem.model_validate(
            {
                "candidate_key": "candidate-1",
                "recognition_status": "recognized",
                "source": "assrt",
                "name": "Example",
                "package_scope": "episode",
                "translation_type": "human",
                "source_details": {"opaque": object()},
            }
        )


@pytest.mark.parametrize("removed", [{"format": "SRT"}, {"hearing_impaired": False}])
def test_manual_candidate_rejects_removed_fields(removed: dict[str, object]) -> None:
    """搜索响应不保留格式和听障标识的兼容字段。"""

    payload = {
        "candidate_key": "candidate-1",
        "recognition_status": "recognized",
        "source": "assrt",
        "name": "Example.srt",
        "package_scope": "episode",
        "translation_type": "human",
    }
    assert search.ManualCandidateItem.model_validate(payload).name == "Example.srt"
    with pytest.raises(ValidationError):
        search.ManualCandidateItem.model_validate({**payload, **removed})


def test_combined_openapi_keeps_shared_nested_component_names() -> None:
    """所有 HTTP 模型组合注册时继续使用既有嵌套 OpenAPI component 名称。"""

    app = FastAPI()
    models = [
        task.TaskDetail,
        task.TaskPage,
        record.RecordDetail,
        record.RecordPage,
        search.ManualSearchResponse,
        target.TargetPage,
        source.SourceStatusItem,
    ]
    for index, model in enumerate(models):

        async def endpoint() -> None:
            return None

        endpoint.__name__ = f"schema_probe_{index}"
        app.get(f"/schema-probe/{index}", response_model=model)(endpoint)

    schema = get_openapi(title="schema probe", version="1", routes=app.routes)
    components = schema["components"]["schemas"]

    for name in ("PathMappingSnapshot", "SearchPlanItem"):
        assert name in components
        assert not any(key.endswith(f"__{name}") for key in components)
