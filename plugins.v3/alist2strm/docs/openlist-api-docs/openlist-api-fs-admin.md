# OpenList API：FS 与 Admin 接口整理

> 研究范围：`https://fox.oplist.org/` 当前公开的 OpenList API 文档中 **File System** 和 **Admin** 两组接口。接口页面的路径、方法、请求/响应示例是官网文档确认的信息；路由归属、鉴权中间件和部分官网未展开的查询参数，用 OpenList 官方仓库源码交叉核对。
>
> 统一响应外壳（官网示例）：`{"code": 200, "message": "success", "data": ...}`。下文的“最小示例”只保留完成调用所需的关键字段，不代表完整 schema。示例中的 `TOKEN`、路径和 ID 均为占位值。

本文整理于 2026-09-21。将 `https://fox.oplist.org` 替换为实际部署的 OpenList 地址即可复用下面的请求。

> 2026-09-24 勘误：针对官方源码提交 `893457cd50d954b4a1a81727273b073193b1997d` 的复核发现，`fs/list` 使用的 `PageReq` 将 `page < 1` 归一为 1、`per_page < 1` 归一为最大整数，并非下文所述“默认 30、上限 100”。下文保留原文档快照描述，不能据此推断部署版本的分页行为。

## 0. 基础约定与登录

### 获取 JWT

除允许 Guest 访问的 FS 读接口外，建议所有请求都显式携带 `Authorization`。登录接口本身不需要 token：

```bash
curl -X POST \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<password>","otp_code":"<6-digit-code>"}' \
  https://fox.oplist.org/api/auth/login
```

对应官方页面为[用户登录](https://fox.oplist.org/364155678e0)，成功响应中的 `data.token` 就是后续请求使用的 JWT：

```json
{"code":200,"message":"success","data":{"token":"eyJhbGciOi..."}}
```

调用时使用：

```http
Authorization: <JWT>
```

当前官方源码直接把 `Authorization` 头的值当作 token 解析/比较；本文示例因此不额外拼接 `Bearer ` 前缀。若你的部署版本或反向代理明确要求标准 Bearer 格式，以该部署的实际行为为准。

Admin API 也接受部署配置的管理员 API token；它同样放在 `Authorization` 头中。

## 来源与确认边界

- [OpenList API 官方文档首页](https://fox.oplist.org/)：接口分组、接口页面、HTTP 方法、路径、JWT Bearer 说明、请求和响应示例。
- [官方仓库 `server/router.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/router.go)：FS/Admin 路由、方法及路由组中间件。
- [官方仓库 `server/middlewares/auth.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/middlewares/auth.go)：`Authorization` 的处理、JWT/管理员 token、guest 和 Admin 权限判断。
- [官方仓库 `server/handles/fsread.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/fsread.go)、[`fsmanage.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/fsmanage.go)、[`fsup.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/fsup.go)、[`multipart.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/multipart.go)：FS 参数、请求头、权限和响应实现。
- [官方仓库 `server/handles/user.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/user.go)、[`storage.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/storage.go)、[`driver.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/driver.go)、[`setting.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/setting.go)、[`meta.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/meta.go)、[`sshkey.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/sshkey.go)、[`index.go`](https://github.com/OpenListTeam/OpenList/blob/main/server/handles/index.go)：Admin 查询参数及处理逻辑。

### 鉴权说明

官网页面对 FS/Admin 接口均展示 `Authorization: TOKEN` 的 JWT Bearer 头。源码进一步表明：

- Admin 路由挂在 `AuthAdmin` 下：需要登录用户且必须是管理员；非管理员返回 `403`。
- FS 的读接口（`list`、`get`、`archive/meta`、`archive/list`）挂在 `Auth(true)` 下；源码允许无 token 以 guest 身份进入，但访问仍受路径、元数据密码和权限控制。实际部署若要求显式鉴权，按官网示例发送 `Authorization`。
- FS 的写入、搜索、目录树、文件操作、上传、离线下载和解压接口挂在 `Auth(false)` 下；空 token 可进入 guest 身份，但 guest 的禁用状态和具体写权限仍会影响结果。因此下表将官网要求记为“JWT Bearer”，并在必要处补充源码行为。
- 官方源码同时支持配置的管理员 API token；它与登录得到的 JWT 都通过 `Authorization` 头传递。这里没有把 token 的具体值或生成方式写入示例。

## 1. File System（FS）

### 1.1 读取、搜索和目录树

| 接口 | HTTP 方法与路径 | 鉴权 | 关键请求参数 | 响应要点 | 最小示例 |
|---|---|---|---|---|---|
| [列目录](https://fox.oplist.org/364155732e0) | `POST /api/fs/list` | JWT Bearer；源码归入可 guest 访问的读路由 | JSON：`path`（默认 `/`）、`password`、`refresh`、`page`（默认 1）、`per_page`（1–100，默认 30） | `data.content` 文件/目录数组，`total`、`readme`、`header`、`write`、`provider` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/","page":1,"per_page":30}' https://fox.oplist.org/api/fs/list` |
| [获取文件/目录信息](https://fox.oplist.org/364155733e0) | `POST /api/fs/get` | JWT Bearer；源码归入可 guest 访问的读路由 | JSON：`path`、`password` | `data` 为对象信息，含 `name`、`size`、`is_dir`、时间、`sign`、`type`、`thumb`、`mount_details` 等 | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/movie.mkv"}' https://fox.oplist.org/api/fs/get` |
| [搜索文件和目录](https://fox.oplist.org/364155734e0) | `POST /api/fs/search` | JWT Bearer；源码使用搜索索引并走普通 FS 鉴权 | JSON：`parent`、`keywords`、`scope`、`page`、`per_page`；源码还接受 `password` | `data.content` 搜索结果，`data.total` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"parent":"/","keywords":"movie","scope":0,"page":1,"per_page":30}' https://fox.oplist.org/api/fs/search` |
| [获取目录树](https://fox.oplist.org/364155735e0) | `POST /api/fs/dirs` | JWT Bearer | JSON：`path`、`password`、`force_root`；`force_root=true` 需要 Admin（源码确认） | `data` 为目录数组，官网示例字段为 `name`、`path`；源码还返回 `modified` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/","force_root":false}' https://fox.oplist.org/api/fs/dirs` |
| [获取其他文件操作](https://fox.oplist.org/364155736e0) | `POST /api/fs/other` | JWT Bearer | JSON：`path`、`method`；源码还带 `password` | `data` 由底层 driver 的 other 操作决定；官网示例为 `null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/movie.mkv","method":"string"}' https://fox.oplist.org/api/fs/other` |

### 1.2 普通上传与离线下载

| 接口 | HTTP 方法与路径 | 鉴权 | 关键请求参数/请求头 | 响应要点 | 最小示例 |
|---|---|---|---|---|---|
| [流式上传](https://fox.oplist.org/364155746e0) | `PUT /api/fs/put` | JWT Bearer | Body：`application/octet-stream`；源码确认关键头：`File-Path`（URL 编码路径）、可选 `Overwrite`、`As-Task`、`X-File-Size`、`X-File-Md5`/`Sha1`/`Sha256`、`Last-Modified` | 成功通常 `data=null`；`As-Task=true` 时可能返回 `data.task` | `curl -X PUT -H 'Authorization: TOKEN' -H 'File-Path: /movie.mkv' --data-binary @movie.mkv https://fox.oplist.org/api/fs/put` |
| [表单上传](https://fox.oplist.org/364155747e0) | `PUT /api/fs/form` | JWT Bearer | `multipart/form-data`，文件字段 `file`（源码确认）；同样使用 `File-Path`，可选 `Overwrite`、`As-Task` 和哈希头 | 成功通常 `data=null`；异步任务时可能返回 `data.task` | `curl -X PUT -H 'Authorization: TOKEN' -H 'File-Path: /movie.mkv' -F 'file=@movie.mkv' https://fox.oplist.org/api/fs/form` |
| [添加离线下载任务](https://fox.oplist.org/364155748e0) | `POST /api/fs/add_offline_download` | JWT Bearer | JSON：`path`、`urls[]`、`tool` | 创建任务，官网成功示例 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/downloads","urls":["https://example.com/file.zip"],"tool":"aria2"}' https://fox.oplist.org/api/fs/add_offline_download` |
| [解压归档](https://fox.oplist.org/364155749e0) | `POST /api/fs/archive/decompress` | JWT Bearer | JSON：`src_dir`、`name`、`dst_dir` | 启动解压，官网成功示例 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"src_dir":"/archives","name":"a.zip","dst_dir":"/extracted"}' https://fox.oplist.org/api/fs/archive/decompress` |
| [读取归档元数据](https://fox.oplist.org/364155750e0) | `POST /api/fs/archive/meta` | JWT Bearer；源码归入可 guest 访问的读路由 | JSON：`path` | `data` 至少示例出 `format`、`encrypted`、`total_files` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/a.zip"}' https://fox.oplist.org/api/fs/archive/meta` |
| [列出归档内容](https://fox.oplist.org/364155751e0) | `POST /api/fs/archive/list` | JWT Bearer；源码归入可 guest 访问的读路由 | JSON：`path`、`archive_path` | `data.content`、`data.total`，内容项结构接近 FS 对象 | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/a.zip","archive_path":"/"}' https://fox.oplist.org/api/fs/archive/list` |

### 1.3 分片上传

分片接口除 `Authorization` 外，关键参数由官方源码以请求头定义：`File-Path`、`X-File-Size`、可选 `X-Chunk-Size`、`Overwrite` 和哈希/时间头；每个 chunk 使用 `X-Upload-Id`、`X-Chunk-Index`。官网页面给出会话快照字段，但没有把这些 header 参数完整展开，因此 header 名称以官方源码为确认来源。

| 接口 | HTTP 方法与路径 | 鉴权 | 关键请求头/请求体 | 响应要点 | 最小示例 |
|---|---|---|---|---|---|
| [初始化分片上传](https://fox.oplist.org/497934258e0) | `POST /api/fs/multipart/init` | JWT Bearer | 头：`File-Path`、`X-File-Size`；可选 `X-Chunk-Size`、`Overwrite`、`Content-Type`、哈希头 | `data` 会话快照：`upload_id`、`state`、`chunk_size`、`total_chunks`、`received`、`frontier`、`storage_progress`、`resumed` | `curl -X POST -H 'Authorization: TOKEN' -H 'File-Path: /movie.mkv' -H 'X-File-Size: 734003200' https://fox.oplist.org/api/fs/multipart/init` |
| [上传一个 chunk](https://fox.oplist.org/497934259e0) | `PUT /api/fs/multipart/chunk` | JWT Bearer | 头：`X-Upload-Id`、`X-Chunk-Index`；Body：`application/octet-stream` | 返回最新会话快照；源码确认 chunk 可幂等、可在窗口内并发/乱序；`409` 表示同 chunk 正在传输，`429` 表示窗口满 | `curl -X PUT -H 'Authorization: TOKEN' -H 'X-Upload-Id: UPLOAD_ID' -H 'X-Chunk-Index: 0' --data-binary @chunk-00000 https://fox.oplist.org/api/fs/multipart/chunk` |
| [完成分片上传](https://fox.oplist.org/497934260e0) | `POST /api/fs/multipart/complete` | JWT Bearer | 头：`X-Upload-Id` | 成功时 `data.state="completed"`，会话被回收；未完成返回错误 | `curl -X POST -H 'Authorization: TOKEN' -H 'X-Upload-Id: UPLOAD_ID' https://fox.oplist.org/api/fs/multipart/complete` |
| [查询上传会话](https://fox.oplist.org/497934261e0) | `GET /api/fs/multipart/status` | JWT Bearer | Query：`upload_id`；或用 `path` + `size` 查找可恢复会话（源码确认） | 返回最新会话快照，字段同初始化接口 | `curl -G -H 'Authorization: TOKEN' --data-urlencode 'upload_id=UPLOAD_ID' https://fox.oplist.org/api/fs/multipart/status` |
| [取消分片上传](https://fox.oplist.org/497934262e0) | `POST /api/fs/multipart/abort` | JWT Bearer | 头：`X-Upload-Id` | 成功时 `data=null`，会话取消并回收 | `curl -X POST -H 'Authorization: TOKEN' -H 'X-Upload-Id: UPLOAD_ID' https://fox.oplist.org/api/fs/multipart/abort` |

### 1.4 文件和目录管理

这些接口均使用 `application/json` body；官网成功响应均为 `code=200`、`message=success`、`data=null`。源码还定义了未在官网最小示例中展示的可选控制字段：重命名的 `overwrite`，移动/复制的 `overwrite`、`skip_existing`、`merge`。

| 接口 | HTTP 方法与路径 | 鉴权 | 关键请求参数 | 响应要点 | 最小示例 |
|---|---|---|---|---|---|
| [创建目录](https://fox.oplist.org/364155737e0) | `POST /api/fs/mkdir` | JWT Bearer | JSON：`path` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/newfolder"}' https://fox.oplist.org/api/fs/mkdir` |
| [重命名文件或目录](https://fox.oplist.org/364155738e0) | `POST /api/fs/rename` | JWT Bearer | JSON：`path`、`name`；源码另有 `overwrite` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/old.txt","name":"new.txt"}' https://fox.oplist.org/api/fs/rename` |
| [批量重命名](https://fox.oplist.org/364155739e0) | `POST /api/fs/batch_rename` | JWT Bearer | JSON：`src_dir`、`rename_objects[]`，元素为 `src_name`、`new_name` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"src_dir":"/folder","rename_objects":[{"src_name":"a.txt","new_name":"b.txt"}]}' https://fox.oplist.org/api/fs/batch_rename` |
| [正则重命名](https://fox.oplist.org/364155740e0) | `POST /api/fs/regex_rename` | JWT Bearer | JSON：`src_dir`、`src_name_regex`、`new_name_regex` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"src_dir":"/folder","src_name_regex":"^(.*)\\.(txt)$","new_name_regex":"$1_renamed.$2"}' https://fox.oplist.org/api/fs/regex_rename` |
| [移动文件或目录](https://fox.oplist.org/364155741e0) | `POST /api/fs/move` | JWT Bearer | JSON：`src_dir`、`dst_dir`、`names[]`；可选 `overwrite`、`skip_existing` | 成功可能立即完成，也可能返回移动任务信息（源码确认）；官网最小示例为 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"src_dir":"/source","dst_dir":"/destination","names":["a.txt"]}' https://fox.oplist.org/api/fs/move` |
| [递归移动](https://fox.oplist.org/364155742e0) | `POST /api/fs/recursive_move` | JWT Bearer | JSON：`src_dir`、`dst_dir` | 官网成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"src_dir":"/source","dst_dir":"/destination"}' https://fox.oplist.org/api/fs/recursive_move` |
| [复制文件或目录](https://fox.oplist.org/364155743e0) | `POST /api/fs/copy` | JWT Bearer | JSON：`src_dir`、`dst_dir`、`names[]`；可选 `overwrite`、`skip_existing`、`merge` | 成功可能立即完成，也可能返回复制任务信息（源码确认）；官网最小示例为 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"src_dir":"/source","dst_dir":"/destination","names":["a.txt"]}' https://fox.oplist.org/api/fs/copy` |
| [删除文件或目录](https://fox.oplist.org/364155744e0) | `POST /api/fs/remove` | JWT Bearer | JSON：`dir`、`names[]` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"dir":"/folder","names":["a.txt"]}' https://fox.oplist.org/api/fs/remove` |
| [删除空目录](https://fox.oplist.org/364155745e0) | `POST /api/fs/remove_empty_directory` | JWT Bearer | JSON：`src_dir` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"src_dir":"/folder"}' https://fox.oplist.org/api/fs/remove_empty_directory` |

## 2. Admin

以下所有接口均挂在官方源码的 `admin(auth.Group("/admin", middlewares.AuthAdmin))` 下：需要 `Authorization`，且调用者必须是 Admin。官网成功响应通常为 `code=200`、`message=success`、`data` 为结果或 `null`；列表接口常见分页结构为 `data.content` + `data.total`。

### 2.1 用户与 SSH 公钥

| 接口 | HTTP 方法与路径 | 关键请求参数 | 响应要点 | 最小示例 |
|---|---|---|---|---|
| [用户列表](https://fox.oplist.org/364155697e0) | `GET /api/admin/user/list` | Query：`page`、`per_page`（源码确认，分页默认/范围沿用 `PageReq`） | `data.content` 用户数组、`data.total`；用户对象示例含 `id`、`username`、`base_path`、`role`、`disabled`、`permission`、`otp` | `curl -G -H 'Authorization: TOKEN' --data 'page=1' --data 'per_page=30' https://fox.oplist.org/api/admin/user/list` |
| [按 ID 获取用户](https://fox.oplist.org/364155698e0) | `GET /api/admin/user/get` | Query：`id` | `data` 为用户对象 | `curl -G -H 'Authorization: TOKEN' --data 'id=1' https://fox.oplist.org/api/admin/user/get` |
| [创建用户](https://fox.oplist.org/364155699e0) | `POST /api/admin/user/create` | JSON：`username`、`password`、`base_path`、`role`、`permission`、`disabled` | 成功 `data=null`；源码禁止创建 Admin/Guest 用户 | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"username":"newuser","password":"password123","base_path":"/","role":0,"permission":0,"disabled":false}' https://fox.oplist.org/api/admin/user/create` |
| [更新用户](https://fox.oplist.org/364155700e0) | `POST /api/admin/user/update` | JSON：`id`、`username`、`password`、`base_path`、`role`、`permission`、`disabled` | 成功 `data=null`；源码不允许修改 role，Admin 不能被禁用 | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"id":1,"username":"admin","base_path":"/","role":0,"permission":29183,"disabled":false}' https://fox.oplist.org/api/admin/user/update` |
| [删除用户](https://fox.oplist.org/364155701e0) | `POST /api/admin/user/delete` | 官网示例 JSON `id`；源码读取 Query `id` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'id=2' https://fox.oplist.org/api/admin/user/delete` |
| [取消用户 2FA](https://fox.oplist.org/364155702e0) | `POST /api/admin/user/cancel_2fa` | 官网示例 JSON `id`；源码读取 Query `id` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'id=2' https://fox.oplist.org/api/admin/user/cancel_2fa` |
| [清理用户缓存](https://fox.oplist.org/364155703e0) | `POST /api/admin/user/del_cache` | 官网示例 JSON `username`；源码读取 Query `username` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'username=alice' https://fox.oplist.org/api/admin/user/del_cache` |
| [列出用户 SSH 公钥](https://fox.oplist.org/364155704e0) | `GET /api/admin/user/sshkey/list` | Query：`uid`；源码还接受 `page`、`per_page` | `data.content` 公钥数组、`data.total` | `curl -G -H 'Authorization: TOKEN' --data 'uid=2' --data 'page=1' --data 'per_page=30' https://fox.oplist.org/api/admin/user/sshkey/list` |
| [删除用户 SSH 公钥](https://fox.oplist.org/364155705e0) | `POST /api/admin/user/sshkey/delete` | 官网示例 JSON `id`；源码读取 Query `id` | 成功为标准成功响应；官网当前页面未展示 200 响应块的具体文本 | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'id=10' https://fox.oplist.org/api/admin/user/sshkey/delete` |

### 2.2 存储与驱动

| 接口 | HTTP 方法与路径 | 关键请求参数 | 响应要点 | 最小示例 |
|---|---|---|---|---|
| [存储列表](https://fox.oplist.org/364155706e0) | `GET /api/admin/storage/list` | Query：`page`、`per_page` | `data.content` 存储数组、`data.total`；对象含 `id`、`mount_path`、`driver`、`addition`、`disabled` 等 | `curl -G -H 'Authorization: TOKEN' --data 'page=1' --data 'per_page=30' https://fox.oplist.org/api/admin/storage/list` |
| [按 ID 获取存储](https://fox.oplist.org/364155707e0) | `GET /api/admin/storage/get` | Query：`id` | `data` 为存储对象 | `curl -G -H 'Authorization: TOKEN' --data 'id=1' https://fox.oplist.org/api/admin/storage/get` |
| [创建存储](https://fox.oplist.org/364155708e0) | `POST /api/admin/storage/create` | JSON：`mount_path`、`driver`、`order`、`cache_expiration`、`addition`、`remark` | 官网示例 `data=null`；源码实现可能返回创建的 `id`（以当前仓库源码为准） | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"mount_path":"/local","driver":"Local","order":0,"cache_expiration":30,"addition":"{\"root_folder_path\":\"/data\"}","remark":"Local"}' https://fox.oplist.org/api/admin/storage/create` |
| [更新存储](https://fox.oplist.org/364155709e0) | `POST /api/admin/storage/update` | JSON：`id`、`mount_path`、`driver`、`order`、`cache_expiration`、`status`、`addition`、`remark`、`disabled`、`disable_index`、`enable_sign`、`order_by`、`order_direction`、`extract_folder`、`web_proxy`、`webdav_policy`、`down_proxy_url` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"id":1,"mount_path":"/local","driver":"Local","order":0,"cache_expiration":30,"addition":"{\"root_folder_path\":\"/data\"}","remark":"Local"}' https://fox.oplist.org/api/admin/storage/update` |
| [删除存储](https://fox.oplist.org/364155710e0) | `POST /api/admin/storage/delete` | 官网示例 JSON `id`；源码读取 Query `id` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'id=1' https://fox.oplist.org/api/admin/storage/delete` |
| [启用存储](https://fox.oplist.org/364155711e0) | `POST /api/admin/storage/enable` | 官网示例 JSON `id`；源码读取 Query `id` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'id=1' https://fox.oplist.org/api/admin/storage/enable` |
| [禁用存储](https://fox.oplist.org/364155712e0) | `POST /api/admin/storage/disable` | 官网示例 JSON `id`；源码读取 Query `id` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'id=1' https://fox.oplist.org/api/admin/storage/disable` |
| [重新加载全部存储](https://fox.oplist.org/364155713e0) | `POST /api/admin/storage/load_all` | 无 body/query | 成功 `data=null`；源码异步重新加载启用的存储 | `curl -X POST -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/storage/load_all` |
| [列出驱动信息](https://fox.oplist.org/364155714e0) | `GET /api/admin/driver/list` | 无 body/query | `data` 为驱动信息数组，含 `name`、`config` | `curl -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/driver/list` |
| [列出驱动名称](https://fox.oplist.org/364155715e0) | `GET /api/admin/driver/names` | 无 body/query | `data` 为名称数组 | `curl -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/driver/names` |
| [获取驱动信息](https://fox.oplist.org/364155716e0) | `GET /api/admin/driver/info` | 官网仅标 Query；源码参数名为 `driver` | `data` 为所选驱动信息；不存在返回 `404` | `curl -G -H 'Authorization: TOKEN' --data-urlencode 'driver=Local' https://fox.oplist.org/api/admin/driver/info` |

### 2.3 设置、API token 与 Meta

| 接口 | HTTP 方法与路径 | 关键请求参数 | 响应要点 | 最小示例 |
|---|---|---|---|---|
| [设置列表](https://fox.oplist.org/364155717e0) | `GET /api/admin/setting/list` | 可选 Query：`group` 或逗号分隔的 `groups`（源码确认） | `data` 为设置项数组，示例字段：`key`、`value`、`type`、`options`、`group`、`flag` | `curl -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/setting/list` |
| [按 key 获取设置](https://fox.oplist.org/364155718e0) | `GET /api/admin/setting/get` | Query：`key`；源码也支持逗号分隔的 `keys` | `data` 为单项或多项设置；官网示例为 `{}` | `curl -G -H 'Authorization: TOKEN' --data-urlencode 'key=site_title' https://fox.oplist.org/api/admin/setting/get` |
| [保存设置](https://fox.oplist.org/364155719e0) | `POST /api/admin/setting/save` | JSON 数组，每项至少 `key`、`value` | 成功 `data=null` | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '[{"key":"site_title","value":"OpenList"}]' https://fox.oplist.org/api/admin/setting/save` |
| [删除设置](https://fox.oplist.org/364155720e0) | `POST /api/admin/setting/delete` | 官网示例 JSON `key`；源码读取 Query `key` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'key=site_title' https://fox.oplist.org/api/admin/setting/delete` |
| [重置 API token](https://fox.oplist.org/364155721e0) | `POST /api/admin/setting/reset_token` | 无 body/query | 官网示例称 token reset 成功且 `data=null`；官方源码当前实现会生成新 token 并把 token 作为 `data` 返回。调用后旧 token 可能失效，属于有外部状态影响的操作 | `curl -X POST -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/setting/reset_token` |
| [Meta 列表](https://fox.oplist.org/364155722e0) | `GET /api/admin/meta/list` | Query：`page`、`per_page` | `data.content` Meta 数组、`data.total`；官网示例内容项为空对象 | `curl -G -H 'Authorization: TOKEN' --data 'page=1' --data 'per_page=30' https://fox.oplist.org/api/admin/meta/list` |
| [按 ID 获取 Meta](https://fox.oplist.org/364155723e0) | `GET /api/admin/meta/get` | Query：`id` | `data` 为 Meta；官网示例为 `null` | `curl -G -H 'Authorization: TOKEN' --data 'id=1' https://fox.oplist.org/api/admin/meta/get` |
| [创建 Meta](https://fox.oplist.org/364155724e0) | `POST /api/admin/meta/create` | JSON：`path`、`password`、`readme`、`header`、`hide` | 成功 `data=null`；源码会校验 `hide` 中的正则 | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"path":"/movies","password":"","readme":"","header":"","hide":""}' https://fox.oplist.org/api/admin/meta/create` |
| [更新 Meta](https://fox.oplist.org/364155725e0) | `POST /api/admin/meta/update` | JSON：`id`、`path`、`password`、`readme`、`header`、`hide` | 成功 `data=null`；`hide` 仍需通过正则校验 | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"id":1,"path":"/movies","password":"","readme":"","header":"","hide":""}' https://fox.oplist.org/api/admin/meta/update` |
| [删除 Meta](https://fox.oplist.org/364155726e0) | `POST /api/admin/meta/delete` | 官网示例 JSON `id`；源码读取 Query `id` | 成功 `data=null` | `curl -X POST -H 'Authorization: TOKEN' --data-urlencode 'id=1' https://fox.oplist.org/api/admin/meta/delete` |

### 2.4 搜索索引

| 接口 | HTTP 方法与路径 | 关键请求参数 | 响应要点 | 最小示例 |
|---|---|---|---|---|
| [构建搜索索引](https://fox.oplist.org/364155727e0) | `POST /api/admin/index/build` | 无 body/query | 启动全量构建，官网成功 `data=null`；任务异步执行 | `curl -X POST -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/index/build` |
| [更新搜索索引](https://fox.oplist.org/364155728e0) | `POST /api/admin/index/update` | JSON：`paths[]`；源码还接受 `max_depth` | 启动指定路径更新，官网成功 `data=null`；索引任务异步执行 | `curl -H 'Authorization: TOKEN' -H 'Content-Type: application/json' -d '{"paths":["/movies"],"max_depth":20}' https://fox.oplist.org/api/admin/index/update` |
| [停止索引](https://fox.oplist.org/364155729e0) | `POST /api/admin/index/stop` | 无 body/query | 成功 `data=null`；未运行时源码返回 `400` | `curl -X POST -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/index/stop` |
| [清空搜索索引](https://fox.oplist.org/364155730e0) | `POST /api/admin/index/clear` | 无 body/query | 成功 `data=null`；运行中源码返回 `400` | `curl -X POST -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/index/clear` |
| [获取索引进度](https://fox.oplist.org/364155731e0) | `GET /api/admin/index/progress` | 无 body/query | `data`：`total`、`current`、`status` | `curl -H 'Authorization: TOKEN' https://fox.oplist.org/api/admin/index/progress` |

## 无法完全确认或存在差异的事项

1. `fox.oplist.org` 页面是一个具体的 Apifox 文档快照，而官方仓库 `main` 可能继续变化；本稿以官网页面确认公开接口，以当前官方源码补充实现细节，二者若冲突已在对应条目中标出。
2. 官网若只显示“Query Params”而没有展开字段，字段名来自官方 handler 源码。例如用户/存储/Meta 的 `id`、驱动信息的 `driver`、设置的 `key`/`keys`、SSH 用户的 `uid`。这些字段已由源码确认，但官网页面本身没有完整显示。
3. 官网将部分变更接口的请求示例显示为 JSON body，但当前官方源码对若干简单 Admin 操作读取的是 query 参数（删除用户、取消 2FA、清理缓存、删除 SSH key、存储删除/启停、设置删除、Meta 删除）。文档示例优先按源码可实际工作的 query 形式给出，并保留官网 body 形式作为“官网示例”记录。
4. `POST /api/admin/setting/reset_token` 的官网响应示例是 `data=null`，当前官方源码返回新 token；实际返回以部署版本为准。该接口会改变认证凭据，调用前应确认影响。
5. 普通上传的 `File-Path`、分片上传的各类 header、FS 管理接口的可选控制字段，以及移动/复制可能返回异步任务，官网最小页面未全部展开，均以官方源码为补充确认；不同发行版本或部署配置可能改变可选字段和具体错误码。
6. 本次没有执行任何写入、上传、删除、索引或 token 重置请求，只做了官方文档和源码的只读核对。
