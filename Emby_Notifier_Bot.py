# APP_VERSION = "1.0.1"
# https://PeiFeng.li

import logging
import httpx
import asyncio
import os
import sys
import json
import re
import uuid
import base64
import hashlib
import hmac
import struct
import signal
import pytz
import time
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
from io import BytesIO
from asyncio import Queue, Lock
from cachetools import TTLCache
from telegram import Bot, InputFile, ParseMode
from telegram.error import TelegramError
from telegram.utils.request import Request
from aiohttp import web
from Crypto.Cipher import AES

# --- 配置类（环境变量读取）---
class Config:
    HTTP_PROXY = os.getenv('HTTP_PROXY')
    HTTPS_PROXY = os.getenv('HTTPS_PROXY')
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    # 支持多个聊天 ID，也兼容 Telegram 的负数群组 ID。
    TELEGRAM_CHAT_IDS = []
    for chat_id in os.getenv("TELEGRAM_CHAT_ID", "").split(","):
        try:
            TELEGRAM_CHAT_IDS.append(int(chat_id.strip()))
        except ValueError:
            continue

    # 企业微信应用消息。WECHAT_* 是早期项目常用别名，便于迁移。
    WECOM_CORP_ID = os.getenv("WECOM_CORP_ID") or os.getenv("WECHAT_CORP_ID")
    WECOM_AGENT_ID = os.getenv("WECOM_AGENT_ID") or os.getenv("WECOM_AGENTLD") or os.getenv("WECHAT_AGENT_ID")
    WECOM_CORP_SECRET = os.getenv("WECOM_CORP_SECRET") or os.getenv("WECHAT_CORP_SECRET")
    WECOM_TO_USER = os.getenv("WECOM_TO_USER") or os.getenv("WECHAT_USER_ID", "@all")
    WECOM_PROXY = os.getenv("WECOM_PROXY")
    # 自建反向代理地址（如 http://1.2.3.4:28395），用于 HTTP 流量中转推送企业微信消息。
    # 设置后 bot 会通过此地址发送请求并自动附加 Host: qyapi.weixin.qq.com 头。
    WECOM_API_URL = os.getenv("WECOM_API_URL")
    # Token 和 EncodingAESKey 仅供企业微信回调地址验签/解密，不用于主动发送应用消息。
    WECOM_TOKEN = os.getenv("WECOM_TOKEN") or os.getenv("WECHAT_TOKEN")
    WECOM_ENCODING_AES_KEY = os.getenv("WECOM_ENCODING_AES_KEY") or os.getenv("WECHAT_ENCODING_AES_KEY")

    @classmethod
    def telegram_configured(cls) -> bool:
        return bool(cls.TELEGRAM_BOT_TOKEN and cls.TELEGRAM_CHAT_IDS)

    @classmethod
    def wecom_configured(cls) -> bool:
        return bool(cls.WECOM_CORP_ID and cls.WECOM_CORP_SECRET and cls.WECOM_AGENT_ID)

# --- 网络代理配置 ---
class NetworkUtils:
    @staticmethod
    def _normalize_proxy(proxy: Optional[str]) -> Optional[str]:
        if not proxy:
            return None
        proxy = proxy.strip()
        if proxy.startswith(("http://", "https://")):
            return proxy
        logger.warning("忽略非法代理地址（必须以 http:// 或 https:// 开头）: %s", proxy)
        return None

    @staticmethod
    def setup_proxy() -> None:
        LAN_SUBNETS = [
            "192.168.", "10.0", "localhost", "127.0."
        ]
        
        def is_emby_lan() -> bool:
            if not EMBY_SERVER_URL:
                return False
            emby_host = EMBY_SERVER_URL.replace("http://", "").replace("https://", "").split(":")[0]
            for subnet in LAN_SUBNETS:
                if emby_host.startswith(subnet):
                    return True
            return False
        
        http_proxy = NetworkUtils._normalize_proxy(Config.HTTP_PROXY)
        https_proxy = NetworkUtils._normalize_proxy(Config.HTTPS_PROXY)
        
        if http_proxy and not is_emby_lan():
            os.environ['http_proxy'] = http_proxy
            logger.info(f"已设置HTTP代理: {http_proxy}")
        
        if https_proxy:
            os.environ['https_proxy'] = https_proxy
            logger.info(f"已设置HTTPS代理: {https_proxy}")

# --- 基础配置 ---
APP_VERSION = "1.0.1"
DEFAULT_TZ = "Europe/Rome"
USER_TZ = pytz.timezone(os.getenv('TZ', DEFAULT_TZ))
PLAY_EVENT_COOLDOWN = 5  # 播放类事件冷却时间（5秒）
USER_LOGIN_COOLDOWN = 5  # 用户登录事件冷却时间（5秒）

# --- 环境变量配置 ---
EMBY_SERVER_URL = os.getenv("EMBY_SERVER_URL")
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 53211))
EMBY_MONITOR_EVENTS = os.getenv(
    "EMBY_MONITOR_EVENTS", 
    "playback.start,playback.stop,playback.pause,user.authenticated,user.logout,library.new,library.deleted,metadata.update"
).lower().split(',')
METADATA_TRACKED_FIELDS = os.getenv(
    "METADATA_TRACKED_FIELDS",
    "Name,Overview,OriginalTitle,Tagline,OfficialRating,CustomRating,CriticRating,CommunityRating,"
    "IndexNumber,ParentIndexNumber,PremiereDate,ProductionYear,EndDate,RunTimeTicks,Tags,Genres,"
    "Studios,ProductionLocations,ProviderIds"
).lower().split(',')
ENABLE_COVERS = os.getenv("ENABLE_COVERS", "True").lower() in ("true", "1", "yes")
OVERVIEW_MAX_LENGTH = int(os.getenv("OVERVIEW_MAX_LENGTH", 150))
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() in ("true", "1", "yes")
RATING_DECIMAL_PLACES = int(os.getenv("RATING_DECIMAL_PLACES", 1))

# --- 字段名称映射 ---
METADATA_FIELD_MAP = {
    "name": "名称", "overview": "剧情简介", "originaltitle": "原始标题", "tagline": "宣传语",
    "officialrating": "官方评级", "customrating": "自定义评级", "criticrating": "影评人评分",
    "communityrating": "社区评分", "indexnumber": "集数", "parentindexnumber": "季数",
    "premieredate": "首映日期", "productionyear": "制作年份", "enddate": "完结日期",
    "runtimeticks": "时长", "tags": "标签", "genres": "类型", "studios": "制作公司",
    "productionlocations": "拍摄地点", "providerids": "外部ID（TMDB/IMDB等）"
}

# 事件类型到中文描述的映射
EVENT_NAME_MAP = {
    "playback.start": "开始播放",
    "playback.stop": "停止播放",
    "playback.pause": "暂停播放",
    "user.authenticated": "用户登录",
    "user.logout": "用户登出",
    "library.new": "新增媒体",
    "library.deleted": "删除媒体",
    "metadata.update": "元数据更新"
}

# --- 全局HTTP客户端 ---
class HTTPClients:
    def __init__(self):
        proxy_url = (NetworkUtils._normalize_proxy(Config.HTTPS_PROXY) or
                     NetworkUtils._normalize_proxy(Config.HTTP_PROXY))
        
        self.check_client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            headers={"User-Agent": f"EmbyNotifier/HealthCheck/{APP_VERSION}"},
            follow_redirects=True,
            proxies=proxy_url or {},
        )
        self.send_client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
            headers={"User-Agent": f"EmbyNotifier/Sender/{APP_VERSION}"},
            follow_redirects=True,
            proxies=proxy_url or {},
        )

    async def close(self):
        await self.check_client.aclose()
        await self.send_client.aclose()

http_clients = HTTPClients()

# --- 带统计功能的缓存 ---
class TrackedTTLCache(TTLCache):
    def __init__(self, maxsize, ttl, timer=time.monotonic, cache_name: str = "unknown"):
        super().__init__(maxsize, ttl, timer)
        self.hits = 0
        self.misses = 0
        self.cache_name = cache_name

    def __getitem__(self, key):
        try:
            result = super().__getitem__(key)
            self.hits += 1
            return result
        except KeyError:
            self.misses += 1
            raise

    def get_stats(self):
        total = self.hits + self.misses
        hit_rate = self.hits / total if total > 0 else 0.0
        return {
            "hits": self.hits, "misses": self.misses, "hit_rate": round(hit_rate, 3),
            "current_size": len(self), "maxsize": self.maxsize, "cache_name": self.cache_name
        }

# --- 核心缓存机制 ---
sent_events_cache = TrackedTTLCache(maxsize=1000, ttl=86400, cache_name="sent_events")
sent_events_lock = Lock()

event_message_id_cache = TrackedTTLCache(maxsize=1000, ttl=604800, cache_name="event_message_ids")
event_message_id_lock = Lock()

play_event_cooldown_cache = TrackedTTLCache(maxsize=500, ttl=PLAY_EVENT_COOLDOWN, cache_name="play_event_cooldown")
play_event_cooldown_lock = Lock()

user_login_cooldown_cache = TrackedTTLCache(maxsize=200, ttl=USER_LOGIN_COOLDOWN, cache_name="user_login_cooldown")
user_login_cooldown_lock = Lock()

media_info_cache = TrackedTTLCache(maxsize=200, ttl=3600, cache_name="media_info")
media_info_lock = Lock()

image_cache = TrackedTTLCache(maxsize=50, ttl=3600, cache_name="image")
image_lock = Lock()

failed_events_queue = Queue(maxsize=500)

# --- 日志配置 ---
log_level = logging.DEBUG if DEBUG_MODE else logging.INFO
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=log_level,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
logging.getLogger('httpx').setLevel(logging.WARNING)

if not Config.telegram_configured() and not Config.wecom_configured():
    raise RuntimeError(
        "至少配置一个通知通道：Telegram（TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID）或"
        "企业微信（WECOM_CORP_ID、WECOM_AGENT_ID、WECOM_CORP_SECRET）。"
    )
if not EMBY_SERVER_URL:
    logger.warning("EMBY_SERVER_URL 未设置，图片功能将不可用")
if (Config.WECOM_TOKEN is None) != (Config.WECOM_ENCODING_AES_KEY is None):
    logger.warning("企业微信回调验签需同时设置 WECOM_TOKEN 和 WECOM_ENCODING_AES_KEY；未启用回调地址")

def custom_time(*args):
    return datetime.now(USER_TZ).timetuple()
logging.Formatter.converter = custom_time

# --- Telegram Bot 实例 ---
telegram_bot = None
telegram_enabled = False

# --- Emoji 映射工具函数 ---
def get_emoji(key: str) -> str:
    """获取对应类型的emoji符号"""
    emoji_map = {
        # 事件类型
        "playback": " 🎬 ",
        "user": " 👤 ",
        "library.new": " 📥 ",
        "library.deleted": " 🗑️ ",
        "metadata.update": " 🔄 ",
        "startup": " 🚀 ",
        "error": " ⚠️ ",
        
        # 媒体类型
        "movie": " 🎬 ",
        "episode": " 📺 ",
        "series": " 📺 ",
        "season": " 📼 ",
        "season_range": " 📼 ",
        "music": " 🎵 ",
        "audio": " 🎵 ",
        "album": " 🎵 ",
        "folder": " 📁 ",
        "boxset": " 📦 ",
        
        # 信息类别
        "user": " 👤 ",
        "device": " 📱 ",
        "ip": " 🌐 ",
        "time": " ⏰ ",
        "progress": " 📊 ",
        "overview": " 📝 ",
        "info": " 🔍 ",
        
        # 评分专用Emoji（按区间区分）
        "rating_perfect": " 💯 ",  # 9.0+ 高分/满分
        "rating_good": " 🌟 ",     # 7.0-8.9 中等偏上
        "rating_low": " 📉 ",      # 5.1-6.9 低分
        "rating_poor": " 💔 ",     # 0-5.0 极差
        "rating_default": " ⭐ ",   # 无评分时默认
    }
    return emoji_map.get(key.lower(), " 🔍 ")

# --- 辅助函数：处理HTML标签 ---
def process_html_tags(text: Optional[str]) -> str:
    if not text:
        return ""
    text = re.sub(r'<a\s+[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', r'\2 (\1)', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n+', '\n', text).strip()
    return text

# --- 季集范围提取 ---
def extract_episode_range(file_name_or_metadata: str) -> str:
    if not file_name_or_metadata:
        return ""
    season_pattern = r'(S\d{1,3}|第\d{1,3}季)\s*((?:[Ee]\d{1,3}[-–]\s*[Ee]\d{1,3}|[Ee]\d{1,3})(?:\s*[,，]\s*(?:[Ee]\d{1,3}[-–]\s*[Ee]\d{1,3}|[Ee]\d{1,3}))*)'
    season_matches = re.findall(season_pattern, file_name_or_metadata, re.IGNORECASE)
    if not season_matches:
        return ""

    season_episodes = {}
    for season_part, episodes_str in season_matches:
        season_num_match = re.search(r'(\d{1,3})', season_part)
        if not season_num_match:
            continue
        season_num = int(season_num_match.group(1))
        standard_season = f"S{season_num:02d}"

        episodes_str = episodes_str.replace('，', ',')
        episode_fragments = [frag.strip() for frag in episodes_str.split(',') if frag.strip()]
        if not episode_fragments:
            continue

        for frag in episode_fragments:
            if "-" in frag or "–" in frag:
                ep_numbers = re.findall(r'[Ee](\d{1,3})', frag)
                if len(ep_numbers) >= 2:
                    try:
                        start_ep, end_ep = sorted(map(int, ep_numbers[:2]))
                        for ep in range(start_ep, end_ep + 1):
                            ep_code = f"E{ep:02d}"
                            if standard_season not in season_episodes:
                                season_episodes[standard_season] = set()
                            season_episodes[standard_season].add(ep_code)
                    except ValueError:
                        continue
            else:
                ep_num_match = re.search(r'[Ee](\d{1,3})', frag)
                if ep_num_match:
                    try:
                        ep_num = int(ep_num_match.group(1))
                        ep_code = f"E{ep_num:02d}"
                        if standard_season not in season_episodes:
                            season_episodes[standard_season] = set()
                        season_episodes[standard_season].add(ep_code)
                    except ValueError:
                        continue

    final_ranges = []
    for season in sorted(season_episodes.keys()):
        eps = sorted(season_episodes[season])
        if not eps:
            continue

        merged_eps = []
        current_start = eps[0]
        current_end = eps[0]

        for ep in eps[1:]:
            current_end_num = int(current_end[1:])
            ep_num = int(ep[1:])
            if ep_num == current_end_num + 1:
                current_end = ep
            else:
                merged_eps.append(f"{current_start}-{current_end}" if current_start != current_end else current_start)
                current_start = ep
                current_end = ep
        merged_eps.append(f"{current_start}-{current_end}" if current_start != current_end else current_start)

        season_range = f"{season} {' , '.join(merged_eps)}"
        final_ranges.append(season_range)

    return " / ".join(final_ranges)

# --- 网络连通性检测 ---
async def check_telegram_connectivity() -> bool:
    check_urls = ["https://api.telegram.org", "https://api.telegram.org/bot"]
    max_attempts = 2
    
    for url in check_urls:
        for attempt in range(max_attempts):
            try:
                response = await http_clients.check_client.head(url)
                if response.status_code in (200, 404):
                    return True
            except (httpx.ConnectError, httpx.TimeoutException):
                if attempt < max_attempts - 1:
                    await asyncio.sleep(1)
                    continue
            except Exception:
                continue
    return False


# --- 企业微信应用消息 ---
class WeComClient:
    """企业微信 access_token 缓存和应用消息发送。"""

    DEFAULT_API_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
    TOKEN_RETRY_CODES = {40001, 40014, 42001}

    def __init__(self):
        # 支持自建反向代理：通过 WECOM_API_URL 指定 HTTP 中转地址，bot 发送 HTTP 请求并
        # 附加 Host 头，由 Caddy/Nginx 等反向代理转发到企业微信 API。
        if Config.WECOM_API_URL:
            self.api_base = Config.WECOM_API_URL.rstrip('/') + "/cgi-bin"
            # 反向代理模式：目标为 HTTP 时设置 Host 头确保路由正确；不走正向代理。
            headers = {
                "User-Agent": f"EmbyNotifier/WeCom/{APP_VERSION}",
                "Host": "qyapi.weixin.qq.com",
            }
            proxy = None
        else:
            self.api_base = self.DEFAULT_API_BASE
            headers = {"User-Agent": f"EmbyNotifier/WeCom/{APP_VERSION}"}
            # 企业微信为国内服务，仅在显式配置 WECOM_PROXY 时使用正向代理，不回退到全局代理。
            proxy = NetworkUtils._normalize_proxy(Config.WECOM_PROXY)

        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers=headers,
            proxies=proxy or {},
            follow_redirects=not bool(Config.WECOM_API_URL),
        )
        self.access_token: Optional[str] = None
        self.expires_at = 0.0
        self.token_lock = Lock()

    async def close(self) -> None:
        await self.client.aclose()

    async def get_access_token(self, force_refresh: bool = False) -> str:
        if not Config.wecom_configured():
            raise RuntimeError("企业微信应用参数未完整配置")

        async with self.token_lock:
            if not force_refresh and self.access_token and time.time() < self.expires_at:
                return self.access_token

            response = await self.client.get(
                f"{self.api_base}/gettoken",
                params={"corpid": Config.WECOM_CORP_ID, "corpsecret": Config.WECOM_CORP_SECRET},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errcode") != 0:
                raise RuntimeError(f"获取企业微信 access_token 失败：{payload.get('errcode')} {payload.get('errmsg')}")

            self.access_token = payload["access_token"]
            self.expires_at = time.time() + max(int(payload.get("expires_in", 7200)) - 120, 60)
            return self.access_token

    async def send_text(self, content: str) -> bool:
        """发送企业微信文本消息，网络错误与短暂 API 错误会重试三次。"""
        if not Config.wecom_configured():
            return False

        while len(content.encode("utf-8")) > 2048:
            content = content[:-1]
        return await self._send_message({
            "touser": Config.WECOM_TO_USER,
            "msgtype": "text",
            "agentid": int(Config.WECOM_AGENT_ID),
            "text": {"content": content},
            "safe": 0,
        })

    async def send_news(self, title: str, description: str, thumb_url: str, url: str = "") -> bool:
        """发送企业微信图文消息，带缩略图。"""
        if not Config.wecom_configured():
            return False

        title = title[:128]
        while len(title.encode("utf-8")) > 128:
            title = title[:-1]

        description = description[:512]
        while len(description.encode("utf-8")) > 512:
            description = description[:-1]

        return await self._send_message({
            "touser": Config.WECOM_TO_USER,
            "msgtype": "news",
            "agentid": int(Config.WECOM_AGENT_ID),
            "news": {
                "articles": [
                    {
                        "title": title,
                        "description": description,
                        "thumb_url": thumb_url,
                        "url": url,
                    }
                ]
            },
            "safe": 0,
        })

    async def _send_message(self, payload: dict) -> bool:
        """统一发送逻辑，自动处理 access_token 和重试。"""
        for attempt in range(3):
            try:
                token = await self.get_access_token(force_refresh=False)
                response = await self.client.post(
                    f"{self.api_base}/message/send",
                    params={"access_token": token},
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
                if result.get("errcode") == 0:
                    msgtype = payload.get("msgtype", "unknown")
                    logger.debug("企业微信%s消息发送成功", msgtype)
                    return True
                if result.get("errcode") in self.TOKEN_RETRY_CODES and attempt == 0:
                    await self.get_access_token(force_refresh=True)
                    continue
                logger.error("企业微信消息发送失败：%s %s", result.get("errcode"), result.get("errmsg"))
                return False
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                if attempt == 2:
                    logger.error("企业微信消息发送异常：%s", exc)
                    return False
                await asyncio.sleep(1 + attempt)
        return False


wecom_client = WeComClient() if Config.wecom_configured() else None


async def send_wecom_message(text: str, image_url: Optional[str] = None) -> bool:
    if not wecom_client:
        return False
    timestamp = datetime.now(USER_TZ).strftime('%Y-%m-%d %H:%M:%S')
    full_text = f"{text}\n\n{get_emoji('time')} 时间: {timestamp}"

    if image_url:
        lines = text.split('\n')
        title = next((line for line in lines if line.strip()), text)
        title = title[:128]
        while len(title.encode("utf-8")) > 128:
            title = title[:-1]
        return await wecom_client.send_news(
            title=title,
            description=full_text,
            thumb_url=image_url,
        )
    return await wecom_client.send_text(full_text)


def verify_wecom_signature(signature: str, timestamp: str, nonce: str, encrypted: str) -> bool:
    if not Config.WECOM_TOKEN:
        return False
    raw = "".join(sorted([Config.WECOM_TOKEN, timestamp, nonce, encrypted]))
    expected = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def decrypt_wecom_echo(encrypted: str) -> str:
    """解密企业微信 URL 验证请求中的 echostr。"""
    if not Config.WECOM_ENCODING_AES_KEY or not Config.WECOM_CORP_ID:
        raise ValueError("企业微信回调参数未配置")
    aes_key = base64.b64decode(Config.WECOM_ENCODING_AES_KEY + "=")
    cipher = AES.new(aes_key, AES.MODE_CBC, aes_key[:16])
    data = cipher.decrypt(base64.b64decode(encrypted))
    padding = data[-1]
    if padding < 1 or padding > 32:
        raise ValueError("企业微信回调填充无效")
    data = data[:-padding]
    message_length = struct.unpack("!I", data[16:20])[0]
    message = data[20:20 + message_length]
    received_corp_id = data[20 + message_length:].decode("utf-8")
    if received_corp_id != Config.WECOM_CORP_ID:
        raise ValueError("企业微信回调 CorpID 不匹配")
    return message.decode("utf-8")


async def wecom_callback_handler(request: web.Request) -> web.Response:
    """可选的企业微信回调地址，仅用于完成平台验证和忽略入站消息。"""
    signature = request.query.get("msg_signature", "")
    timestamp = request.query.get("timestamp", "")
    nonce = request.query.get("nonce", "")
    encrypted = request.query.get("echostr", "")
    if not verify_wecom_signature(signature, timestamp, nonce, encrypted):
        return web.Response(text="invalid signature", status=403)
    if request.method == "GET":
        try:
            return web.Response(text=decrypt_wecom_echo(encrypted))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.warning("企业微信回调验证失败：%s", exc)
            return web.Response(text="invalid callback", status=400)
    return web.Response(text="success")

# --- Telegram Bot 初始化 ---
async def initialize_telegram_bot():
    global telegram_bot, telegram_enabled
    if not Config.telegram_configured():
        logger.info("未配置 Telegram，跳过 Telegram 初始化")
        return
    retry_count = 0
    max_retries = 3
    retry_delay = 10
    
    while retry_count < max_retries:
        try:
            # 配置代理
            if Config.HTTP_PROXY or Config.HTTPS_PROXY:
                request = Request(
                    proxy_url=Config.HTTPS_PROXY or Config.HTTP_PROXY,
                    connect_timeout=60,
                    read_timeout=60,
                )
            else:
                request = Request()
            
            # 创建Bot实例
            telegram_bot = Bot(
                token=Config.TELEGRAM_BOT_TOKEN,
                request=request,
            )
            
            # 测试连接
            bot_info = await telegram_bot.get_me()
            telegram_enabled = True
            logger.info(f"Telegram Bot连接成功，Bot名称: {bot_info.first_name} (@{bot_info.username})")
            return
        except Exception as e:
            retry_count += 1
            logger.warning(f"Telegram Bot初始化失败（第{retry_count}/{max_retries}次）: {str(e)}")
            if retry_count < max_retries:
                await asyncio.sleep(retry_delay)
    
    # 所有重试都失败后的处理
    telegram_enabled = False
    logger.error("Telegram Bot初始化多次失败，将在运行中继续重试")
    asyncio.create_task(periodic_bot_reconnect())

async def periodic_bot_reconnect():
    global telegram_bot, telegram_enabled
    while True:
        if not telegram_enabled:
            try:
                logger.info("尝试重新连接Telegram Bot...")
                if Config.HTTP_PROXY or Config.HTTPS_PROXY:
                    request = Request(
                        proxy_url=Config.HTTPS_PROXY or Config.HTTP_PROXY,
                        connect_timeout=60,
                        read_timeout=60,
                    )
                else:
                    request = Request()
                
                telegram_bot = Bot(
                    token=Config.TELEGRAM_BOT_TOKEN,
                    request=request,
                )
                await telegram_bot.get_me()
                telegram_enabled = True
                logger.info("Telegram Bot重新连接成功")
            except Exception as e:
                logger.warning(f"Telegram Bot重连失败: {str(e)}")
        await asyncio.sleep(300)

# --- 缓存操作工具函数 ---
async def is_event_sent(event_key: str) -> bool:
    async with sent_events_lock:
        return event_key in sent_events_cache

async def mark_event_sent(event_key: str) -> None:
    async with sent_events_lock:
        sent_events_cache[event_key] = True

async def unmark_event_sent(event_key: str) -> None:
    async with sent_events_lock:
        if event_key in sent_events_cache:
            del sent_events_cache[event_key]

async def get_cached_message_id(event_key: str) -> Optional[int]:
    async with event_message_id_lock:
        return event_message_id_cache.get(event_key)

async def cache_message_id(event_key: str, message_id: int) -> None:
    async with event_message_id_lock:
        event_message_id_cache[event_key] = message_id

async def remove_cached_message_id(event_key: str) -> None:
    async with event_message_id_lock:
        if event_key in event_message_id_cache:
            del event_message_id_cache[event_key]

async def is_play_event_in_cooldown(item_id: str, event_type: str) -> bool:
    cache_key = f"{item_id}_{event_type}"
    async with play_event_cooldown_lock:
        return cache_key in play_event_cooldown_cache

async def mark_play_event_cooldown(item_id: str, event_type: str) -> None:
    cache_key = f"{item_id}_{event_type}"
    async with play_event_cooldown_lock:
        play_event_cooldown_cache[cache_key] = time.time()

async def is_user_login_in_cooldown(user_id: str, event_type: str) -> bool:
    cache_key = f"{user_id}_{event_type}"
    async with user_login_cooldown_lock:
        return cache_key in user_login_cooldown_cache

async def mark_user_login_cooldown(user_id: str, event_type: str) -> None:
    cache_key = f"{user_id}_{event_type}"
    async with user_login_cooldown_lock:
        user_login_cooldown_cache[cache_key] = time.time()

async def check_and_add_cache(cache: TrackedTTLCache, lock: Lock, key: str, value: Any = True, timeout: float = 3.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with lock:
                if key in cache:
                    return True
                cache[key] = value
                return False
    except asyncio.TimeoutError:
        logger.warning(f"获取缓存锁超时，键: {key}")
        return False

async def get_cache_value(cache: TrackedTTLCache, lock: Lock, key: str, timeout: float = 3.0) -> Optional[Any]:
    try:
        async with asyncio.timeout(timeout):
            async with lock:
                return cache.get(key)
    except asyncio.TimeoutError:
        logger.warning(f"获取缓存值超时，键: {key}")
        return None

async def set_cache_value(cache: TrackedTTLCache, lock: Lock, key: str, value: Any, timeout: float = 3.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            async with lock:
                cache[key] = value
                return True
    except asyncio.TimeoutError:
        logger.warning(f"设置缓存值超时，键: {key}")
        return False

async def clean_expired_cache():
    while True:
        sent_events_cache.expire()
        event_message_id_cache.expire()
        play_event_cooldown_cache.expire()
        user_login_cooldown_cache.expire()
        await asyncio.sleep(3600)  # 每小时清理一次

# --- 事件键生成 ---
def get_event_key(event_data: Dict) -> str:
    event_id = event_data.get("Id") or event_data.get("EventId") or event_data.get("NotificationId")
    if event_id:
        return f"emby_id_{event_id}"
    
    event_type = event_data.get("Event", "").lower()
    event_time = event_data.get("Timestamp") or datetime.now().timestamp()
    time_key = str(int(float(event_time) * 1000))
    
    if event_type in ["library.new", "library.deleted", "metadata.update"]:
        item_id = event_data.get("Item", {}).get("Id") or event_data.get("ItemId")
        return f"{event_type}_item_{item_id}_t_{time_key}"
    
    elif event_type.startswith("playback"):
        item_id = event_data.get("Item", {}).get("Id")
        user_id = event_data.get("User", {}).get("Id")
        return f"{event_type}_user_{user_id}_item_{item_id}_t_{time_key}"
    
    elif event_type in ["user.authenticated", "user.logout"]:
        user_id = event_data.get("User", {}).get("Id")
        return f"{event_type}_user_{user_id}_t_{time_key}"
    
    return f"fallback_{uuid.uuid4().hex}_t_{time_key}"

def is_valid_event(data: Dict) -> Tuple[bool, str]:
    event_type = data.get("Event", "").lower()
    if not event_type:
        return False, "缺少Event字段"
    
    if event_type in ["library.new", "library.deleted", "metadata.update"]:
        if not data.get("Item") and not data.get("ItemId"):
            return False, f"事件{event_type}缺少Item或ItemId"
    
    elif event_type.startswith("playback"):
        if not data.get("Item") or not data.get("User"):
            return False, f"播放事件{event_type}缺少Item或User"
    
    elif event_type in ["user.authenticated", "user.logout"]:
        if not data.get("User"):
            return False, f"用户事件{event_type}缺少User"
    
    return True, "有效数据"

def get_emby_image_url(item: Dict) -> Optional[str]:
    if not EMBY_SERVER_URL:
        return None
    
    item_id = item.get("Id")
    if not item_id:
        return None
    
    image_types = ["Backdrop", "Fanart", "Primary"]
    item_type = item.get("Type", "")
    
    if item_type == "Episode":
        series_id = item.get("SeriesId")
        series_image_tags = item.get("SeriesImageTags", {})
        if series_id and "Primary" in series_image_tags:
            image_url = f"{EMBY_SERVER_URL.rstrip('/')}/Items/{series_id}/Images/Primary"
            params = [f"tag={series_image_tags['Primary']}", "quality=90", "maxWidth=1200"]
            if EMBY_API_KEY:
                params.append(f"api_key={EMBY_API_KEY}")
            return f"{image_url}?{'&'.join(params)}"
    
    if item_type in ["Music", "Audio"]:
        album_id = item.get("AlbumId")
        album_image_tags = item.get("AlbumImageTags", {})
        if album_id and "Primary" in album_image_tags:
            image_url = f"{EMBY_SERVER_URL.rstrip('/')}/Items/{album_id}/Images/Primary"
            params = [f"tag={album_image_tags['Primary']}", "quality=90", "maxWidth=1200"]
            if EMBY_API_KEY:
                params.append(f"api_key={EMBY_API_KEY}")
            return f"{image_url}?{'&'.join(params)}"
    
    backdrop_tags = item.get("BackdropImageTags", [])
    if backdrop_tags:
        image_url = f"{EMBY_SERVER_URL.rstrip('/')}/Items/{item_id}/Images/Backdrop"
        params = [f"tag={backdrop_tags[0]}", "quality=90", "maxWidth=1200"]
        if EMBY_API_KEY:
            params.append(f"api_key={EMBY_API_KEY}")
        return f"{image_url}?{'&'.join(params)}"
    
    item_image_tags = item.get("ImageTags", {})
    for image_type in image_types:
        if image_type in item_image_tags:
            image_url = f"{EMBY_SERVER_URL.rstrip('/')}/Items/{item_id}/Images/{image_type}"
            params = [f"tag={item_image_tags[image_type]}", "quality=90", "maxWidth=1200"]
            if EMBY_API_KEY:
                params.append(f"api_key={EMBY_API_KEY}")
            return f"{image_url}?{'&'.join(params)}"
    
    default_url = f"{EMBY_SERVER_URL.rstrip('/')}/Items/{item_id}/Images/Primary"
    params = ["quality=90", "maxWidth=800"]
    if EMBY_API_KEY:
        params.append(f"api_key={EMBY_API_KEY}")
    return f"{default_url}?{'&'.join(params)}"

def ticks_to_time(ticks: int) -> str:
    if not ticks:
        return "00:00:00"
    seconds = int(ticks / 10_000_000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def format_media_info(item: Dict, event_type: str = "") -> str:
    media_type_map = {
        "Movie": "电影", "Episode": "剧集", "Series": "剧集", "Season": "季",
        "Music": "音乐", "Audio": "音频", "Album": "专辑", "Folder": "文件夹", "BoxSet": "合集"
    }
    
    original_media_type = item.get("Type", "").lower()
    media_emoji = get_emoji(original_media_type)
    media_type = media_type_map.get(item.get("Type"), item.get("Type", "媒体"))
    year = item.get("ProductionYear")
    name = item.get("Name", "未知名称")
    
    name_parts = [name]
    if year:
        name_parts.append(f"({year})")
    full_name = " ".join(name_parts)
    
    if media_type == "剧集":
        series_name = item.get("SeriesName", "未知剧集")
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        
        info = [f"{media_emoji} 【{media_type}】{series_name}"]
        if season is not None and episode is not None:
            info.append(f" · S{season:02d}E{episode:02d} · {full_name}")
        else:
            info.append(f" · {full_name}")
    else:
        info = [f"{media_emoji} 【{media_type}】{full_name}"]
    
    return "".join(info)

# --- 元数据变更解析 ---
def parse_updated_fields(description: str) -> List[str]:
    if not description:
        return []
    
    raw_fields = re.split(r'[,;，；\s]+', description.lower())
    raw_fields = [f.strip() for f in raw_fields if f.strip()]
    
    tracked_fields = [f for f in raw_fields if f in METADATA_TRACKED_FIELDS]
    chinese_fields = [METADATA_FIELD_MAP.get(f, f) for f in tracked_fields]
    
    if len(chinese_fields) > 5:
        return chinese_fields[:5] + ["..."]
    return chinese_fields

# --- 事件格式化 ---
def format_library_event(event_data: Dict, item: Dict, event_type: str) -> str:
    event_map = {
        "library.new": "⭐ 媒体库更新 | 新增内容 ⭐",
        "library.deleted": "⭐ 媒体库更新 | 内容删除 ⭐",
        "metadata.update": "⭐ 媒体库更新 | 元数据刷新 ⭐"
    }
    event_name = event_map.get(event_type, f"⭐ {event_type} ⭐")
    
    media_type_map = {
        "Movie": "电影", "Episode": "剧集", "Series": "剧集", "Season": "季",
        "Music": "音乐", "Audio": "音频", "Album": "专辑", "Folder": "文件夹", "BoxSet": "合集"
    }
    
    original_media_type = item.get("Type", "").lower()
    media_emoji = get_emoji(original_media_type)
    media_type = media_type_map.get(item.get("Type"), item.get("Type", "未知媒体"))
    name = item.get("Name", "未知名称")
    year = item.get("ProductionYear")
    series_name = item.get("SeriesName")
    album_name = item.get("Album")
    
    description = process_html_tags(event_data.get("Description", ""))
    file_path = item.get("Path", "")
    episode_range = extract_episode_range(f"{description} {file_path}")
    
    base_name_parts = [name]
    if year:
        base_name_parts.append(f"({year})")
    base_name = " ".join(base_name_parts)
    
    info = [event_name, ""]
    if media_type == "剧集":
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        
        episode_info = []
        if series_name:
            episode_info.append(f"{media_emoji} 【剧集】{series_name}")
        if season is not None and episode is not None:
            episode_info.append(f"S{season:02d}E{episode:02d} · {base_name}")
        else:
            episode_info.append(f"{media_emoji} 【剧集】{base_name}")
        
        info.append(" ".join(episode_info))
    else:
        info.append(f"{media_emoji} 【{media_type}】{base_name}")
    
    info.append("——————")
    
    if event_type == "library.new":
        community_rating = item.get("CommunityRating")
        if community_rating is not None:
            if community_rating >= 9.0:
                emoji_key = "rating_perfect"
            elif 7.0 <= community_rating < 9.0:
                emoji_key = "rating_good"
            elif 5.1 <= community_rating < 7.0:
                emoji_key = "rating_low"
            else:
                emoji_key = "rating_poor"
            
            rating_format = f"%.{RATING_DECIMAL_PLACES}f"
            info.append(f"{get_emoji(emoji_key)} 社区评分: {rating_format % community_rating}/10")
        else:
            info.append(f"{get_emoji('rating_default')} 社区评分: 暂无")
    
    if episode_range:
        info.append(f"{get_emoji('season_range')} 季集范围: {episode_range}")
    
    tmdb_id_match = re.search(r'TmdbId:\s*(\d+)', description)
    if tmdb_id_match:
        info.append(f"{get_emoji('info')} TMDB id: 「{tmdb_id_match.group(1)}」")
    
    if series_name and media_type != "剧集":
        info.append(f"{get_emoji('info')} 所属系列: {series_name}")
    elif album_name:
        info.append(f"{get_emoji('info')} 所属专辑: {album_name}")
    
    if event_type == "metadata.update":
        updated_fields = parse_updated_fields(description)
        if updated_fields:
            info.append("")
            info.append(f"🔄 已更新字段:")
            info.append(f"   • {', '.join(updated_fields)}")
    
    overview = process_html_tags(item.get("Overview") or item.get("Plot") or "")
    if len(overview) > OVERVIEW_MAX_LENGTH:
        overview = overview[:OVERVIEW_MAX_LENGTH] + "..."
    if overview:
        info.append("")
        info.append(f"{get_emoji('overview')} 剧情:\n{overview}")
    
    return "\n".join(info)

def format_user_event(user: Dict, event_type: str, device_name: str, ip_address: str, client_name: str) -> str:
    action = "登录成功" if event_type in ["user.login", "user.authenticated"] else "已登出"
    title = f"⭐ 用户操作 | {action} ⭐"
    user_name = user.get("Name", "未知用户")
    
    info = [title, ""]
    info.append(f"{get_emoji('user')} 用户名: {user_name}")
    info.append("——————")
    
    info.append(f"{get_emoji('device')} 设备: {device_name}")
    info.append(f"{get_emoji('ip')} IP: {ip_address}")
    info.append(f"{get_emoji('info')} 客户端: {client_name}")
    
    if action == "登录成功":
        last_active = user.get("LastActivityDate")
        if last_active:
            try:
                utc_time = datetime.fromisoformat(last_active.replace('Z', '+00:00'))
                local_time = utc_time.astimezone(USER_TZ)
                info.append(f"{get_emoji('time')} 最后活动: {local_time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception:
                pass
    
    return "\n".join(info)

# --- 核心消息发送函数（支持多聊天ID）---
async def atomic_send_telegram_message_single_chat(text: str, event_key: str, chat_id: int) -> Optional[int]:
    if not telegram_enabled or not telegram_bot:
        logger.debug(f"[发送取消] Telegram未启用，键: {event_key}，chat_id: {chat_id}")
        return None

    if await is_event_sent(event_key):
        logger.debug(f"[重复拦截] 事件已标记为发送，键: {event_key}，chat_id: {chat_id}")
        return await get_cached_message_id(event_key)
    
    cached_msg_id = await get_cached_message_id(event_key)
    if cached_msg_id is not None:
        logger.debug(f"[重复拦截] 已有消息ID: {cached_msg_id}，键: {event_key}，chat_id: {chat_id}")
        return cached_msg_id

    try:
        current_time = datetime.now(USER_TZ).strftime('%Y-%m-%d %H:%M:%S')
        full_text = f"{text}\n\n{get_emoji('time')} 时间: {current_time}"
        
        logger.debug(f"[发送文本] 键: {event_key}，chat_id: {chat_id}")
        message = await telegram_bot.send_message(
            chat_id=chat_id,
            text=full_text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        msg_id = message.message_id

        try:
            await cache_message_id(event_key, msg_id)
            await mark_event_sent(event_key)
            logger.debug(f"[发送成功] 文本消息ID: {msg_id}，键: {event_key}，chat_id: {chat_id}")
        except Exception as cache_err:
            logger.warning(f"[发送成功但缓存失败] 文本消息ID: {msg_id}，键: {event_key}，chat_id: {chat_id}，错误: {str(cache_err)}")
        
        return msg_id

    except Exception as e:
        logger.error(f"[发送失败] 文本发送异常: {str(e)}，键: {event_key}，chat_id: {chat_id}")
        await unmark_event_sent(event_key)
        await remove_cached_message_id(event_key)
        return None


async def atomic_send_telegram_photo_single_chat(photo_url: str, caption: str, event_key: str, chat_id: int) -> Optional[int]:
    if not telegram_enabled or not telegram_bot:
        logger.debug(f"[发送取消] Telegram未启用，键: {event_key}，chat_id: {chat_id}")
        return None

    if await is_event_sent(event_key):
        logger.debug(f"[重复拦截] 事件已标记为发送，键: {event_key}，chat_id: {chat_id}")
        return await get_cached_message_id(event_key)
    
    cached_msg_id = await get_cached_message_id(event_key)
    if cached_msg_id is not None:
        logger.debug(f"[重复拦截] 已有消息ID: {cached_msg_id}，键: {event_key}，chat_id: {chat_id}")
        return cached_msg_id

    try:
        response = await http_clients.send_client.get(photo_url)
        response.raise_for_status()
        
        if len(response.content) > 10 * 1024 * 1024:
            logger.warning(f"[图片过大] 降级为文本，键: {event_key}，chat_id: {chat_id}")
            return await atomic_send_telegram_message_single_chat(caption, event_key, chat_id)
            
        photo_data = BytesIO(response.content)
        photo_data.name = "emby_cover.jpg"

        current_time = datetime.now(USER_TZ).strftime('%Y-%m-%d %H:%M:%S')
        full_caption = f"{caption}\n\n{get_emoji('time')} 时间: {current_time}"
        
        logger.debug(f"[发送图片] 键: {event_key}，chat_id: {chat_id}")
        message = await telegram_bot.send_photo(
            chat_id=chat_id,
            photo=InputFile(photo_data),
            caption=full_caption,
            parse_mode=ParseMode.MARKDOWN
        )
        msg_id = message.message_id

        try:
            await cache_message_id(event_key, msg_id)
            await mark_event_sent(event_key)
            logger.debug(f"[发送成功] 图片消息ID: {msg_id}，键: {event_key}，chat_id: {chat_id}")
        except Exception as cache_err:
            logger.warning(f"[发送成功但缓存失败] 图片消息ID: {msg_id}，键: {event_key}，chat_id: {chat_id}，错误: {str(cache_err)}")
        
        return msg_id

    except Exception as e:
        logger.error(f"[发送失败] 图片发送异常: {str(e)}，键: {event_key}，chat_id: {chat_id}")
        await unmark_event_sent(event_key)
        await remove_cached_message_id(event_key)
        return None

# --- 通知发送函数（多聊天ID入口）---
async def send_telegram_message(text: str, event_key: str) -> Dict[int, Optional[int]]:
    """向所有配置的聊天ID发送文本消息，返回{chat_id: message_id}"""
    results = {}
    for chat_id in Config.TELEGRAM_CHAT_IDS:
        sub_event_key = f"{event_key}_chat_{chat_id}"
        msg_id = await atomic_send_telegram_message_single_chat(text, sub_event_key, chat_id)
        results[chat_id] = msg_id
    return results

async def send_telegram_photo(photo_url: str, caption: str, event_key: str) -> Dict[int, Optional[int]]:
    """向所有配置的聊天ID发送图片消息，返回{chat_id: message_id}"""
    results = {}
    for chat_id in Config.TELEGRAM_CHAT_IDS:
        sub_event_key = f"{event_key}_chat_{chat_id}"
        msg_id = await atomic_send_telegram_photo_single_chat(photo_url, caption, sub_event_key, chat_id)
        results[chat_id] = msg_id
    return results

# --- 失败事件重试 ---
async def retry_failed_events() -> None:
    consecutive_failures = 0
    while True:
        try:
            if not await check_telegram_connectivity():
                consecutive_failures += 1
                sleep_time = 60 if consecutive_failures < 3 else 120
                await asyncio.sleep(sleep_time)
                continue
            
            if consecutive_failures > 0:
                logger.info(f"Telegram API恢复连接（之前连续{consecutive_failures}次失败）")
                consecutive_failures = 0
            
            for _ in range(min(3, failed_events_queue.qsize())):
                event = await failed_events_queue.get()
                event_type = event["type"]
                content = event["content"]
                image_url = event.get("image_url")
                event_key = event["event_key"]
                chat_id = event.get("chat_id") or Config.TELEGRAM_CHAT_IDS[0]  # 多聊天ID适配
                retry_count = event.get("retry_count", 0)
                max_retries = 5
                
                if await is_event_sent(event_key) or (await get_cached_message_id(event_key) is not None):
                    logger.debug(f"事件{event_key}已缓存，跳过重试")
                    failed_events_queue.task_done()
                    continue

                if retry_count >= max_retries:
                    logger.info(f"事件{event_key}超过最大重试次数（{max_retries}次），放弃")
                    failed_events_queue.task_done()
                    continue

                try:
                    if image_url:
                        result = await atomic_send_telegram_photo_single_chat(image_url, content, event_key, chat_id)
                    else:
                        result = await atomic_send_telegram_message_single_chat(content, event_key, chat_id)
                    
                    if result is not None:
                        logger.debug(f"[重试成功] 事件{event_key}（第{retry_count+1}次）")
                except Exception as e:
                    next_retry_delay = min(2 ** retry_count, 60)
                    event["retry_count"] = retry_count + 1
                    await failed_events_queue.put(event)
                    logger.debug(
                        f"[重试失败] 事件{event_key}（第{retry_count+1}次），"
                        f"下次重试在{next_retry_delay}秒后"
                    )
                    await asyncio.sleep(next_retry_delay)
                finally:
                    failed_events_queue.task_done()
        
        except Exception as e:
            logger.error(f"[重试任务异常] {str(e)}", exc_info=DEBUG_MODE)
        
        await asyncio.sleep(30)

# --- 缓存统计 ---
async def print_cache_stats():
    while True:
        logger.info(f"已发送事件缓存统计: {sent_events_cache.get_stats()}")
        logger.info(f"消息ID映射缓存统计: {event_message_id_cache.get_stats()}")
        logger.info(f"播放事件时效缓存统计: {play_event_cooldown_cache.get_stats()}")
        logger.info(f"用户登录时效缓存统计: {user_login_cooldown_cache.get_stats()}")
        await asyncio.sleep(3600)

# --- Webhook处理函数 ---
async def emby_webhook_handler(request: web.Request) -> web.Response:
    try:
        if request.content_type == "application/json":
            data_str = await request.text()
        else:
            post_data = await request.post()
            data_str = post_data.get('data', '{}')
        if DEBUG_MODE and len(data_str) > 200:
            logger.debug(f"Webhook原始数据(截断): {data_str[:200]}...")
        
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError as e:
            logger.warning(f"[无效事件] JSON格式错误: {str(e)}")
            return web.Response(text="无效JSON格式", status=400)
        
        event_key = get_event_key(data)
        logger.debug(f"处理事件，键: {event_key}")
        
        if await is_event_sent(event_key) or (await get_cached_message_id(event_key) is not None):
            logger.debug(f"[重复拦截] 已处理，键: {event_key}")
            return web.Response(text="重复事件，已跳过", status=200)
        
        event_type = data.get("Event", "").lower()
        
        if event_type.startswith("playback"):
            item = data.get("Item", {})
            item_id = item.get("Id") or data.get("ItemId")
            if item_id:
                if await is_play_event_in_cooldown(item_id, event_type):
                    logger.debug(f"[时效拦截] 同一内容({item_id})的{event_type}事件5分钟内已发送，跳过")
                    return web.Response(text="时效内重复事件，已跳过", status=200)
        
        if event_type == "user.authenticated":
            user = data.get("User", {})
            user_id = user.get("Id")
            if user_id:
                if await is_user_login_in_cooldown(user_id, event_type):
                    logger.debug(f"[时效拦截] 同一用户({user_id})的登录事件5分钟内已发送，跳过")
                    return web.Response(text="登录事件时效内重复，已跳过", status=200)
        
        is_valid, reason = is_valid_event(data)
        if not is_valid:
            logger.warning(f"[无效事件] {reason}，键: {event_key}")
            await mark_event_sent(event_key)
            await cache_message_id(event_key, -1)
            return web.Response(text=f"无效事件: {reason}", status=400)
        
        if event_type not in EMBY_MONITOR_EVENTS:
            logger.debug(f"事件{event_type}不在监控列表，键: {event_key}")
            await mark_event_sent(event_key)
            await cache_message_id(event_key, -2)
            return web.Response(text="事件不监控", status=200)
        
        user = data.get("User", {})
        session = data.get("Session", {})
        item = data.get("Item", {}) if "Item" in data else data
        if not item and "ItemId" in data:
            item = {"Id": data["ItemId"]}
        
        device_name = session.get("DeviceName") or data.get("Device", {}).get("Name") or "未知设备"
        ip_address = session.get("RemoteEndPoint") or data.get("RemoteEndPoint") or \
                     request.headers.get("X-Real-IP") or "未知IP"
        
        notification_text = ""
        image_url = None
        
        if event_type == "user.authenticated":
            if not user:
                user = {"Name": "未知用户"}
            client_name = session.get("Client") or session.get("DeviceName") or "未知客户端"
            notification_text = format_user_event(user, event_type, device_name, ip_address, client_name)
        
        elif event_type == "user.logout":
            if not user:
                user = {"Name": "未知用户"}
            client_name = session.get("Client") or session.get("DeviceName") or "未知客户端"
            notification_text = format_user_event(user, event_type, device_name, ip_address, client_name)
        
        elif event_type.startswith("playback"):
            if event_type in ["playback.stop", "playback.pause"]:
                action = "停止播放" if event_type == "playback.stop" else "播放暂停"
                msg = [f"⭐ 播放通知 | {action} ⭐", ""]
                msg.append(format_media_info(item, event_type))
                msg.append("——————")
                
                user_name = user.get("Name", "未知用户")
                msg.append(f"{get_emoji('user')} 用户: {user_name}")
                msg.append(f"{get_emoji('device')} 设备: {device_name}")
                msg.append(f"{get_emoji('ip')} IP: {ip_address}")
                msg.append("")
                
                playback_info = data.get("PlaybackInfo", {})
                position = playback_info.get("PositionTicks", 0)
                runtime = item.get("RunTimeTicks", 0)
                
                played = ticks_to_time(position)
                total = ticks_to_time(runtime)
                
                if runtime > 0:
                    progress = round((position / runtime) * 100, 1)
                    msg.append(f"{get_emoji('progress')} 进度: 「{progress}%」 | 已播放: {played} / 总时长: {total}")
                else:
                    msg.append(f"{get_emoji('progress')} 播放时长: {played} (总时长未知)")
                notification_text = "\n".join(msg)
            
            else:  # playback.start
                position = data.get("PositionTicks")
                runtime = item.get("RunTimeTicks")
                overview = process_html_tags(item.get("Overview", "暂无剧情简介"))
                if len(overview) > OVERVIEW_MAX_LENGTH:
                    overview = overview[:OVERVIEW_MAX_LENGTH] + "..."
                
                caption = [f"⭐ 播放通知 | 开始播放 ⭐", ""]
                caption.append(format_media_info(item, event_type))
                caption.append("——————")
                caption.append(f"{get_emoji('user')} 用户: {user.get('Name', '未知用户')}")
                caption.append(f"{get_emoji('device')} 设备: {device_name}")
                caption.append(f"{get_emoji('ip')} IP: {ip_address}")
                
                if position and runtime and runtime > 0:
                    progress = round((position / runtime) * 100, 1)
                    played = ticks_to_time(position)
                    total = ticks_to_time(runtime)
                    caption.append(f"{get_emoji('progress')} 进度: 「{progress}%」 | 已播放: {played} / 总时长: {total}")
                
                caption.extend(["", f"{get_emoji('overview')} 剧情:\n{overview}"])
                notification_text = "\n".join(caption)
                image_url = get_emby_image_url(item) if ENABLE_COVERS else None
        
        elif event_type in ["library.new", "library.deleted", "metadata.update"]:
            if not item:
                msg = [f"⭐ 媒体库事件 ⭐", "", f"{get_emoji('error')} 缺少详细数据"]
                notification_text = "\n".join(msg)
            else:
                notification_text = format_library_event(data, item, event_type)
                image_url = get_emby_image_url(item) if ENABLE_COVERS else None
        
        else:
            msg = [f"⭐ 未知事件 ⭐", f"{get_emoji('info')} 事件类型: {event_type}"]
            notification_text = "\n".join(msg)
        
        # Telegram 与企业微信互不依赖：任一通道异常不会阻止另一通道发送。
        send_results = {}
        if telegram_enabled:
            if ENABLE_COVERS and image_url and event_type in ["playback.start", "library.new", "metadata.update"]:
                send_results = await send_telegram_photo(image_url, notification_text, event_key)
            else:
                send_results = await send_telegram_message(notification_text, event_key)

        wecom_sent = await send_wecom_message(notification_text, image_url) if wecom_client else False
        
        # 处理发送失败的聊天ID
        for chat_id, msg_id in send_results.items():
            if msg_id:
                # 标记冷却（仅对成功发送的ID生效）
                if event_type.startswith("playback") and item.get("Id"):
                    await mark_play_event_cooldown(item.get("Id"), event_type)
                elif event_type == "user.authenticated" and user.get("Id"):
                    await mark_user_login_cooldown(user.get("Id"), event_type)
            else:
                # 失败的ID加入重试队列
                sub_event_key = f"{event_key}_chat_{chat_id}"
                logger.warning(f"[发送失败] 加入重试队列，键: {sub_event_key}，chat_id: {chat_id}")
                await failed_events_queue.put({
                    "type": "photo" if (ENABLE_COVERS and image_url) else "text",
                    "content": notification_text,
                    "image_url": image_url if (ENABLE_COVERS and image_url) else None,
                    "event_key": sub_event_key,
                    "chat_id": chat_id,
                    "retry_count": 0
                })

        delivered = wecom_sent or any(msg_id is not None for msg_id in send_results.values())
        if delivered:
            # 用基础事件键去重，使仅配置企业微信的部署也具备重复事件保护。
            await mark_event_sent(event_key)
            if event_type.startswith("playback") and item.get("Id"):
                await mark_play_event_cooldown(item.get("Id"), event_type)
            elif event_type == "user.authenticated" and user.get("Id"):
                await mark_user_login_cooldown(user.get("Id"), event_type)
        
        return web.Response(status=200)
    
    except Exception as e:
        logger.error(f"处理事件异常: {str(e)}", exc_info=DEBUG_MODE)
        return web.Response(status=500)

# --- 服务启动 ---
async def main():
    NetworkUtils.setup_proxy()
    await initialize_telegram_bot()
    
    webapp = web.Application()
    webapp.add_routes([web.post('/emby-webhook', emby_webhook_handler)])
    if Config.WECOM_TOKEN and Config.WECOM_ENCODING_AES_KEY:
        webapp.add_routes([
            web.get('/wecom-callback', wecom_callback_handler),
            web.post('/wecom-callback', wecom_callback_handler),
        ])
    
    startup_time = datetime.now(USER_TZ).strftime('%Y-%m-%d %H:%M:%S')
    
    # 将监控事件列表转换为中文
    monitored_events_chinese = [EVENT_NAME_MAP.get(event, event) for event in EMBY_MONITOR_EVENTS]
    
    startup_info = (
        f"⭐ Emby监控通知服务已启动 ⭐\n\n"
        f"{get_emoji('info')} 版本: {APP_VERSION}\n"
        f"{get_emoji('info')} 时区: {USER_TZ.zone}\n"
        f"{get_emoji('info')} Telegram 接收ID数量: {len(Config.TELEGRAM_CHAT_IDS)}\n"
        f"{get_emoji('info')} 企业微信应用推送: {'已启用' if wecom_client else '未启用'}\n"
        f"{get_emoji('info')} 网络代理配置: {'已启用' if (Config.HTTP_PROXY or Config.HTTPS_PROXY or Config.WECOM_PROXY) else '未启用'}\n"
        f"{get_emoji('info')} 防频繁通知: 已启用{PLAY_EVENT_COOLDOWN}秒冷却时间\n"
        f"{get_emoji('time')} 启动时间: {startup_time}\n"
        f"{get_emoji('info')} 监控事件: {', '.join(monitored_events_chinese)}"
        )
    logger.info(startup_info)
    
    retry_task = asyncio.create_task(retry_failed_events()) if Config.telegram_configured() else None
    stats_task = asyncio.create_task(print_cache_stats())
    logger.info("缓存统计任务已启动%s", "，Telegram 失败事件重试任务已启动" if retry_task else "")
    
    start_event_key = f"startup_{datetime.now(USER_TZ).timestamp()//300}"
    if telegram_enabled:
        await send_telegram_message(startup_info, start_event_key)
    elif Config.telegram_configured():
        for chat_id in Config.TELEGRAM_CHAT_IDS:
            sub_event_key = f"{start_event_key}_chat_{chat_id}"
            await failed_events_queue.put({
                "type": "text",
                "content": startup_info,
                "event_key": sub_event_key,
                "chat_id": chat_id,
                "retry_count": 0
            })
    if wecom_client:
        await send_wecom_message(startup_info)
    
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    logger.info(f"Web服务监听: {WEBHOOK_HOST}:{WEBHOOK_PORT}")
    
    shutdown_event = asyncio.Event()
    try:
        await shutdown_event.wait()
    finally:
        await failed_events_queue.join()
        if retry_task:
            retry_task.cancel()
        stats_task.cancel()
        await http_clients.close()
        if wecom_client:
            await wecom_client.close()
        await runner.cleanup()
        logger.info("服务已关闭")

if __name__ == "__main__":
    try:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
            
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("服务手动停止")
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}", exc_info=True)
        sys.exit(1)
