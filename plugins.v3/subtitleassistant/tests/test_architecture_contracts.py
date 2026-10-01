"""能力优先目录、接口与依赖的永久静态契约。"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {
    "api",
    "attribution",
    "candidate",
    "config",
    "event",
    "file",
    "plugin",
    "record",
    "schemas",
    "search",
    "source",
    "store",
    "target",
    "task",
}
LEGACY_PACKAGES = {"application", "domain", "infrastructure", "sources"}
INTERFACES = {
    "candidate": {
        "REJECTION_REASON_NAMES",
        "admit_automatic_candidates",
        "candidate_from_record",
        "candidate_is_allowed",
        "candidate_rank",
        "describe_rejections",
        "has_exact_media_identity",
        "has_simplified_chinese",
        "normalize_format_priority",
        "normalized_imdb_id",
        "record_rank",
        "sort_candidates",
    },
    "source": {
        "SOURCE_NAMES",
        "SOURCE_SKIP_REASONS",
        "SourceAdministration",
        "describe_source_run",
        "source_run_is_warning",
    },
    "target": {
        "MediaResolution",
        "MediaResolver",
        "TargetCatalog",
        "build_media_context",
        "enrich_search_target",
        "match_context_from_history",
        "match_context_from_mediainfo",
        "target_from_history",
    },
    "attribution": {"AttributionService"},
    "record": {"RecordCatalog", "RecordCommitter", "RecordMaintenance"},
    "task": {"TaskOperations"},
    "search": {"ManualSearch"},
    "config": {"load_config", "public_config"},
    "file": {"ArchiveExtractor", "SubtitleFiles"},
    "store": {"PluginDataStore", "StoreInitializationError"},
    "event": {"SubtitleEvents"},
    "api": {"ApiController"},
    "plugin": {"PluginRuntime", "RuntimeInitializationError", "build_runtime"},
}
SCHEMA_EXPORTS = {
    "attribution": {
        "AttributionEvidence",
        "CandidateAttributionSnapshot",
        "CandidateMatchContext",
        "FileAttributionBatchResult",
        "FileAttributionEvidence",
        "FileAttributionMethod",
        "FileAttributionRequest",
        "PackageAttributionStrategy",
        "UnmatchedReason",
    },
    "candidate": {
        "CandidateRecognition",
        "CandidateRecognitionStatus",
        "PackageScope",
        "SubtitleCandidate",
        "TranslationType",
    },
    "config": {"PluginConfig"},
    "event": {"SubtitleWrittenEvent", "SubtitleWrittenOperation"},
    "file": {"ExtractedSubtitle"},
    "record": {
        "CommittedFileFact",
        "BatchDeletePreflight",
        "BatchDeletePreflightItem",
        "BatchDeleteRecordConfirmation",
        "BatchDeleteResult",
        "BatchDeleteResultItem",
        "BatchDeleteStatus",
        "BatchRetargetPreview",
        "BatchRetargetPreviewItem",
        "BatchRetargetResult",
        "BatchRetargetResultItem",
        "DeleteMode",
        "DeleteRecordConfirmation",
        "DeleteRecordResult",
        "FileLocation",
        "InventoryConsumeResult",
        "MatchRecord",
        "RecordStatus",
        "RetargetHistoryEntry",
        "RetargetMapping",
        "RetargetPreview",
        "RetargetResult",
    },
    "search": {
        "ManualSearchResult",
        "ManualSourceView",
        "ManualSubmitResult",
        "ManualSubmitStatus",
    },
    "source": {
        "AssrtDownloadHandle",
        "CandidateHandle",
        "DownloadedAsset",
        "MoviePilotDownloadHandle",
        "OpenSubtitlesDownloadHandle",
        "SourceDetails",
        "SourceErrorCode",
        "SourceHealth",
        "SourcePlanEntry",
        "SourceSearchBatch",
        "SourceSearchResult",
        "SourceSearchStatus",
        "SourceStatus",
        "SubtitleSource",
    },
    "target": {
        "MediaIdentityKind",
        "MediaType",
        "PathMapping",
        "PathMappingResolution",
        "PathMappingSnapshot",
        "ResolvedTarget",
        "SearchTarget",
        "SubtitleTarget",
    },
    "task": {
        "AttemptResult",
        "CandidateAttemptReasonCode",
        "SubtitleTask",
        "TaskStatus",
        "TaskTrigger",
        "TaskWorkItem",
    },
}
HTTP_SCHEMA_EXPORTS = {
    "page": {"PageSize"},
    "record": {
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
    },
    "search": {
        "ManualCandidateItem",
        "ManualDownloadRequest",
        "ManualDownloadResponse",
        "ManualSearchRequest",
        "ManualSearchResponse",
        "ManualSourceResult",
        "SearchPlanItem",
    },
    "source": {"CredentialUpdate", "SourceStatusItem"},
    "target": {"TargetListItem", "TargetPage"},
    "task": {"TaskDetail", "TaskListItem", "TaskPage"},
}
ALLOWLIST = {
    "schemas": set(),
    "config": {"schemas"},
    "candidate": {"schemas"},
    "file": {"schemas"},
    "store": {"schemas"},
    "event": {"schemas"},
    "source": {"schemas"},
    "target": {"schemas"},
    "attribution": {"schemas"},
    "record": {"schemas", "candidate", "file", "target"},
    "task": {"schemas", "candidate", "source", "target", "attribution", "record", "file"},
    "search": {"schemas", "source", "target", "attribution"},
    "api": {"schemas", "task", "record", "target", "search", "source"},
    "plugin": CAPABILITIES - {"plugin"},
}
STABLE_HOST_SDK_MODULES = {
    "app.core.cache",
    "app.core.config",
    "app.core.context",
    "app.core.event",
    "app.core.metainfo",
    "app.helper.sites",
    "app.log",
    "app.plugins",
    "app.utils.http",
}
HOST_CAPABILITIES = {"root", "plugin", "api", "store", "event", "source", "target", "attribution", "search"}
TEST_IMPLEMENTATION_EXCEPTIONS = {
    "tests/test_external_sources.py": {"source.assrt", "source.limiter", "source.opensubtitles"},
    "tests/test_manual_search.py": {"search.service"},
    "tests/test_matching.py": {"attribution.matching"},
    "tests/test_moviepilot_source.py": {"source.base", "source.common", "source.moviepilot"},
    "tests/test_path_mapping.py": {"target.mapping"},
    "tests/test_publication.py": {"event.publication"},
    "tests/test_source_base.py": {"source.base", "source.common"},
    "tests/test_tasks.py": {"task.service"},
    "tests/test_runtime.py": {"plugin.runtime"},
}


def _files(directory: Path) -> Iterable[Path]:
    """返回目录中的 Python 文件，忽略缓存与临时资料。"""

    excluded = {".scratch", ".ruff_cache", "frontend", "__pycache__"}
    return (path for path in directory.rglob("*.py") if not (set(path.relative_to(ROOT).parts) & excluded))


def _exports(path: Path) -> list[str]:
    """从 AST 读取字面量 ``__all__``。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            assert isinstance(value, ast.List), f"{path.relative_to(ROOT)} 的 __all__ 必须是字面量列表"
            return [ast.literal_eval(element) for element in value.elts]
    raise AssertionError(f"{path.relative_to(ROOT)} 未声明 __all__")


def _module_parts(path: Path, module: str | None, level: int) -> list[str] | None:
    """把相对或绝对 import 解析为插件根起的模块片段。"""

    if level:
        package = list(path.relative_to(ROOT).parent.parts)
        base = package[: len(package) - level + 1]
        return base + (module.split(".") if module else [])
    if module is None:
        return None
    parts = module.split(".")
    return parts[parts.index("subtitleassistant") + 1 :] if "subtitleassistant" in parts else None


def _imports(path: Path) -> list[tuple[list[str], bool]]:
    """返回文件中指向当前插件的 import 路径及其是否直达内部文件。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: list[tuple[list[str], bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = _module_parts(path, node.module, node.level)
            if parts:
                result.append((parts, len(parts) > 1))
                if len(parts) == 1 and parts[0] in CAPABILITIES:
                    result.extend(
                        (parts + [alias.name], True)
                        for alias in node.names
                        if (ROOT / parts[0] / f"{alias.name}.py").exists()
                    )
        elif isinstance(node, ast.Import):
            result.extend(
                (parts, len(parts) > 1) for alias in node.names if (parts := _module_parts(path, alias.name, 0))
            )
    return result


def test_runtime_topology_and_file_names_are_permanent() -> None:
    """目标能力目录完整，生产文件名保持单词形式，旧目录物理缺席。"""

    assert {path.name for path in ROOT.iterdir() if path.is_dir()} >= CAPABILITIES
    assert not any((ROOT / name).exists() for name in LEGACY_PACKAGES)
    for path in _files(ROOT):
        if path.relative_to(ROOT).parts[0] in {"tests", "scripts"}:
            continue
        assert path.name == "__init__.py" or re.fullmatch(r"[a-z]+\.py", path.name), path.relative_to(ROOT)


def test_package_interfaces_and_schema_owners_are_exact() -> None:
    """根、能力和 schema 所有者的公开集合保持精确。"""

    assert _exports(ROOT / "__init__.py") == ["SubtitleAssistant"]
    for package, expected in INTERFACES.items():
        assert set(_exports(ROOT / package / "__init__.py")) == expected
    assert _exports(ROOT / "schemas" / "__init__.py") == []
    assert _exports(ROOT / "schemas" / "http" / "__init__.py") == []
    for owner, expected in SCHEMA_EXPORTS.items():
        assert set(_exports(ROOT / "schemas" / f"{owner}.py")) == expected
    for owner, expected in HTTP_SCHEMA_EXPORTS.items():
        assert set(_exports(ROOT / "schemas" / "http" / f"{owner}.py")) == expected
    assert _exports(ROOT / "schemas" / "base.py") == []
    assert _exports(ROOT / "schemas" / "http" / "base.py") == []


def test_public_schemas_are_plugin_owned_and_removed_aliases_stay_absent() -> None:
    """公共 schema 不依赖宿主对象，也不重建废弃聚合或 Python 别名。"""

    assert not (ROOT / "schemas" / "model.py").exists()
    assert not (ROOT / "schemas" / "types.py").exists()
    schema_sources = "\n".join(path.read_text(encoding="utf-8") for path in _files(ROOT / "schemas"))
    assert "AiTakeoverAudit" not in schema_sources
    assert "DeleteRecordRequest" not in schema_sources
    for path in _files(ROOT / "schemas"):
        assert "app." not in path.read_text(encoding="utf-8"), path.relative_to(ROOT)


def test_capability_dependencies_use_interfaces_schema_owners_and_acyclic_allowlist() -> None:
    """生产跨能力依赖只使用公开接口或 schema 所有者，并严格遵循 DAG。"""

    edges: dict[str, set[str]] = {capability: set() for capability in CAPABILITIES}
    for path in _files(ROOT):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"tests", "scripts"}:
            continue
        current = relative.parts[0] if relative.parts[0] in CAPABILITIES else "root"
        for parts, direct_implementation in _imports(path):
            if not parts:
                continue
            target = parts[0]
            if target not in CAPABILITIES or target == current:
                continue
            if current == "root":
                assert target == "plugin", f"{relative} 根 package 只能依赖 plugin"
                continue
            assert target in ALLOWLIST[current], f"{relative} 不允许 {current} -> {target}"
            if target == "schemas":
                assert len(parts) >= 2 and parts[1] in {"http", "base", *SCHEMA_EXPORTS}, (
                    f"{relative} 必须导入 schema 所有者文件"
                )
            else:
                assert not direct_implementation, f"{relative} 必须通过 {target} package interface 导入"
            edges[current].add(target)

    visited: set[str] = set()
    active: set[str] = set()

    def visit(node: str) -> None:
        assert node not in active, f"capability DAG 存在环，回到 {node}"
        if node in visited:
            return
        active.add(node)
        for target in edges[node]:
            visit(target)
        active.remove(node)
        visited.add(node)

    for capability in CAPABILITIES:
        visit(capability)


def test_host_dependencies_only_appear_in_real_adapter_capabilities() -> None:
    """MoviePilot 依赖只能位于允许承载真实 adapter 的能力。"""

    def is_host_import(node: ast.Import | ast.ImportFrom) -> bool:
        """判断一条非 SDK import 是否指向 MoviePilot 的 app package。"""

        modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        return any(module == "app" or (module.startswith("app.") and not module.startswith("app.sdk")) for module in modules)


    for path in _files(ROOT):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"tests", "scripts"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        host_import = any(
            is_host_import(node) for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        )
        if not host_import:
            continue
        capability = relative.parts[0] if relative.parts[0] in CAPABILITIES else "root"
        assert capability in HOST_CAPABILITIES, relative


def test_stable_host_capabilities_use_sdk_contracts() -> None:
    """宿主已有稳定 SDK 能力必须通过 SDK 门面导入。"""

    for path in _files(ROOT):
        if path.relative_to(ROOT).parts[0] in {"tests", "scripts"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        assert not STABLE_HOST_SDK_MODULES.intersection(modules), path.relative_to(ROOT)


def test_legacy_imports_and_cross_capability_test_implementation_imports_are_absent() -> None:
    """生产、测试和脚本无旧 seam；测试私有实现访问只限同能力精确例外。"""

    for path in _files(ROOT):
        relative = path.relative_to(ROOT)
        for parts, direct_implementation in _imports(path):
            assert not LEGACY_PACKAGES.intersection(parts), f"{relative} 仍引用旧技术目录"
            if relative.parts[0] != "tests" or not parts or parts[0] not in CAPABILITIES:
                continue
            if parts[0] == "schemas" or not direct_implementation:
                continue
            target = ".".join(parts[:2])
            assert target in TEST_IMPLEMENTATION_EXCEPTIONS.get(str(relative), set()), (
                f"{relative} 未获准直达 {target} implementation"
            )


def _plugin_import_names(path: Path) -> set[str]:
    """返回文件从插件内导入的顶层符号名。"""

    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _module_parts(path, node.module, node.level):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _module_parts(path, alias.name, 0):
                    names.add(alias.name.split(".")[0])
    return names


def _defined_functions(path: Path) -> set[str]:
    """返回文件顶层与类内定义的方法名。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_manual_and_automatic_chains_share_search_prefix_and_fork_only_at_admission() -> None:
    """自动链与手动链共用目标投影、来源 facade 与来源结论文案，只在准入处分叉。"""

    automatic = ROOT / "task" / "service.py"
    manual = ROOT / "search" / "service.py"
    event_runtime = ROOT / "plugin" / "runtime.py"
    automatic_names = _plugin_import_names(automatic)
    manual_names = _plugin_import_names(manual)

    # 两链都经 target 包投影获得 match_context（自动为事件投影，手动为 enrich 投影）。
    assert {"build_media_context", "match_context_from_mediainfo"} <= _plugin_import_names(event_runtime)
    assert "enrich_search_target" in manual_names
    # 两链都经统一 source facade(SourceAdministration)查询。
    assert "SourceAdministration" in automatic_names
    assert "SourceAdministration" in manual_names
    # 两链都经统一的来源结论文案记录。
    assert "describe_source_run" in automatic_names
    assert "describe_source_run" in manual_names

    # 唯一准入分叉：自动链应用准入纯函数，手动链不应用。
    assert "admit_automatic_candidates" in automatic_names
    assert "admit_automatic_candidates" not in manual_names
    # 识别标注只在手动链，自动链不标注；排序只在自动链，手动链不排序。
    assert "AttributionService" in manual_names
    assert "candidate_rank" in automatic_names
    assert "candidate_rank" not in manual_names

    # search 零依赖 task 死边保持。
    assert "task" not in ALLOWLIST["search"]
    assert "task" not in {parts[0] for parts in _imported_capabilities(manual)}


def _imported_capabilities(path: Path) -> set[tuple[str, ...]]:
    """返回文件导入的跨插件限界模块路径。"""

    return {tuple(parts) for parts, _ in _imports(path) if parts and parts[0] in CAPABILITIES}


def test_api_layer_owns_search_http_projection_and_search_service_drops_it() -> None:
    """搜索 HTTP 投影归 api 层；人工搜索服务不再承载 target_item/source_item。"""

    router = ROOT / "api" / "router.py"
    projection = ROOT / "api" / "projection.py"
    manual = ROOT / "search" / "service.py"

    assert projection.exists()
    assert {"source_item", "target_item"} <= _plugin_import_names(router)
    assert {"source_item", "target_item"} <= _defined_functions(projection)

    manual_functions = _defined_functions(manual)
    assert "target_item" not in manual_functions
    assert "source_item" not in manual_functions
    # 目标查询计划不再由投影手工复刻，来源 facade 是唯一真源。
    assert "SourceAdministration" in _imported_names_from(projection, "source")
    assert "default_queries" in projection.read_text(encoding="utf-8")


def _imported_names_from(path: Path, capability: str) -> set[str]:
    """返回文件从指定插件能力导入的符号名。"""

    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            parts = _module_parts(path, node.module, node.level)
            if parts and parts[0] == capability:
                names.update(alias.name for alias in node.names)
    return names


def test_removed_compatibility_surfaces_are_absent_from_production() -> None:
    """旧来源与 AI 归属兼容入口不得重新成为可调用属性。"""

    production = [path for path in _files(ROOT) if path.relative_to(ROOT).parts[0] not in {"tests", "scripts"}]
    source_text = "\n".join(path.read_text(encoding="utf-8") for path in production)
    assert "SourceExecution" not in source_text
    assert "def fetch_page(" not in source_text
    assert "is_valid_download_locator" not in source_text
    assert "AiTakeoverAdapter" not in source_text
    assert "AiAttributionAdapter" not in source_text
    assert "AiAttributionAudit" not in source_text
    assert "attribute_files" not in source_text
    assert not (ROOT / "attribution" / "ai.py").exists()

    # `ai_takeover` 仅允许作为旧数据一次性迁移的枚举值白名单存在（数据而非兼容入口）。
    legacy_value_owners = {"store/storage.py"}
    ai_takeover_owners = {
        path.relative_to(ROOT).as_posix()
        for path in production
        if "ai_takeover" in path.read_text(encoding="utf-8")
    }
    assert ai_takeover_owners <= legacy_value_owners, ai_takeover_owners

    removed_aliases = {
        "_PathMappingSnapshot",
        "_CandidateAttributionSnapshot",
        "_AiAttributionAudit",
        "_RetargetHistoryEntry",
        "_StageTrace",
        "_SourceRun",
        "_CandidateAttempt",
        "_SourceCandidatePoolResult",
        "_CandidatePoolQueryBatchResult",
    }
    for path in production:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id in removed_aliases
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
            for node in ast.walk(tree)
        ), path
