# OpenSubtitles REST API v1 文档

> 研究日期：2026-08-26（Asia/Shanghai）  
> 用途：OpenSubtitles 字幕搜索、详情读取与下载接入参考。  
> 说明：本文只整理官方文档、官方站点页面及 OpenSubtitles 官方 GitHub 客户端源码；示例中的密钥、Token 和账号均为占位符。

## 1. OpenSubtitles REST API v1

### 1.1 基础约定

- 默认 Base URL：`https://api.opensubtitles.com/api/v1`。
- 登录成功后，响应会返回 `base_url`；后续请求应继续发往该主机。VIP 用户可能返回 `vip-api.opensubtitles.com`，对应路径仍为 `/api/v1/...`。
- 每个请求都应带 `Accept`、`Api-Key` 和带版本号的 `User-Agent`；用户登录后再带 `Authorization: Bearer <JWT>`。官方文档也允许无法设置 `User-Agent` 时使用 `X-User-Agent`。
- `Api-Key` 面向开发者应用；可在账户的 [API consumers 页面](https://www.opensubtitles.com/consumers) 创建 consumer 获取，不要让普通终端用户自行填入开发者 API Key。
- API 支持 CORS。字幕文件默认以 UTF-8 编码返回，`/download` 默认输出 UTF-8 SRT。

官方客户端源码也把默认地址、VIP 地址、`Api-Key` 头和 `Authorization: Bearer` 注入逻辑写死在客户端中，可作为实现交叉核对：[opensubtitlescli/rest `rest.go`](https://github.com/opensubtitlescli/rest/blob/main/rest.go)。

推荐请求头：

```http
Accept: application/json
Api-Key: <OPEN_SUBTITLES_API_KEY>
User-Agent: MyApp v1.0.0
Authorization: Bearer <ACCESS_TOKEN>
```

### 1.2 认证

#### `POST /login`

创建用户会话并返回 JWT。需要 `Api-Key`；请求体为 JSON：

```json
{
  "username": "<USERNAME>",
  "password": "<PASSWORD>"
}
```

成功响应（字段示意）：

```json
{
  "user": {
    "allowed_translations": 5,
    "allowed_downloads": 100,
    "level": "Sub leecher",
    "user_id": 66,
    "ext_installed": false,
    "vip": false
  },
  "base_url": "api.opensubtitles.com",
  "token": "<JWT>",
  "status": 200
}
```

官方文档给出的登录限速是 1 秒 1 次、每分钟 10 次、每小时 30 次；收到 401 后不要用同一组错误凭据继续重试。JWT 有效期为 24 小时。后续请求使用响应中的 `base_url`，不要把开发者账号密码硬编码进公开发布的应用。

来源：[Login](https://opensubtitles.stoplight.io/docs/opensubtitles-api/73acf79accc0a-login)、[Getting started — Authentication](https://opensubtitles.stoplight.io/docs/opensubtitles-api/e3750fd63a100-getting-started#authentication)。

#### `DELETE /logout`

销毁当前用户 Token。需要 `Api-Key`、Bearer Token 和 `User-Agent`。

```bash
curl --location --request DELETE \
  'https://api.opensubtitles.com/api/v1/logout' \
  -H 'Accept: application/json' \
  -H 'Api-Key: <OPEN_SUBTITLES_API_KEY>' \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'User-Agent: MyApp v1.0.0'
```

响应示例：

```json
{
  "message": "token successfully destroyed",
  "status": 200
}
```

来源：[Logout](https://opensubtitles.stoplight.io/docs/opensubtitles-api/9fe4d6d078e50-logout)。

### 1.3 字幕搜索

#### `GET /subtitles`

按文件 moviehash、IMDB/TMDB ID、剧集信息或文本查询字幕。查询参数可组合，但官方建议把它们视为过滤器：如果已知精确 ID，就不要再附加相互矛盾的年份或文本条件。

常用参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `moviehash` | string | 视频文件 moviehash；官方约束为 16 位小写十六进制字符串 |
| `moviehash_match` | string | `include`/`only`；带 moviehash 时可把匹配结果置前 |
| `query` | string | 文件名或文本搜索 |
| `imdb_id` | integer | 电影或剧集集数的 IMDB ID |
| `tmdb_id` | integer | 电影或剧集集数的 TMDB ID |
| `parent_imdb_id` | integer | TV Show 的父级 IMDB ID |
| `parent_tmdb_id` | integer | TV Show 的父级 TMDB ID |
| `parent_feature_id` | integer | TV Show 的父级 feature ID |
| `season_number` | integer | 剧集季号 |
| `episode_number` | integer | 剧集集号 |
| `languages` | string | 逗号分隔的语言代码，按字母排序，例如 `en,fr` |
| `type` | string | `movie`、`episode` 或 `all`（默认 `all`） |
| `year` | integer | 电影/剧集年份过滤 |
| `hearing_impaired` | string | `include`、`exclude`、`only`（默认 `include`） |
| `foreign_parts_only` | string | `include`、`exclude`、`only`（默认 `include`） |
| `ai_translated` | string | `include`、`exclude`（默认 `include`） |
| `machine_translated` | string | `include`、`exclude`（默认 `exclude`） |
| `trusted_sources` | string | `include`、`only`（默认 `include`） |
| `uploader_id` | integer | 单独使用时列出某用户上传的字幕 |
| `order_by` | string | `language`、`download_count`、`new_download_count`、`hearing_impaired`、`hd`、`fps`、`votes`、`points`、`ratings`、`from_trusted`、`foreign_parts_only`、`ai_translated`、`machine_translated`、`upload_date`、`release` 或 `comments` |
| `order_direction` | string | `asc` 或 `desc` |
| `page` | integer | 页码 |

请求示例：

```bash
curl --location --get \
  'https://api.opensubtitles.com/api/v1/subtitles' \
  -H 'Accept: application/json' \
  -H 'Api-Key: <OPEN_SUBTITLES_API_KEY>' \
  -H 'User-Agent: MyApp v1.0.0' \
  --data-urlencode 'moviehash=239f938f5b1d6ebd' \
  --data-urlencode 'languages=zh-cn,en'
```

响应结构：

```json
{
  "total_pages": 1,
  "total_count": 1,
  "per_page": 20,
  "page": 1,
  "data": [
    {
      "id": "<SUBTITLE_ID>",
      "type": "subtitle",
      "attributes": {
        "subtitle_id": "<SUBTITLE_ID>",
        "language": "en",
        "download_count": 0,
        "new_download_count": 0,
        "hearing_impaired": false,
        "hd": true,
        "fps": 23.976,
        "votes": 0,
        "ratings": 0,
        "from_trusted": false,
        "foreign_parts_only": false,
        "upload_date": "2024-01-01T00:00:00Z",
        "ai_translated": false,
        "machine_translated": false,
        "release": "<RELEASE>",
        "comments": "",
        "uploader": {
          "uploader_id": 0,
          "name": "<UPLOADER>",
          "rank": ""
        },
        "feature_details": {
          "feature_id": 0,
          "feature_type": "Movie",
          "year": 2024,
          "title": "<TITLE>",
          "imdb_id": 0,
          "tmdb_id": 0,
          "season_number": 0,
          "episode_number": 0,
          "parent_imdb_id": 0,
          "parent_tmdb_id": 0,
          "parent_feature_id": 0
        },
        "url": "<SUBTITLE_PAGE_URL>",
        "related_links": [],
        "files": [
          {
            "file_id": 123,
            "cd_number": 1,
            "file_name": "subtitle.srt"
          }
        ]
      }
    }
  ]
}
```

如果请求带 `moviehash`，响应会包含/排序 moviehash 匹配结果；如果要搜索 TV episode，官方推荐发送父级 ID、季号和集号；如果已有唯一的 episode ID，则只发送该 ID。为减少重定向，官方建议 GET 参数按字母排序、值使用小写、删除默认值，并从 IMDB ID 中去掉 `tt` 和前导零。

来源：[Search for subtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api/a172317bd5ccc-search-for-subtitles)、[Best-Practices](https://opensubtitles.stoplight.io/docs/opensubtitles-api/6ef2e232095c7-best-practices)。

> 兼容性提示：官方 `opensubtitlescli/rest` 的旧版参数结构仍使用 `languages` 和 `user_id` 字段，而当前 Stoplight 搜索页面使用 `languages` 和 `uploader_id`。实现时应以当前接口页面/实测响应为准，旧客户端源码只作为字段类型参考：[subtitles.go](https://github.com/opensubtitlescli/rest/blob/main/subtitles.go)。

### 1.4 下载字幕

#### `POST /download`

根据搜索结果中的 `file_id` 请求临时下载 URL。此操作会计入下载次数，实际访问返回的文件链接不会再次计数。

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | integer | 是 | `/subtitles` 搜索结果中的文件 ID |
| `sub_format` | string | 否 | `/infos/formats` 返回的目标格式 |
| `file_name` | string | 否 | 期望的文件名 |
| `in_fps` | number | 否 | 转换时的原始 FPS；与 `out_fps` 成对提供 |
| `out_fps` | number | 否 | 转换后的 FPS；与 `in_fps` 成对提供 |
| `timeshift` | number | 否 | 时间偏移，单位秒，可正可负 |
| `force_download` | boolean | 否 | 将文件头设为 `application/force-download` |

官方特别强调：请求必须同时带 `Api-Key` 与 `Authorization`；转 FPS 时必须同时提供输入和输出 FPS；返回 URL 最多有效 3 小时，不要缓存。

```bash
curl --location --request POST \
  'https://api.opensubtitles.com/api/v1/download' \
  -H 'Accept: application/json' \
  -H 'Api-Key: <OPEN_SUBTITLES_API_KEY>' \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'Content-Type: application/json' \
  -H 'User-Agent: MyApp v1.0.0' \
  --data '{
    "file_id": 123,
    "sub_format": "srt",
    "file_name": "movie.zh-cn.srt"
  }'
```

响应结构：

```json
{
  "link": "<TEMPORARY_DOWNLOAD_URL>",
  "file_name": "movie.zh-cn.srt",
  "requests": 3,
  "remaining": 97,
  "message": "Your quota will be renewed in ...",
  "reset_time": "07 hours and 30 minutes",
  "reset_time_utc": "2024-01-01T13:03:16.000Z"
}
```

来源：[Download](https://opensubtitles.stoplight.io/docs/opensubtitles-api/6be7f6ae2d918-download)、[Error codes](https://opensubtitles.stoplight.io/docs/opensubtitles-api/12f131ce12132-error-codes)。

### 1.5 Features 与 Discover

#### `GET /features`

按标题、ID 或年份查找电影/剧集 feature；适合做搜索框自动补全。

| 参数 | 说明 |
| --- | --- |
| `feature_id` | OpenSubtitles feature ID |
| `imdb_id` | IMDB ID；删除前导零 |
| `tmdb_id` | TMDB ID；建议与 `type` 一起使用 |
| `query` | 至少 3 个字符的文本/文件名查询 |
| `query_match` | `start`（默认）、`word`、`exact` |
| `full_search` | 是否搜索原始标题及 title aka/翻译，默认 `false` |
| `type` | 空值列出全部，或 `movie`、`tvshow`、`episode` |
| `year` | 年份；只能与 `query` 组合 |

自动补全不要超过 10 秒 40 次请求。来源：[Search for features](https://opensubtitles.stoplight.io/docs/opensubtitles-api/f5eb2608c8fc7-search-for-features)（若页面 slug 发生变化，可从官方 API 目录进入 Features）。

#### Discover 端点

| 方法 | 作用 | 参数 |
| --- | --- | --- |
| `GET /discover/popular` | 最近 30 天最热门 feature | `language`（每次一个语言代码或 `all`）、`type`（`movie`/`tvshow`） |
| `GET /discover/latest` | 最近上传的 60 条字幕 | `language`、`type` |
| `GET /discover/most_downloaded` | 最近 30 天下载最多的字幕 | `language`、`type` |

来源：[Popular features](https://opensubtitles.stoplight.io/docs/opensubtitles-api/6d285998026d0-popular-features)、[Latest subtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api/f36cef28efaa9-latest-subtitles)、[Most downloaded subtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api/3a149b956fcab-most-downloaded-subtitles)。

> 参数差异提示：上述 Discover 页面使用单数 `language`；旧版官方 Go 客户端结构体使用 `Languages`/`languages` 列表。优先按当前接口页面发送单个 `language`，遇到重定向或校验差异时再对照实测响应。

### 1.6 信息接口

| 方法 | 返回内容 |
| --- | --- |
| `GET /infos/languages` | `{ "data": [{"language_code":"en", "language_name":"English"}] }` |
| `GET /infos/formats` | `{ "data": {"output_formats":["srt","sub","mpl","webvtt","dfxp","txt"]} }` |
| `GET /infos/user` | 需要 Bearer Token；返回 `allowed_downloads`、`level`、`user_id`、`vip`、`downloads_count`、`remaining_downloads` |

来源：[Languages](https://opensubtitles.stoplight.io/docs/opensubtitles-api/1de776d20e873-languages)、[Subtitle Formats](https://opensubtitles.stoplight.io/docs/opensubtitles-api/69b286fc7506e-subtitle-formats)、[User Informations](https://opensubtitles.stoplight.io/docs/opensubtitles-api/ea912bb244ef0-user-informations)。

### 1.7 AI 信息与积分接口（可选）

这些端点属于当前官方文档中的 AI/积分功能，不是普通字幕搜索所必需的：

| 方法 | 认证 | 作用 |
| --- | --- | --- |
| `GET /ai/info/translation` | `Api-Key` | 返回可用翻译 API、支持语言、价格和可靠性 |
| `GET /ai/info/transcription` | `Api-Key` | 返回可用转录 API、支持语言、价格和可靠性 |
| `GET /ai/credits` | `Api-Key` + Bearer | 查询当前用户积分 |
| `GET /ai/credits/buy` | `Api-Key` + Bearer | 返回积分套餐和 checkout URL；不会在 API 调用本身完成支付 |

来源：[AI Translation](https://opensubtitles.stoplight.io/docs/opensubtitles-api/5lfbmcs2cnuk9-ai-translation)、[AI Transcription](https://opensubtitles.stoplight.io/docs/opensubtitles-api/ms3u3hhl9phcu-ai-transcription)、[User Credits Informations](https://opensubtitles.stoplight.io/docs/opensubtitles-api/hhxx9oion0rdo-user-credits-informations)、[Buy Credits](https://opensubtitles.stoplight.io/docs/opensubtitles-api/0tuklm9ob00a3-buy-credits)。

### 1.8 限流、配额与错误处理

#### 限流/配额

- 通用 API：5 请求/秒/IP；超限通常返回 429。响应可能带 `ratelimit-limit`、`ratelimit-remaining`、`ratelimit-reset` 以及对应的 `x-ratelimit-*` 头。
- 登录：额外限制为 1 请求/秒、10 请求/分钟、30 请求/小时。
- 官方 Getting started 页面称搜索本身没有“下载配额”限制，但下载仍受通用限流和账户配额约束。
- 未登录 consumer：文档给出的下载上限为每 IP 每 24 小时 5 条；Under Development consumer 可临时达到每天 100 次。
- 登录用户：文档给出的下载额度从普通注册用户 20 条到 VIP 1000 条，实际剩余额度以 `/download` 响应为准；计数在 UTC 午夜重置。

上述额度属于官方文档当前公开说明，产品策略可能变化，生产客户端应读取服务端返回的 `remaining`/`reset_time_utc`，不要只依赖硬编码数字。

#### 常见 HTTP 错误

| HTTP | 典型信息 | 处理建议 |
| --- | --- | --- |
| 401 | `Error, invalid username/password` | 检查账号密码；停止重复提交同一错误凭据 |
| 403 | `You cannot consume this service` | 检查 `Api-Key`；也可能是 `User-Agent` 不符合要求 |
| 406 | `Invalid file_id` | 检查 `/download` 的 `file_id`；若无响应体，补充 `Accept` |
| 406 | 下载额度 `remaining: -1` | 等待 `reset_time_utc` 或使用有额度的账户 |
| 406 | `invalid token` | 重新登录获得 JWT |
| 410 | `Invalid or expired link` | 重新调用 `/download` 生成链接 |
| 429 | `Throttle limit reached. Retry later.` | 按限流头等待后重试 |
| 5xx | 服务端错误 | 官方建议约 1 秒后重试同一请求 |

来源：[Error codes](https://opensubtitles.stoplight.io/docs/opensubtitles-api/12f131ce12132-error-codes)、[Best-Practices — Limits](https://opensubtitles.stoplight.io/docs/opensubtitles-api/6ef2e232095c7-best-practices#limits)、[Getting started](https://opensubtitles.stoplight.io/docs/opensubtitles-api/e3750fd63a100-getting-started)。

---

## 实现建议

1. Api-Key 与用户 JWT 只放在服务端或安全存储中，不要提交到 Git、前端 bundle 或日志。
2. 先调用 /subtitles 搜索，再使用返回的 file_id 调用 /download；不要把搜索结果中的页面 URL 当作下载链接缓存。
3. /download 返回的链接最多有效 3 小时；遇到 410 或链接过期时重新申请，不要长期保存。
4. 对 429 和 5xx 使用带抖动的退避；登录接口还要遵守 1 秒 1 次、每分钟 10 次、每小时 30 次的限制。
5. 生产请求固定设置可识别的 User-Agent: AppName vX.Y，并遵循服务端重定向。
6. 读取服务端返回的 remaining、reset_time_utc 和限流响应头，不要只依赖硬编码配额。

## 官方来源

- [Getting started](https://opensubtitles.stoplight.io/docs/opensubtitles-api/e3750fd63a100-getting-started)
- [Login](https://opensubtitles.stoplight.io/docs/opensubtitles-api/73acf79accc0a-login)
- [Logout](https://opensubtitles.stoplight.io/docs/opensubtitles-api/9fe4d6d078e50-logout)
- [Search for subtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api/a172317bd5ccc-search-for-subtitles)
- [Download](https://opensubtitles.stoplight.io/docs/opensubtitles-api/6be7f6ae2d918-download)
- [Search for features](https://opensubtitles.stoplight.io/docs/opensubtitles-api/f5eb2608c8fc7-search-for-features)
- [Popular features](https://opensubtitles.stoplight.io/docs/opensubtitles-api/6d285998026d0-popular-features)
- [Latest subtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api/f36cef28efaa9-latest-subtitles)
- [Most downloaded subtitles](https://opensubtitles.stoplight.io/docs/opensubtitles-api/3a149b956fcab-most-downloaded-subtitles)
- [Languages](https://opensubtitles.stoplight.io/docs/opensubtitles-api/1de776d20e873-languages)
- [Subtitle Formats](https://opensubtitles.stoplight.io/docs/opensubtitles-api/69b286fc7506e-subtitle-formats)
- [User Informations](https://opensubtitles.stoplight.io/docs/opensubtitles-api/ea912bb244ef0-user-informations)
- [Error codes](https://opensubtitles.stoplight.io/docs/opensubtitles-api/12f131ce12132-error-codes)
- [Best-Practices](https://opensubtitles.stoplight.io/docs/opensubtitles-api/6ef2e232095c7-best-practices)
- [官方客户端源码：opensubtitlescli/rest](https://github.com/opensubtitlescli/rest)

