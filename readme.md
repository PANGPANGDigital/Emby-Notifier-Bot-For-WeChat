# Emby Notifier Bot — 企业微信应用推送版

基于 [leolitaly/emby-notifier-bot](https://hub.docker.com/r/leolitaly/emby-notifier-bot) 扩展而来。实时监控 Emby 媒体服务器事件，通过 **Telegram Bot** 和/或 **企业微信自建应用** 推送格式化通知。

## 核心特性

- **双通道推送**：Telegram 与企业微信可同时启用，互不依赖；只配置其中一个也能正常运行
- **事件去重**：基于事件 ID + TTL 缓存，避免重复推送
- **冷却机制**：播放事件 5 秒冷却、用户登录 5 秒冷却，防止通知轰炸
- **智能封面**：自动匹配剧集/电影封面，通过 Telegram 发送图片消息
- **企业微信反向代理**：支持通过 Caddy/Nginx 中转 HTTP 流量推送企业微信消息
- **失败重试**：Telegram 发送失败事件自动进入重试队列，指数退避
- **回调验签**：可选提供 `/wecom-callback` 接口完成企业微信 URL 验证

## 监控事件

| 事件 | 说明 |
|------|------|
| `playback.start` | 开始播放 |
| `playback.stop` | 停止播放 |
| `playback.pause` | 暂停播放 |
| `user.authenticated` | 用户登录 |
| `user.logout` | 用户登出 |
| `library.new` | 新增媒体 |
| `library.deleted` | 删除媒体 |
| `metadata.update` | 元数据更新 |

## 快速开始

### 环境要求

- Docker & Docker Compose
- Emby Server 4.8.0.80+（Webhook 内置到通知功能中）

### 部署步骤

1. 克隆本仓库：

```sh
git clone https://github.com/<你的用户名>/emby-notifier-bot-wecom.git
cd emby-notifier-bot-wecom
```

2. 编辑 `docker-compose.yml`，填入你的配置：

```sh
nano docker-compose.yml
```

3. 构建并启动：

```sh
docker compose up -d --build
docker compose logs -f emby-notifier-bot
```

### 群晖 Container Manager

将整个项目目录上传到群晖，编辑 `docker-compose.yml` 后，在 Container Manager 中新建“项目”并选择该目录构建。不能只上传 Compose 文件，因为镜像会从源码构建。

## 配置说明

### 企业微信（可选）

在企业微信管理后台创建自建应用，取得以下三项：

| 环境变量 | 必填 | 说明 |
|----------|------|------|
| `WECOM_CORP_ID` | 是 | 企业 ID（CorpID） |
| `WECOM_AGENT_ID` | 是 | 自建应用 AgentId |
| `WECOM_CORP_SECRET` | 是 | 自建应用 Secret |
| `WECOM_TO_USER` | 否 | 接收成员 ID，多个用 `\|` 分隔；默认 `@all` |
| `WECOM_API_URL` | 否 | 反向代理地址，如 `http://your_caddy:28395` |
| `WECOM_PROXY` | 否 | 正向代理，如 `http://proxy:7890` |
| `WECOM_TOKEN` | 否 | 仅用于回调验签 |
| `WECOM_ENCODING_AES_KEY` | 否 | 仅用于回调解密；需与 Token 同时配置 |

> 旧式变量也可用：`WECHAT_CORP_ID`、`WECHAT_AGENT_ID`、`WECHAT_CORP_SECRET`、`WECHAT_USER_ID`、`WECHAT_TOKEN`、`WECHAT_ENCODING_AES_KEY`。

### Telegram（可选）

| 环境变量 | 必填 | 说明 |
|----------|------|------|
| `TELEGRAM_BOT_TOKEN` | 是* | 通过 @BotFather 创建 |
| `TELEGRAM_CHAT_ID` | 是* | 用户或群组 ID，多个用逗号分隔，支持负数群组 ID |

\* 仅当启用 Telegram 推送时需要。

### 通用配置

| 环境变量 | 说明 |
|----------|------|
| `EMBY_SERVER_URL` | Emby 服务器地址，用于获取封面图片 |
| `EMBY_API_KEY` | Emby API 密钥，用于获取封面图片 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 全局网络代理；企业微信未指定 `WECOM_PROXY` 时会回退使用 |
| `ENABLE_COVERS` | 是否向 Telegram 发送封面，默认 `true` |
| `OVERVIEW_MAX_LENGTH` | 剧情简介最大长度，默认 `300` |
| `TZ` | 时区，示例 `Asia/Shanghai` |
| `DEBUG_MODE` | 调试日志，默认 `false` |
| `WEBHOOK_HOST` / `WEBHOOK_PORT` | 监听地址和端口，默认 `0.0.0.0:53211` |

## 企业微信反向代理模式

如果你的 Caddy/Nginx 不支持 CONNECT 隧道，可使用 HTTP 中转模式：

```yaml
# docker-compose.yml
WECOM_API_URL: "http://your_caddy:28395"
```

对应的 Caddyfile 示例：

```caddy
qyapi.weixin.qq.com:443 {
    reverse_proxy https://qyapi.weixin.qq.com {
        header_up Host qyapi.weixin.qq.com
    }
}
```

或针对具体端口：

```caddy
http://your_caddy:28395 {
    reverse_proxy https://qyapi.weixin.qq.com {
        header_up Host qyapi.weixin.qq.com
    }
}
```

## Emby Webhook 配置

在 Emby 管理后台 → 设置 → 通知 → Webhook → 添加：

- **名称**：任意（如 `企业微信通知`）
- **URL**：`http://<服务IP>:53211/emby-webhook`
- **格式**：`multipart/form-data` 或 `application/json`
- **事件**：按需选择，建议全选

## 常见问题

**Q: 企业微信消息发送失败，报 `Unknown scheme for proxy URL`？**

A: 检查 `WECOM_PROXY` 环境变量。代理地址必须以 `http://` 或 `https://` 开头，例如 `http://proxy:7890`。空字符串或不合法格式会被自动忽略。

**Q: 如何只使用企业微信，不使用 Telegram？**

A: 只配置 `WECOM_CORP_ID`、`WECOM_AGENT_ID`、`WECOM_CORP_SECRET` 三个环境变量即可，无需填写任何 Telegram 配置。

**Q: 封面图片不显示？**

A: 确保 `EMBY_SERVER_URL` 和 `EMBY_API_KEY` 已正确配置，且 Emby 服务器可从容器内访问。

**Q: 企业微信回调验签如何配置？**

A: 在企业微信管理后台设置“接收消息”回调 URL 为 `https://<公网域名>/wecom-callback`，同时配置 `WECOM_TOKEN` 和 `WECOM_ENCODING_AES_KEY`。服务会自动处理 URL 验证请求。

## 相关项目

- [leolitaly/emby-notifier-bot](https://hub.docker.com/r/leolitaly/emby-notifier-bot) — 原 Telegram 版本
- [Ccccx159/Emby_Notifier](https://github.com/Ccccx159/Emby_Notifier) — 支持 Telegram / 企业微信 / Bark

## License

MIT
