# 豆包工作本机 OAuth 网关

本目录包含可独立部署的单用户网关源码，要求 macOS 和 Node.js ≥24，无外部 npm 依赖。服务固定监听 `127.0.0.1:8791`，不是 OpenAI 协议网关，不支持直接公网或容器部署。接口合同见 [docs/openapi.json](docs/openapi.json)。

## 目标机器部署

先将本目录放到稳定的安装位置，确认 Node.js 版本，并确认 8791 未被其他服务占用。不要在已有服务运行时重复启动；不能通过随意改一个端口解决冲突。

在飞书开放平台为本人准备有豆包工作权限的应用，登记精确回调 `http://127.0.0.1:8791/oauth/callback`，申请并启用：

```text
doubao:file:write
doubao:session.event:write
doubao:session.message:read
doubao:session:read
doubao:session:write
offline_access
```

平台是否接受本机 HTTP 回调、应用可用范围、PKCE/DPoP 兼容性和账号业务权益，须在目标机器实际核验；不绕过平台安全限制。

在本目录的本人终端执行以下命令，或使用对应 `.command`：

```bash
npm run configure
npm run prepare-local
npm start
```

服务启动后，在另一终端的本目录执行：

```bash
npm run authorize
npm run status
```

`configure` 隐藏输入 App Secret；`authorize` 打开本人浏览器授权。不要把 App Secret、本地 Key、令牌或一次性授权链接写进日志、提交到仓库或发送到聊天。`npm run key` 会显示本地 Key，只能由本人在私有终端按需使用。

## 安全与恢复

保留本地 Bearer Key、Host/Origin 校验、一次性 state/ticket、浏览器 cookie、PKCE S256、DPoP、刷新互斥和 AES-256-GCM 加密，不得为部署方便移除 OAuth 或鉴权。

秘密存于 macOS 钥匙串。非秘密配置和加密台账位于 `~/.local/share/doubao-work-api/<安装路径指纹>/`，指纹取安装目录 SHA-256 前16位。目录权限0700，配置和数据库0600。换安装路径会改变钥匙串服务名和状态目录；新电脑应本人重新配置和授权，不能复制客户端 metadata、OAuth 凭据、数据库、浏览器身份或历史任务。不要单独删除加密主键。

服务受理消息不等于模型完成，须按返回消息 ID 与 `reply_id` 核对回复及真实产物。写入的 UUID 必须为同一逻辑请求保存并复用；结果未知或进程中断后只查询原请求，不更换 UUID 自动重发。刷新令牌结果未知时要求本人重新授权。删除本地授权不等于平台撤销。

## 接入边界

端口在 runtime 配置校验、CLI 地址、回调、OpenAPI 中均固定为8791。Q金业务客户端优先读取 `QJIN_DOUBAO_GATEWAY_PROJECT`，未设置时使用仓库 `extras/gateway/`。该变量必须指向实际安装目录，以计算相同的钥匙串指纹；仅移动本目录或设置 URL 不能解决身份变化。

独立安装时，在启动业务后端前将该变量设为网关目录的绝对路径。不扫描其他钥匙串身份。若以后支持端口配置，必须同步客户端地址、监听、回调登记、CLI 和接口合同，保持回环及来源限制。

## 验证

```bash
npm test
```

测试使用临时数据库、人工测试凭据和受控上游，通过随机回环端口测试真实 HTTP；不读取钥匙串、不调用真实 OAuth、不占用8791。测试通过不代表目标机器授权、套餐、生成能力或完整业务接入已验收。
