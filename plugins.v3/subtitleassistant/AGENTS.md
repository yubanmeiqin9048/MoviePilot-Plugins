# 字幕助手项目规范

字幕助手是 MoviePilot 的独立插件项目，负责字幕全生命周期管理。本文件约束该插件的资料读取、实现、测试和交付。

## 范围与资料

目录约定：MoviePilot 宿主和 MoviePilot 前端放在插件主仓库根目录的同级目录，目录名分别为 `MoviePilot` 和 `MoviePilot-Frontend`。宿主虚拟环境放在 `MoviePilot/.venv/`：

```text
开发目录/
├── MoviePilot-Plugins/          # 插件主仓库
│   └── plugins.v3/subtitleassistant/    # 插件开发目录
├── MoviePilot/                  # 宿主源码
│   └── .venv/                   # Python 虚拟环境
└── MoviePilot-Frontend/         # 宿主前端源码
```

已有外部仓库位于其他位置时，可在约定位置创建目录符号链接。读取资料或执行检查时，只验证当前任务需要的外部目录；缺失时说明受影响的操作。

在目标插件 worktree 内按以下命令解析路径。插件目录以当前 worktree 为准；外部依赖以主仓库为准，通过 Git 公共目录定位，任意位置的 linked worktree 共用同一组外部依赖：

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
MAIN_REPO_ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
WORKSPACE_ROOT="$(dirname "${MAIN_REPO_ROOT}")"
PLUGIN_ROOT="${REPO_ROOT}/plugins.v3/subtitleassistant"
MOVIEPILOT_ROOT="${WORKSPACE_ROOT}/MoviePilot"
MOVIEPILOT_FRONTEND_ROOT="${WORKSPACE_ROOT}/MoviePilot-Frontend"
MOVIEPILOT_VENV="${MOVIEPILOT_ROOT}/.venv"
export MOVIEPILOT_ROOT
```

开始实现或修复时，先读取插件仓库内相关规范、领域文档、ADR 和当前任务资料。涉及宿主集成时再读取 MoviePilot 规则与源码；涉及 Vue 联邦渲染、侧栏入口或宿主加载时再读取 MoviePilot 前端资料。

## 仓库边界

插件目录由外层 `MoviePilot-Plugins` Git 仓库管理。提交、状态、diff、回滚和交付范围限定为当前 worktree 的 `plugins.v3/subtitleassistant/`；发布时按需更新根目录 `package.v3.json` 的 `SubtitleAssistant` 条目。MoviePilot 宿主和 MoviePilot 前端只作为依赖、测试入口或参考源码，不在其中落地或提交插件变更。

正式项目资料可以纳入插件版本控制；临时调研、未整理草稿、依赖、缓存、日志、本机配置和运行数据留在版本控制之外。修改已有文件时保留与当前任务无关的用户变更。

## Python 规范

Python 代码遵循 MoviePilot 与周边插件风格，如果有冲突，则以本规范为准。固定名称为插件类 `SubtitleAssistant`、插件目录 `subtitleassistant`；文件和符号使用标准 Python 命名，测试文件使用 `test_<domain>.py`，避免临时缩写。

公开类、方法和函数必须提供中文 docstring 与准确类型注解；私有接口存在非显然约束时也应写简短中文 docstring。可空与联合类型使用 `... | None` 和 `... | ...`，路径类型与纯路径运算使用 `pathlib.Path`。

异步边界：

- 网络请求、插件 API、事件处理和外部字幕源调用优先使用 async。
- I/O 优先使用 MoviePilot 依赖中已有的异步库；没有异步接口时才使用 `asyncio.to_thread` 包装同步调用。
- 同步路径操作通过 `from anyio import Path as AsyncPath` 转为异步调用，不使用 `asyncio.to_thread` 包装 `pathlib.Path` 操作。
- 纯内存操作保持同步。

内部服务按层次返回领域结果或记录日志，不抛 FastAPI HTTP 异常。日志使用中文；密钥、Token、密码、用户凭据和完整敏感配置不得进入日志、测试快照、文档示例或前端假数据。新增抽象必须有明确复用价值。

第三方 HTTP 请求统一通过 MoviePilot 的 `AsyncRequestUtils`。缓存使用 `app.sdk.cache`，缓存 key 由结构化字段组成。运行数据使用 `_PluginBase` 的 PluginData，不新增宿主数据库表或迁移；同步 PluginData 接口仅用于同步生命周期，异步业务直接等待异步接口，轮询和普通 getter 只读内存快照。

## 插件测试

本节仅约束插件开发目录 `tests/` 下的测试。主工作树与任意位置的 linked worktree 使用相同测试范围、隔离策略和断言，不因 worktree 环境跳过、放宽或改写测试。测试复用 MoviePilot 测试后端和真实网络守卫，不发起真实网络请求。

单元测试优先直接测试纯逻辑方法；需要插件或运行态实例时，优先使用 `object.__new__(SubtitleAssistant)` / `object.__new__(PluginRuntime)` 绕过 `__init__`，仅设置被测方法需要的状态，并在 I/O 边界注入内存替身。仅初始化、生命周期装配和宿主集成契约测试按需调用真实构造或启动独立进程；同一入口的测试收集与隔离探针共用一次进程启动，各进程使用独立临时配置目录。

所有 Python 执行与测试使用 `${MOVIEPILOT_VENV}/bin/python`；直接调用可执行文件，不依赖 shell 激活状态。`timeout`、`ty` 和 `ruff` 等独立工具从 `PATH` 查找，但分析目标必须使用该虚拟环境及其依赖。

按“范围与资料”解析并导出路径后，以插件的 `tests/` 同时作为 pytest 根目录和测试目标：

```bash
PYTHONPATH="${MOVIEPILOT_ROOT}" \
  timeout --signal=TERM 120s \
  "${MOVIEPILOT_VENV}/bin/python" -m pytest -q \
  --rootdir="${PLUGIN_ROOT}/tests" "${PLUGIN_ROOT}/tests"
```

解析并导出路径后，pytest 可以从任意目录启动。运行单个文件或节点时保留 `MOVIEPILOT_ROOT`、`PYTHONPATH` 和 `--rootdir`，只替换最后的测试目标。测试进程中的 `app.plugins.subtitleassistant` 必须来自当前 worktree，宿主模块必须来自 `${MOVIEPILOT_ROOT}`。

## 前端规范

前端源码位于 `frontend/`，构建产物位于 `frontend/dist/assets/`。界面使用 MoviePilot/Vuetify 既有习惯并服务实际管理任务。修改前端后运行 `yarn typecheck` 和 `yarn build`，同步提交更新后的构建产物；无法执行时在交付说明中报告原因和产物状态。

每个 worktree 使用独立的 `frontend/node_modules/`、`frontend/.vite/` 和 `frontend/dist/`。缺少依赖时，在该 worktree 的 `frontend/` 运行 `yarn install --frozen-lockfile --prefer-offline`，优先复用 Yarn 共享缓存；明确禁止联网时改用 `--offline`，缓存不完整则直接失败。worktree 不链接或修改其他工作树的依赖与产物。

## 交付门禁

提交前完成与变更范围对应的检查：

1. 确认插件状态只含本次变更，MoviePilot 宿主和 MoviePilot 前端未被污染。
2. 按“插件测试”执行相关测试；Python 变更还需在 `cd "${PLUGIN_ROOT}"` 后通过 `ty check --python "${MOVIEPILOT_VENV}" --extra-search-path "$(dirname "${PLUGIN_ROOT}")" --extra-search-path "${MOVIEPILOT_ROOT}"`、`ruff check .` 和 `ruff format --check .`。`ty` 仅以 error-level 诊断为门禁。
3. 前端变更按“前端规范”完成依赖安装、类型检查、构建和 worktree 隔离检查。
4. 最终说明准确列出已执行、失败和未执行的验证。

## Agent skills

### Issue tracker

Issues and specs live as Markdown files under `.scratch/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default five triage labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo: read root `GLOSSARY.md`/`GLOSSARY-MAP.md` when present, `CONTEXT.md`, and `docs/adr/` when exploring. See `docs/agents/domain.md`.
