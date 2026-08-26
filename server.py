"""RunningHub LTX 2.3 dual-character dialogue MCP server.

The server accepts a two-person reference image, one complete dialogue audio
track and an LTX video prompt.  ChatGPT attachments and public URLs are
downloaded by this service, uploaded to RunningHub, and then inserted into the
configured RunningHub AI App's editable nodes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv

# A production MCP server should not contact PyPI during startup.  Disabling
# FastMCP's update banner also avoids startup failures in restricted networks.
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
os.environ.setdefault("FASTMCP_SHOW_CLI_BANNER", "false")

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("runninghub-ltx23-dialogue-mcp")


class ConfigurationError(ValueError):
    """Raised when deployment configuration is incomplete or inconsistent."""


class RunningHubAPIError(RuntimeError):
    """A sanitized RunningHub error that is safe to return through MCP."""

    def __init__(self, message: str, *, code: str | int | None = None) -> None:
        super().__init__(message)
        self.code = code


class OpenAIFile(BaseModel):
    """ChatGPT file object supplied for a declared OpenAI file parameter."""

    model_config = ConfigDict(extra="forbid")

    download_url: str = Field(description="Temporary URL downloadable by the MCP server")
    file_id: str = Field(description="ChatGPT file identifier")
    # Keep these JSON Schema properties as strings (not string|null).  ChatGPT
    # may omit them, so the runtime default is still None.
    mime_type: str = Field(default=None, description="File MIME type")  # type: ignore[assignment]
    file_name: str = Field(default=None, description="Original file name")  # type: ignore[assignment]


def finalize_openai_file_param_schema(tool: Any, *parameter_names: str) -> None:
    """Make FastMCP's schema comply with ChatGPT's file parameter contract."""

    properties = tool.parameters.get("properties", {})
    for parameter_name in parameter_names:
        file_schema = properties.get(parameter_name)
        if not isinstance(file_schema, dict):
            raise RuntimeError(f"Missing file parameter schema: {parameter_name}")
        file_schema["additionalProperties"] = False
        for optional_name in ("mime_type", "file_name"):
            optional_schema = file_schema.get("properties", {}).get(optional_name, {})
            optional_schema.pop("default", None)


@dataclass(frozen=True)
class NodeTarget:
    node_id: str
    field_name: str

    def to_dict(self) -> dict[str, str]:
        return {"nodeId": self.node_id, "fieldName": self.field_name}


SUPPORTED_ROLES = {
    "image",
    "audio",
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "aspect_ratio",
    "duration_seconds",
    "frame_count",
    "fps",
    "seed",
}

ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "image": (
        "image",
        "input_image",
        "reference_image",
        "start_image",
        "first_frame",
        "首帧",
        "参考图",
        "双人图",
        "上传图像",
        "上传图片",
        "图片",
        "图像",
    ),
    "audio": (
        "audio",
        "input_audio",
        "driving_audio",
        "dialogue_audio",
        "reference_audio",
        "驱动音频",
        "对话音频",
        "参考音频",
        "上传音频",
        "音频",
        "声音",
    ),
    "prompt": (
        "prompt",
        "positive_prompt",
        "text_prompt",
        "video_prompt",
        "提示词",
        "视频提示词",
        "正向提示词",
    ),
    "negative_prompt": (
        "negative_prompt",
        "negative",
        "负面提示词",
        "反向提示词",
    ),
    "width": ("width", "video_width", "宽度", "视频宽度"),
    "height": ("height", "video_height", "高度", "视频高度"),
    "aspect_ratio": ("aspect_ratio", "ratio", "画幅", "比例", "宽高比"),
    "duration_seconds": (
        "duration_seconds",
        "duration",
        "seconds",
        "video_length",
        "时长",
        "秒数",
    ),
    "frame_count": (
        "frame_count",
        "num_frames",
        "video_frames",
        "length",
        "总帧数",
        "帧数",
    ),
    "fps": ("fps", "frame_rate", "framerate", "帧率"),
    "seed": ("seed", "random_seed", "噪声种子", "随机种子"),
}


@dataclass(frozen=True)
class Settings:
    api_key: str
    webapp_id: str
    base_url: str = "https://www.runninghub.cn"
    upload_path: str = "/openapi/v2/media/upload/binary"
    access_password: str = ""
    node_map: dict[str, NodeTarget] = field(default_factory=dict)
    extra_node_info: list[dict[str, Any]] = field(default_factory=list)
    auto_discover_nodes: bool = True
    http_timeout_seconds: float = 120.0
    poll_interval_seconds: float = 5.0
    max_remote_file_bytes: int = 200 * 1024 * 1024

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> "Settings":
        api_key = os.getenv("RUNNINGHUB_API_KEY", "").strip()
        webapp_id = os.getenv("RUNNINGHUB_WEBAPP_ID", "").strip()
        if require_credentials and not api_key:
            raise ConfigurationError("缺少 RUNNINGHUB_API_KEY。")
        if require_credentials and not webapp_id:
            raise ConfigurationError("缺少 RUNNINGHUB_WEBAPP_ID。")

        base_url = os.getenv(
            "RUNNINGHUB_BASE_URL", "https://www.runninghub.cn"
        ).strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("RUNNINGHUB_BASE_URL 必须是有效的 HTTP(S) 地址。")

        upload_path = os.getenv(
            "RUNNINGHUB_UPLOAD_PATH", "/openapi/v2/media/upload/binary"
        ).strip()
        if not upload_path.startswith("/"):
            upload_path = f"/{upload_path}"

        max_bytes = env_int(
            "MAX_REMOTE_FILE_BYTES",
            200 * 1024 * 1024,
            minimum=1 * 1024 * 1024,
            maximum=1024 * 1024 * 1024,
        )

        return cls(
            api_key=api_key,
            webapp_id=webapp_id,
            base_url=base_url,
            upload_path=upload_path,
            access_password=os.getenv("RUNNINGHUB_ACCESS_PASSWORD", "").strip(),
            node_map=parse_node_map(os.getenv("RUNNINGHUB_NODE_MAP_JSON", "")),
            extra_node_info=parse_extra_node_info(
                os.getenv("RUNNINGHUB_EXTRA_NODE_INFO_JSON", "")
            ),
            auto_discover_nodes=env_bool("RUNNINGHUB_AUTO_DISCOVER_NODES", True),
            http_timeout_seconds=env_float(
                "RUNNINGHUB_HTTP_TIMEOUT_SECONDS", 120.0, minimum=10.0, maximum=600.0
            ),
            poll_interval_seconds=env_float(
                "RUNNINGHUB_POLL_INTERVAL_SECONDS", 5.0, minimum=1.0, maximum=30.0
            ),
            max_remote_file_bytes=max_bytes,
        )


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


def env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字。") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
    return value


def parse_node_map(raw: str) -> dict[str, NodeTarget]:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"RUNNINGHUB_NODE_MAP_JSON 不是有效 JSON：{exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("RUNNINGHUB_NODE_MAP_JSON 必须是 JSON 对象。")

    result: dict[str, NodeTarget] = {}
    for role, target in payload.items():
        if role not in SUPPORTED_ROLES:
            raise ConfigurationError(
                f"不支持的节点角色 {role!r}；允许值：{', '.join(sorted(SUPPORTED_ROLES))}"
            )
        if isinstance(target, str) and ":" in target:
            node_id, field_name = target.split(":", 1)
        elif isinstance(target, dict):
            node_id = str(target.get("nodeId") or target.get("node_id") or "")
            field_name = str(
                target.get("fieldName") or target.get("field_name") or ""
            )
        else:
            raise ConfigurationError(
                f"节点角色 {role!r} 应写成 '节点ID:字段名' 或对象。"
            )
        node_id, field_name = node_id.strip(), field_name.strip()
        if not node_id or not field_name:
            raise ConfigurationError(f"节点角色 {role!r} 缺少 nodeId 或 fieldName。")
        result[role] = NodeTarget(node_id, field_name)
    return result


def parse_extra_node_info(raw: str) -> list[dict[str, Any]]:
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            f"RUNNINGHUB_EXTRA_NODE_INFO_JSON 不是有效 JSON：{exc.msg}"
        ) from exc
    if not isinstance(payload, list):
        raise ConfigurationError("RUNNINGHUB_EXTRA_NODE_INFO_JSON 必须是 JSON 数组。")

    result: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"额外节点第 {index} 项必须是对象。")
        node_id = str(item.get("nodeId", "")).strip()
        field_name = str(item.get("fieldName", "")).strip()
        if not node_id or not field_name or "fieldValue" not in item:
            raise ConfigurationError(
                f"额外节点第 {index} 项必须包含 nodeId、fieldName、fieldValue。"
            )
        result.append(
            {
                "nodeId": node_id,
                "fieldName": field_name,
                "fieldValue": item["fieldValue"],
            }
        )
    return result


def normalize_token(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def score_node_for_role(node: dict[str, Any], role: str) -> int:
    field_name = normalize_token(node.get("fieldName"))
    node_name = normalize_token(node.get("nodeName"))
    description = normalize_token(
        f"{node.get('description', '')} {node.get('descriptionEn', '')}"
    )
    field_type = normalize_token(node.get("fieldType"))
    combined = f"{field_name} {node_name} {description}"
    score = 0

    for alias in ROLE_ALIASES[role]:
        normalized = normalize_token(alias)
        if field_name == normalized:
            score = max(score, 130)
        elif normalized and normalized in field_name:
            score = max(score, 100)
        if normalized and normalized in node_name:
            score = max(score, 80)
        if normalized and normalized in description:
            score = max(score, 60)

    if role == "image":
        if field_type == "image" or "loadimage" in node_name:
            score += 150
        if field_type == "audio" or "audio" in combined or "音频" in combined:
            score -= 250
    elif role == "audio":
        if field_type == "audio" or "loadaudio" in node_name:
            score += 150
        if "audio" in combined or "音频" in combined:
            score += 100
        if field_type == "image" or "image" in combined or "图像" in combined:
            score -= 250
    elif role == "prompt":
        if field_type in {"string", "text"}:
            score += 20
        if any(marker in combined for marker in ("negative", "负面", "反向")):
            score -= 250
    elif role == "negative_prompt":
        if any(marker in combined for marker in ("negative", "负面", "反向")):
            score += 160

    return score


def infer_node_map(nodes: list[dict[str, Any]]) -> dict[str, NodeTarget]:
    inferred: dict[str, NodeTarget] = {}
    for role in SUPPORTED_ROLES:
        ranked = sorted(
            ((score_node_for_role(node, role), node) for node in nodes),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] <= 0:
            continue
        best_score, best = ranked[0]
        if len(ranked) > 1 and ranked[1][0] == best_score:
            # Ambiguous discovery must be resolved explicitly in Railway.
            continue
        node_id = str(best.get("nodeId", "")).strip()
        field_name = str(best.get("fieldName", "")).strip()
        if node_id and field_name:
            inferred[role] = NodeTarget(node_id, field_name)
    return inferred


def ensure_text(name: str, value: str, *, max_length: int) -> str:
    cleaned = str(value or "").strip()
    if len(cleaned) > max_length:
        raise ValueError(f"{name} 最长允许 {max_length} 个字符。")
    return cleaned


class RunningHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.api_key}",
            "User-Agent": "runninghub-ltx23-dialogue-mcp/1.0",
        }

    def _unwrap(self, body: Any) -> Any:
        if not isinstance(body, dict):
            return body
        if "code" not in body:
            return body
        code = body.get("code", 0)
        if str(code) != "0":
            message = str(body.get("msg") or body.get("message") or "未知错误")
            message = message.replace(self.settings.api_key, "***")
            raise RunningHubAPIError(
                f"RunningHub API 错误 {code}：{message}", code=code
            )
        return body.get("data")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        request_payload = dict(payload or {})
        request_params = dict(params or {})
        if method.upper() == "GET":
            request_params["apiKey"] = self.settings.api_key
        else:
            request_payload["apiKey"] = self.settings.api_key

        headers = dict(self.headers)
        if method.upper() != "GET":
            headers["Content-Type"] = "application/json"

        transient = {408, 425, 429, 500, 502, 503, 504}
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    base_url=self.settings.base_url,
                    timeout=self.settings.http_timeout_seconds,
                    headers=headers,
                    follow_redirects=True,
                ) as http:
                    response = await http.request(
                        method,
                        path,
                        json=request_payload if method.upper() != "GET" else None,
                        params=request_params or None,
                    )
            except httpx.RequestError as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
                raise RunningHubAPIError(
                    f"无法连接 RunningHub：{type(exc).__name__}。"
                ) from exc

            if response.status_code in transient and attempt < 2:
                await asyncio.sleep(1.5 * (2**attempt))
                continue
            if response.is_error:
                safe_text = response.text.replace(self.settings.api_key, "***")[:500]
                raise RunningHubAPIError(
                    f"RunningHub HTTP {response.status_code}：{safe_text or '请求失败'}",
                    code=response.status_code,
                )
            try:
                return self._unwrap(response.json())
            except ValueError as exc:
                raise RunningHubAPIError("RunningHub 返回了非 JSON 响应。") from exc

        raise RunningHubAPIError("RunningHub 请求在重试后仍然失败。")

    async def get_ai_app_demo(self) -> dict[str, Any]:
        data = await self.request_json(
            "GET",
            "/api/webapp/apiCallDemo",
            params={"webappId": self.settings.webapp_id},
        )
        if not isinstance(data, dict):
            raise RunningHubAPIError("AI 应用调用示例返回格式不正确。")
        return data

    async def upload_bytes(
        self, content: bytes, filename: str, content_type: str
    ) -> str:
        if not content:
            raise ValueError(f"{filename} 是空文件。")
        headers = dict(self.headers)
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.base_url,
                timeout=max(self.settings.http_timeout_seconds, 300.0),
                headers=headers,
                follow_redirects=True,
            ) as http:
                response = await http.post(
                    self.settings.upload_path,
                    data={"apiKey": self.settings.api_key},
                    files={"file": (filename, content, content_type)},
                )
        except httpx.RequestError as exc:
            raise RunningHubAPIError(
                f"上传到 RunningHub 失败：{type(exc).__name__}。"
            ) from exc
        if response.is_error:
            safe_text = response.text.replace(self.settings.api_key, "***")[:500]
            raise RunningHubAPIError(
                f"RunningHub 上传 HTTP {response.status_code}：{safe_text}"
            )
        try:
            data = self._unwrap(response.json())
        except ValueError as exc:
            raise RunningHubAPIError("RunningHub 上传接口返回了非 JSON 响应。") from exc
        if not isinstance(data, dict):
            raise RunningHubAPIError("RunningHub 上传接口没有返回文件信息。")
        uploaded = str(data.get("fileName") or data.get("filename") or "").strip()
        if not uploaded:
            raise RunningHubAPIError("RunningHub 上传成功响应中缺少 fileName。")
        return uploaded

    async def run_ai_app(self, node_info_list: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "webappId": self.settings.webapp_id,
            "nodeInfoList": node_info_list,
        }
        if self.settings.access_password:
            payload["accessPassword"] = self.settings.access_password
        data = await self.request_json(
            "POST", "/task/openapi/ai-app/run", payload=payload
        )
        if not isinstance(data, dict) or not data.get("taskId"):
            raise RunningHubAPIError("RunningHub 未返回 taskId，任务没有成功创建。")
        prompt_tips = data.get("promptTips")
        if isinstance(prompt_tips, str):
            try:
                prompt_tips = json.loads(prompt_tips)
            except json.JSONDecodeError:
                pass
        if isinstance(prompt_tips, dict) and prompt_tips.get("node_errors"):
            raise RunningHubAPIError(
                f"RunningHub 节点校验失败：{prompt_tips['node_errors']}"
            )
        return data

    async def get_outputs(self, task_id: str) -> list[dict[str, Any]] | dict[str, Any]:
        data = await self.request_json(
            "POST",
            "/task/openapi/outputs",
            payload={"taskId": task_id},
        )
        if isinstance(data, (list, dict)):
            return data
        return []


_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_active_task_id: str | None = None
_submit_lock = asyncio.Lock()


async def get_discovery(
    settings: Settings, client: RunningHubClient
) -> dict[str, Any]:
    cache_key = f"{settings.base_url}|{settings.webapp_id}"
    cached = _discovery_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    demo = await client.get_ai_app_demo()
    _discovery_cache[cache_key] = (time.monotonic(), demo)
    return demo


async def resolve_node_map(
    settings: Settings, client: RunningHubClient
) -> tuple[dict[str, NodeTarget], dict[str, Any]]:
    resolved = dict(settings.node_map)
    demo = await get_discovery(settings, client)
    if settings.auto_discover_nodes:
        nodes = [
            item
            for item in (demo.get("nodeInfoList") or [])
            if isinstance(item, dict)
        ]
        for role, target in infer_node_map(nodes).items():
            resolved.setdefault(role, target)
    return resolved, demo


def _ensure_public_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("只支持 http:// 或 https:// 文件 URL。")
    if not parsed.hostname:
        raise ValueError("文件 URL 缺少主机名。")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("不允许 localhost URL。")
    try:
        infos = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise ValueError(f"无法解析文件 URL 主机：{host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError("不允许访问私有或本地网络 URL。")
    return url


async def download_public_file(
    url: str,
    *,
    filename_hint: str = "",
    max_bytes: int,
) -> tuple[bytes, str, str]:
    current_url = _ensure_public_http_url(url)
    async with httpx.AsyncClient(timeout=300.0) as http:
        for _ in range(6):
            async with http.stream(
                "GET", current_url, follow_redirects=False
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise ValueError("文件下载重定向缺少 Location。")
                    current_url = _ensure_public_http_url(
                        urljoin(current_url, location)
                    )
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise ValueError(f"文件超过 {max_bytes} 字节限制。")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"文件超过 {max_bytes} 字节限制。")
                    chunks.append(chunk)
                content = b"".join(chunks)
                if not content:
                    raise ValueError("下载到的文件为空。")
                content_type = (
                    response.headers.get("content-type", "application/octet-stream")
                    .split(";", 1)[0]
                    .strip()
                    or "application/octet-stream"
                )
                filename = PurePosixPath(filename_hint).name if filename_hint else ""
                if not filename:
                    filename = (
                        PurePosixPath(urlparse(current_url).path).name
                        or "chatgpt-upload.bin"
                    )
                return content, filename, content_type
        raise ValueError("文件 URL 重定向次数过多。")


def validate_media_type(
    *, filename: str, content_type: str, expected_kind: str
) -> None:
    lowered_type = content_type.lower()
    suffix = PurePosixPath(filename).suffix.lower()
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    audio_suffixes = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
    if expected_kind == "image":
        valid = lowered_type.startswith("image/") or suffix in image_suffixes
    elif expected_kind == "audio":
        valid = lowered_type.startswith("audio/") or suffix in audio_suffixes
    else:
        valid = True
    if not valid:
        raise ValueError(
            f"{filename} 不是支持的{expected_kind}文件，MIME={content_type}。"
        )


async def upload_file_object(
    client: RunningHubClient,
    settings: Settings,
    file: OpenAIFile,
    *,
    expected_kind: str,
) -> str:
    safe_name = PurePosixPath(file.file_name or "").name
    if not safe_name:
        safe_name = f"{file.file_id}.bin"
    content, filename, downloaded_type = await download_public_file(
        file.download_url,
        filename_hint=safe_name,
        max_bytes=settings.max_remote_file_bytes,
    )
    content_type = (file.mime_type or downloaded_type or "application/octet-stream").strip()
    validate_media_type(
        filename=filename, content_type=content_type, expected_kind=expected_kind
    )
    return await client.upload_bytes(content, filename, content_type)


async def upload_url(
    client: RunningHubClient,
    settings: Settings,
    url: str,
    *,
    expected_kind: str,
    filename_hint: str = "",
) -> str:
    content, filename, content_type = await download_public_file(
        url,
        filename_hint=filename_hint,
        max_bytes=settings.max_remote_file_bytes,
    )
    validate_media_type(
        filename=filename, content_type=content_type, expected_kind=expected_kind
    )
    return await client.upload_bytes(content, filename, content_type)


def compose_prompt(
    *,
    prompt: str,
    left_character: str,
    right_character: str,
    speaker_timeline: str,
    action: str,
    camera: str,
) -> str:
    parts = [
        ensure_text("prompt", prompt, max_length=5000),
        "保持首帧中两位人物的身份、五官、服装和左右位置稳定。",
        "只有当前发言者自然张嘴，另一人保持倾听反应；不要交换角色，不要同时抢话。",
        "口型、表情和身体节奏严格跟随输入音频，画面连续自然，不自动添加字幕。",
    ]
    if left_character:
        parts.append(f"左侧人物：{left_character}")
    if right_character:
        parts.append(f"右侧人物：{right_character}")
    if speaker_timeline:
        parts.append(f"发言顺序与时间线：{speaker_timeline}")
    if action:
        parts.append(f"动作与表演：{action}")
    if camera:
        parts.append(f"镜头：{camera}")
    return "\n".join(item for item in parts if item)


def validate_numeric_inputs(
    *, width: int, height: int, frame_count: int, fps: int, seed: int
) -> None:
    if width and (width < 256 or width > 4096 or width % 32 != 0):
        raise ValueError("width 必须是 256–4096 之间且能被 32 整除的整数。")
    if height and (height < 256 or height > 4096 or height % 32 != 0):
        raise ValueError("height 必须是 256–4096 之间且能被 32 整除的整数。")
    if frame_count and (frame_count < 9 or frame_count > 4097 or frame_count % 8 != 1):
        raise ValueError("frame_count 必须满足 8n+1，例如 81、121、161、241。")
    if fps and not 8 <= fps <= 60:
        raise ValueError("fps 必须在 8–60 之间。")
    if not -1 <= seed <= 2_147_483_647:
        raise ValueError("seed 必须为 -1 或 0–2147483647。")


def make_node_info_list(
    node_map: dict[str, NodeTarget],
    values: dict[str, Any],
    extra_node_info: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ordered: dict[tuple[str, str], dict[str, Any]] = {}
    for role, target in node_map.items():
        value = values.get(role)
        if value in {None, "", 0, -1}:
            continue
        key = (target.node_id, target.field_name)
        ordered[key] = {
            "nodeId": target.node_id,
            "fieldName": target.field_name,
            "fieldValue": value,
        }
    for item in extra_node_info:
        key = (str(item["nodeId"]), str(item["fieldName"]))
        ordered[key] = dict(item)
    return list(ordered.values())


def summarize_outputs(outputs: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
    if isinstance(outputs, dict):
        failed_reason = outputs.get("failedReason") or outputs.get("failed_reason")
        return {
            "outputs": [],
            "video_urls": [],
            "all_output_urls": [],
            "failed_reason": failed_reason,
        }
    video_urls: list[str] = []
    all_urls: list[str] = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        url = str(item.get("fileUrl") or item.get("url") or "").strip()
        file_type = str(item.get("fileType") or item.get("type") or "").lower()
        if not url:
            continue
        all_urls.append(url)
        clean_url = url.lower().split("?", 1)[0]
        if "video" in file_type or clean_url.endswith(
            (".mp4", ".mov", ".webm", ".mkv")
        ):
            video_urls.append(url)
    return {
        "outputs": outputs,
        "video_urls": video_urls,
        "all_output_urls": all_urls,
        "failed_reason": None,
    }


async def clear_active_task(task_id: str) -> None:
    global _active_task_id
    async with _submit_lock:
        if _active_task_id == task_id:
            _active_task_id = None


async def generate_impl(
    *,
    image_filename: str,
    audio_filename: str,
    prompt: str,
    left_character: str = "",
    right_character: str = "",
    speaker_timeline: str = "",
    action: str = "",
    camera: str = "固定中近景，轻微自然呼吸感，不切镜",
    negative_prompt: str = "字幕，文字，水印，角色换位，身份变化，两人同时开口，口型错位，面部畸形，闪烁",
    width: int = 0,
    height: int = 0,
    aspect_ratio: str = "",
    duration_seconds: int = 0,
    frame_count: int = 0,
    fps: int = 0,
    seed: int = -1,
) -> dict[str, Any]:
    global _active_task_id
    image_filename = ensure_text("image_filename", image_filename, max_length=1000)
    audio_filename = ensure_text("audio_filename", audio_filename, max_length=1000)
    if not image_filename or not audio_filename:
        raise ValueError("必须同时提供 image_filename 和 audio_filename。")
    if duration_seconds and not 1 <= duration_seconds <= 120:
        raise ValueError("duration_seconds 必须在 1–120 之间；0 表示跟随音频或应用默认值。")
    validate_numeric_inputs(
        width=width,
        height=height,
        frame_count=frame_count,
        fps=fps,
        seed=seed,
    )
    full_prompt = compose_prompt(
        prompt=prompt,
        left_character=ensure_text("left_character", left_character, max_length=1000),
        right_character=ensure_text("right_character", right_character, max_length=1000),
        speaker_timeline=ensure_text("speaker_timeline", speaker_timeline, max_length=2000),
        action=ensure_text("action", action, max_length=2000),
        camera=ensure_text("camera", camera, max_length=500),
    )

    settings = Settings.from_env()
    client = RunningHubClient(settings)
    node_map, _ = await resolve_node_map(settings, client)
    missing = [role for role in ("image", "audio", "prompt") if role not in node_map]
    if missing:
        raise ConfigurationError(
            "无法确定以下必需节点："
            + ", ".join(missing)
            + "。请先调用 inspect_ltx23_app，并用 RUNNINGHUB_NODE_MAP_JSON 映射。"
        )

    values: dict[str, Any] = {
        "image": image_filename,
        "audio": audio_filename,
        "prompt": full_prompt,
        "negative_prompt": ensure_text(
            "negative_prompt", negative_prompt, max_length=2000
        ),
        "width": width,
        "height": height,
        "aspect_ratio": ensure_text("aspect_ratio", aspect_ratio, max_length=30),
        "duration_seconds": duration_seconds,
        "frame_count": frame_count,
        "fps": fps,
        "seed": seed,
    }
    node_info_list = make_node_info_list(
        node_map, values, settings.extra_node_info
    )

    async with _submit_lock:
        if _active_task_id:
            try:
                active_outputs = await client.get_outputs(_active_task_id)
            except RunningHubAPIError as exc:
                if str(exc.code) == "804":
                    raise RunningHubAPIError(
                        f"当前已有任务 {_active_task_id} 正在生成，请先查询完成后再提交。",
                        code="TASK_BUSY",
                    ) from exc
                if str(exc.code) == "805":
                    _active_task_id = None
                else:
                    raise
            else:
                active_summary = summarize_outputs(active_outputs)
                if not active_summary.get("video_urls") and not active_summary.get(
                    "failed_reason"
                ):
                    raise RunningHubAPIError(
                        f"当前已有任务 {_active_task_id} 正在生成，请先查询完成后再提交。",
                        code="TASK_BUSY",
                    )
                _active_task_id = None

        task = await client.run_ai_app(node_info_list)
        task_id = str(task["taskId"])
        _active_task_id = task_id

    return {
        "ok": True,
        "task_id": task_id,
        "status": str(task.get("taskStatus") or task.get("status") or "RUNNING").upper(),
        "webapp_id": settings.webapp_id,
        "used_node_roles": sorted(role for role in node_map if values.get(role) not in {None, "", 0, -1}),
        "runninghub_files": {
            "image_filename": image_filename,
            "audio_filename": audio_filename,
        },
        "prompt": full_prompt,
        "next_action": "调用 query_ltx23_task，并传入 task_id。",
    }


SERVER_INSTRUCTIONS = (
    "This server creates LTX 2.3 two-person dialogue videos on RunningHub. "
    "Use inspect_ltx23_app first after deployment. For ChatGPT attachments, use "
    "generate_ltx23_dialogue_from_chatgpt_attachments. For public URLs, use "
    "generate_ltx23_dialogue_from_urls. Each generation consumes RunningHub credits. "
    "After submission, call query_ltx23_task until video_urls are returned."
)

mcp = FastMCP("RunningHub LTX 2.3 Dialogue", instructions=SERVER_INSTRUCTIONS)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def inspect_ltx23_app() -> dict[str, Any]:
    """只读检查 LTX 2.3 AI 应用、可修改节点和自动映射，不消耗生成额度。"""

    settings = Settings.from_env()
    client = RunningHubClient(settings)
    resolved, demo = await resolve_node_map(settings, client)
    nodes: list[dict[str, Any]] = []
    for node in demo.get("nodeInfoList") or []:
        if not isinstance(node, dict):
            continue
        field_data = node.get("fieldData")
        if isinstance(field_data, list):
            choices: Any = field_data[:30]
        elif isinstance(field_data, dict):
            choices = {
                str(key): value[:30] if isinstance(value, list) else value
                for key, value in list(field_data.items())[:20]
            }
        else:
            choices = None
        nodes.append(
            {
                "nodeId": str(node.get("nodeId", "")),
                "nodeName": str(node.get("nodeName", "")),
                "fieldName": str(node.get("fieldName", "")),
                "fieldType": str(node.get("fieldType", "")),
                "description": str(node.get("description", "")),
                "defaultValue": node.get("fieldValue"),
                "choices": choices,
            }
        )
    missing = [role for role in ("image", "audio", "prompt") if role not in resolved]
    return {
        "ok": not missing,
        "base_url": settings.base_url,
        "webapp_id": settings.webapp_id,
        "webapp_name": str(demo.get("webappName", "")),
        "access_encrypted": bool(demo.get("accessEncrypted", False)),
        "resolved_node_map": {
            role: target.to_dict() for role, target in sorted(resolved.items())
        },
        "missing_required_roles": missing,
        "available_nodes": nodes,
        "generation_will_consume_credits": True,
    }


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def generate_ltx23_dialogue(
    image_filename: str,
    audio_filename: str,
    prompt: str,
    left_character: str = "",
    right_character: str = "",
    speaker_timeline: str = "",
    action: str = "",
    camera: str = "固定中近景，轻微自然呼吸感，不切镜",
    negative_prompt: str = "字幕，文字，水印，角色换位，身份变化，两人同时开口，口型错位，面部畸形，闪烁",
    width: int = 0,
    height: int = 0,
    aspect_ratio: str = "",
    duration_seconds: int = 0,
    frame_count: int = 0,
    fps: int = 0,
    seed: int = -1,
) -> dict[str, Any]:
    """用已经上传到 RunningHub 的图片和音频文件生成双人对话视频。此操作消耗 RH 币。"""

    return await generate_impl(
        image_filename=image_filename,
        audio_filename=audio_filename,
        prompt=prompt,
        left_character=left_character,
        right_character=right_character,
        speaker_timeline=speaker_timeline,
        action=action,
        camera=camera,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        duration_seconds=duration_seconds,
        frame_count=frame_count,
        fps=fps,
        seed=seed,
    )


@mcp.tool(
    meta={"openai/fileParams": ["image_file", "audio_file"]},
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def generate_ltx23_dialogue_from_chatgpt_attachments(
    image_file: OpenAIFile,
    audio_file: OpenAIFile,
    prompt: str,
    left_character: str = "",
    right_character: str = "",
    speaker_timeline: str = "",
    action: str = "",
    camera: str = "固定中近景，轻微自然呼吸感，不切镜",
    negative_prompt: str = "字幕，文字，水印，角色换位，身份变化，两人同时开口，口型错位，面部畸形，闪烁",
    width: int = 0,
    height: int = 0,
    aspect_ratio: str = "",
    duration_seconds: int = 0,
    frame_count: int = 0,
    fps: int = 0,
    seed: int = -1,
) -> dict[str, Any]:
    """直接使用 ChatGPT 附件中的双人首帧图和完整对白音频生成视频。此操作消耗 RH 币。"""

    settings = Settings.from_env()
    client = RunningHubClient(settings)
    image_filename, audio_filename = await asyncio.gather(
        upload_file_object(
            client, settings, image_file, expected_kind="image"
        ),
        upload_file_object(
            client, settings, audio_file, expected_kind="audio"
        ),
    )
    return await generate_impl(
        image_filename=image_filename,
        audio_filename=audio_filename,
        prompt=prompt,
        left_character=left_character,
        right_character=right_character,
        speaker_timeline=speaker_timeline,
        action=action,
        camera=camera,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        duration_seconds=duration_seconds,
        frame_count=frame_count,
        fps=fps,
        seed=seed,
    )


finalize_openai_file_param_schema(
    generate_ltx23_dialogue_from_chatgpt_attachments,
    "image_file",
    "audio_file",
)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def generate_ltx23_dialogue_from_urls(
    image_url: str,
    audio_url: str,
    prompt: str,
    left_character: str = "",
    right_character: str = "",
    speaker_timeline: str = "",
    action: str = "",
    camera: str = "固定中近景，轻微自然呼吸感，不切镜",
    negative_prompt: str = "字幕，文字，水印，角色换位，身份变化，两人同时开口，口型错位，面部畸形，闪烁",
    width: int = 0,
    height: int = 0,
    aspect_ratio: str = "",
    duration_seconds: int = 0,
    frame_count: int = 0,
    fps: int = 0,
    seed: int = -1,
) -> dict[str, Any]:
    """下载公网图片和音频 URL，上传至 RunningHub 后生成双人对话视频。此操作消耗 RH 币。"""

    settings = Settings.from_env()
    client = RunningHubClient(settings)
    image_filename, audio_filename = await asyncio.gather(
        upload_url(client, settings, image_url, expected_kind="image"),
        upload_url(client, settings, audio_url, expected_kind="audio"),
    )
    return await generate_impl(
        image_filename=image_filename,
        audio_filename=audio_filename,
        prompt=prompt,
        left_character=left_character,
        right_character=right_character,
        speaker_timeline=speaker_timeline,
        action=action,
        camera=camera,
        negative_prompt=negative_prompt,
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        duration_seconds=duration_seconds,
        frame_count=frame_count,
        fps=fps,
        seed=seed,
    )


@mcp.tool(
    meta={"openai/fileParams": ["file"]},
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def upload_ltx23_media_from_chatgpt(
    file: OpenAIFile, media_kind: str
) -> dict[str, Any]:
    """只上传一份 ChatGPT 图片或音频附件到 RunningHub，不启动生成任务。"""

    media_kind = ensure_text("media_kind", media_kind, max_length=20).lower()
    if media_kind not in {"image", "audio"}:
        raise ValueError("media_kind 必须是 image 或 audio。")
    settings = Settings.from_env()
    client = RunningHubClient(settings)
    filename = await upload_file_object(
        client, settings, file, expected_kind=media_kind
    )
    return {
        "ok": True,
        "media_kind": media_kind,
        "source_file_id": file.file_id,
        "runninghub_filename": filename,
    }


finalize_openai_file_param_schema(upload_ltx23_media_from_chatgpt, "file")


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def query_ltx23_task(task_id: str, wait_seconds: int = 0) -> dict[str, Any]:
    """查询 LTX 2.3 任务；成功时返回 video_urls。wait_seconds 建议不超过 45 秒。"""

    task_id = ensure_text("task_id", task_id, max_length=200)
    if not task_id:
        raise ValueError("task_id 不能为空。")
    if not 0 <= wait_seconds <= 55:
        raise ValueError("wait_seconds 必须在 0–55 之间。")
    settings = Settings.from_env()
    client = RunningHubClient(settings)
    deadline = time.monotonic() + wait_seconds

    while True:
        try:
            outputs = await client.get_outputs(task_id)
        except RunningHubAPIError as exc:
            code = str(exc.code)
            if code == "804":
                if time.monotonic() < deadline:
                    await asyncio.sleep(settings.poll_interval_seconds)
                    continue
                return {
                    "ok": True,
                    "task_id": task_id,
                    "status": "RUNNING",
                    "next_action": "任务仍在生成，请稍后再次查询。",
                }
            if code == "805":
                await clear_active_task(task_id)
                return {
                    "ok": False,
                    "task_id": task_id,
                    "status": "FAILED",
                    "video_urls": [],
                    "failed_reason": "RunningHub 任务被中断、取消或内部状态异常。",
                }
            raise

        summary = summarize_outputs(outputs)
        if summary.get("video_urls"):
            await clear_active_task(task_id)
            return {
                "ok": True,
                "task_id": task_id,
                "status": "SUCCESS",
                **summary,
            }
        if summary.get("failed_reason"):
            await clear_active_task(task_id)
            return {
                "ok": False,
                "task_id": task_id,
                "status": "FAILED",
                **summary,
            }
        if time.monotonic() < deadline:
            await asyncio.sleep(settings.poll_interval_seconds)
            continue
        return {
            "ok": True,
            "task_id": task_id,
            "status": "RUNNING",
            **summary,
            "next_action": "任务尚未返回视频，请稍后再次查询。",
        }


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    try:
        settings = Settings.from_env(require_credentials=False)
        config_error: str | None = None
    except ConfigurationError as exc:
        settings = None
        config_error = str(exc)
    return JSONResponse(
        {
            "status": "ok",
            "service": "runninghub-ltx23-dialogue-mcp",
            "mcp_path_configured": bool(os.getenv("MCP_PATH", "/mcp").strip()),
            "runninghub_api_key_configured": bool(settings and settings.api_key),
            "runninghub_webapp_id": settings.webapp_id if settings else "",
            "configuration_error": config_error,
        }
    )


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "RunningHub LTX 2.3 Dialogue MCP",
            "health": "/health",
            "mcp": "configured privately",
        }
    )


if __name__ == "__main__":
    mcp_path = os.getenv("MCP_PATH", "/mcp").strip() or "/mcp"
    if not mcp_path.startswith("/"):
        mcp_path = f"/{mcp_path}"
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        path=mcp_path,
    )
