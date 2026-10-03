# ASSRT API v1 文档

> 研究日期：2026-08-26（Asia/Shanghai）  
> 用途：ASSRT 字幕搜索、详情读取与下载接入参考。  
> 说明：本文只整理 ASSRT 官方 API 文档与官方变更日志；示例中的 Token 和字幕 ID 均为占位符。

## 1. ASSRT API v1

### 1.1 文档状态与基础约定

ASSRT 官方 API 文档仍位于 `assrt.net/api/doc`，但页面底部标注“最后修改：2020/04/27”；官方变更日志最后一项为 2016/09/13。因此，下面把它视为“官方历史 API 合约”：接入前应先用自己的 Token 做小流量验证。

- 主域名：`https://api.assrt.net`；官方同时说明可使用 HTTP，网络受限时可尝试 `api.makedie.me`。
- 版本路径：`/v1/<service>/<action>`。
- 返回格式：JSON。
- GET 与 POST 均可使用；POST 使用 `application/x-www-form-urlencoded`。POST 请求 URL 中的查询参数会被忽略，以请求体参数为准。
- 官方要求在合适位置注明来源，例如“字幕服务由 assrt.net 提供”。

来源：[ASSRT API 文档](https://assrt.net/api/doc#summary)、[ASSRT 变更日志](https://assrt.net/api/changelog)。

### 1.2 Token 与配额

- Token 是 32 位数字、大小写字母随机组合。
- 用户注册后可在用户面板查看 API Token，并可重置 Token。
- 默认所有请求都需要 Token。可以使用查询参数 `token=TOKEN`，也可以使用 `Authorization: Bearer TOKEN`。
- 初始配额为 20 次/分钟；同一个 Token 与同一个 IP 的请求共享配额：不同 Token 同 IP 会合并计算，不同 IP 同 Token 也会合并计算。
- 需要更高速率时，官方文档要求联系站方审核。

查询参数方式：

```http
GET /v1/sub/detail?token=<ASSRT_TOKEN>&id=602333 HTTP/1.1
Host: api.assrt.net
Accept: application/json
```

Bearer 方式：

```http
GET /v1/sub/detail?id=602333 HTTP/1.1
Host: api.assrt.net
Accept: application/json
Authorization: Bearer <ASSRT_TOKEN>
```

来源：[使用 Token](https://assrt.net/api/doc#usetoken)、[API 配额](https://assrt.net/api/doc#apiquota)。

### 1.3 字幕搜索

#### `GET/POST /v1/sub/search`

搜索字符串或视频文件名。参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `q` | 是 | 搜索字符串；正文写“至少长度为 3” |
| `pos` | 否 | 分页起始位置 |
| `cnt` | 否 | 返回数量，最多 15，默认 15 |
| `is_file` | 否 | `1` 表示 `q` 是不含扩展名的视频文件名 |
| `no_muxer` | 否 | `1` 表示忽略压制组和视频参数；官方说明该模式默认按文件名搜索 |
| `filelist` | 否 | `1` 同时返回压缩包内文件列表，但不返回文件下载链接 |

请求示例：

```bash
curl --get 'https://api.assrt.net/v1/sub/search' \
  --data-urlencode 'token=<ASSRT_TOKEN>' \
  --data-urlencode 'q=big.brother.us.s17e26.720p.hdtv.x264-bajskorv' \
  --data-urlencode 'is_file=1' \
  --data-urlencode 'cnt=15' \
  --data-urlencode 'pos=0'
```

成功响应骨架：

```json
{
  "status": 0,
  "sub": {
    "subs": [
      {
        "native_name": "<NATIVE_TITLE>",
        "videoname": "<VIDEO_NAME>",
        "revision": 0,
        "subtype": "Subrip(srt)",
        "upload_time": "2015-08-21 22:11:00",
        "vote_score": 0,
        "id": 594897,
        "release_site": "<RELEASE_SITE>",
        "lang": {
          "langlist": {"langdou": true},
          "desc": "双语"
        }
      }
    ],
    "action": "search",
    "keyword": "<QUERY>",
    "result": "succeed"
  }
}
```

搜索文件名时，官方建议优先尝试普通查询；结果过多再使用 `is_file=1` 或 `no_muxer=1`。官方正文参数表写“至少 3 个字符”，但错误表 101 的英文是 `length of keyword must be longer than 3`。这是官方资料内部矛盾；为避免 101，客户端可按“长度大于 3（至少 4 个字符）”校验，这是基于两处官方文本的兼容性推断。

来源：[sub/search](https://assrt.net/api/doc#subsearch)、[ASSRT 错误值](https://assrt.net/api/doc#errors)。

### 1.4 字幕详情与下载地址

#### `GET/POST /v1/sub/detail`

参数 `id` 必填，官方定义为 6 位整数。

详情响应中的 `sub.subs[0]` 可能包含：

| 字段 | 含义 |
| --- | --- |
| `filename` | 字幕压缩包/文件名 |
| `native_name` | 影片原名 |
| `id` | 字幕 ID |
| `down_count` | 下载次数 |
| `revision` | 修订版本 |
| `upload_time` | 上传时间 |
| `url` | 压缩包下载地址 |
| `size` | 文件大小 |
| `producer` | 发布人、校订者、来源、上传者 |
| `filelist` | 压缩包内文件数组；每项含 `url`、`f`（文件名）、`s`（大小） |
| `subtype` | 字幕格式 |
| `title` | 标题 |
| `vote_score` | 评分 |
| `release_site` | 字幕组/来源 |
| `videoname` | 匹配的视频文件名 |
| `view_count` | 浏览次数 |
| `lang` | 语言描述与语言布尔标记 |

官方明确要求不要保存下载地址：每次生成的 URL 都是唯一的，并且有有效期。`sub/similar` 的返回格式与搜索一致。

请求示例：

```bash
curl --get 'https://api.assrt.net/v1/sub/detail' \
  --data-urlencode 'token=<ASSRT_TOKEN>' \
  --data-urlencode 'id=602333'
```

来源：[sub/detail](https://assrt.net/api/doc#subdetail)、[数据类型 subs](https://assrt.net/api/doc#subs)。

#### `GET/POST /v1/sub/similar`

参数同样为必填的 6 位 `id`，返回与指定字幕相似的其他 5 条字幕，响应格式与 `sub/search` 相同。

来源：[sub/similar](https://assrt.net/api/doc#subsimilar)。

### 1.5 用户配额

#### `GET/POST /v1/user/quota`

无业务参数，仍需 Token。

```bash
curl --get 'https://api.assrt.net/v1/user/quota' \
  --data-urlencode 'token=<ASSRT_TOKEN>'
```

成功响应：

```json
{
  "status": 0,
  "user": {
    "result": "succeed",
    "action": "quota",
    "quota": 20
  }
}
```

来源：[user/quota](https://assrt.net/api/doc#userquota)。

### 1.6 已删除/不应新实现的端点

ASSRT 官方目录把以下端点用删除线标记，当前页面没有可依赖的参数和响应合约：

| 历史端点 | 官方状态 |
| --- | --- |
| `/v1/sub/vote` | 已删除/废弃 |
| `/v1/sub/upload` | 已删除/废弃 |
| `/v1/user/reset_token` | 已删除/废弃 |
| `/v1/user/get_token` | 已删除/废弃 |

不要根据旧博客或第三方 SDK 猜测这些端点的写入行为；如果业务确实需要投票、上传或 Token 管理，应改用 ASSRT 站点的用户界面或先取得站方确认。

来源：[ASSRT 官方 API 目录（删除线端点）](https://assrt.net/api/doc#endpoints)。

### 1.7 响应状态与错误码

客户端应先判断顶层 `status`：`0` 表示成功，非零表示错误。ASSRT 的 `APIError` 可能仍返回 HTTP 200；`ClientFail` 通常是 4xx；`ServerFail` 通常是 5xx。

| 类别 | 代码 | 英文信息 | 含义 |
| --- | ---: | --- | --- |
| APIError | 1 | `no such user` | 用户不存在 |
| APIError | 101 | `length of keyword must be longer than 3` | 搜索关键词太短 |
| ClientFail | 20000 | `your request is missing essential arguments` | 缺少必要参数 |
| ClientFail | 20001 | `invalid token` | Token 不存在/无效 |
| ClientFail | 20400 | `API endpoint not found` | 端点不存在 |
| ClientFail | 20900 | `subtitle not found` | 字幕不存在 |
| ServerFail | 30000 | `server is encounting errors` | 服务端错误 |
| ServerFail | 30001 | `database is unavailable` | 数据库不可用 |
| ServerFail | 30002 | `search engine is unavailable` | 搜索引擎不可用 |
| ServerFail | 30300 | `API is temporarily unavailable` | API 临时不可用 |
| ServerFail | 30900 | `you are exceeding request limits` | 超出配额 |

来源：[错误值](https://assrt.net/api/doc#errors)。

---

## 实现建议

1. Token 只放在服务端或安全存储中，不要提交到 Git、前端 bundle 或日志。
2. sub/detail 返回的 url 与 filelist[].url 视为临时地址；每次下载前重新请求详情。
3. 客户端必须先判断顶层 status，不能只依据 HTTP 状态码；APIError 可能仍返回 HTTP 200。
4. 对 4xx/5xx 和配额错误使用带抖动的退避；默认配额为 20 次/分钟，实际以站点返回和实测为准。
5. 按官方要求保留“字幕服务由 assrt.net 提供”等来源声明。
6. 官方资料较旧，建议在集成测试中记录请求时间、HTTP 状态、顶层 status、响应体结构和限流行为，并把真实验证日期写入项目文档。

## 官方来源

- [ASSRT API 文档](https://assrt.net/api/doc)
- [ASSRT API 文档：概述](https://assrt.net/api/doc#summary)
- [ASSRT API 文档：使用 Token](https://assrt.net/api/doc#usetoken)
- [ASSRT API 文档：API 配额](https://assrt.net/api/doc#apiquota)
- [ASSRT API 文档：sub/search](https://assrt.net/api/doc#subsearch)
- [ASSRT API 文档：sub/detail](https://assrt.net/api/doc#subdetail)
- [ASSRT API 文档：sub/similar](https://assrt.net/api/doc#subsimilar)
- [ASSRT API 文档：user/quota](https://assrt.net/api/doc#userquota)
- [ASSRT API 文档：数据类型](https://assrt.net/api/doc#subs)
- [ASSRT API 文档：错误值](https://assrt.net/api/doc#errors)
- [ASSRT API 变更日志](https://assrt.net/api/changelog)

