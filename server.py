"""RunningHub Qwen3-TTS -> LTX 2.3 dialogue video orchestrator.

The MCP server accepts a two-person image and dialogue script, submits the
script to a Qwen3-TTS AI App, waits asynchronously through later query calls,
uploads the resulting audio back to RunningHub, and then submits an LTX 2.3
two-person dialogue video task.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from dotenv import load_dotenv

os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
os.environ.setdefault("FASTMCP_SHOW_CLI_BANNER", "false")

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from orchestrator_core import (
    ConfigurationError,
    LTX_ROLE_ALIASES,
    TTS_ROLE_ALIASES,
    NodeTarget,
    PipelineRecord,
    PipelineStore,
    compose_ltx_prompt,
    ensure_text,
    env_bool,
    env_float,
    env_int,
    extract_failure_reason,
    extract_media_urls,
    infer_node_map,
    make_node_info_list,
    parse_extra_node_info,
    parse_node_map,
    summarize_nodes,
    validate_ltx_numbers,
)


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("runninghub-qwen-ltx-orchestrator")


# Known-good input mappings for the two RunningHub AI Apps used by this
# deployment. RunningHub exposes the Qwen text inputs only as generic
# ``String / inStr`` fields, so semantic auto-discovery cannot distinguish the
# dialogue script from the speaker voice prompts. These defaults are applied
# only while the configured WebApp IDs still match the known applications.
# A non-empty Railway environment variable always takes precedence.
DEFAULT_QWEN_TTS_WEBAPP_ID = "2051851200194195458"
DEFAULT_LTX23_WEBAPP_ID = "2048763193677324290"

DEFAULT_QWEN_TTS_NODE_MAP_JSON = json.dumps(
    {
        "script": {"nodeId": "10", "fieldName": "inStr"},
        "voice_a": {"nodeId": "14", "fieldName": "inStr"},
        "voice_b": {"nodeId": "15", "fieldName": "inStr"},
    },
    separators=(",", ":"),
)

DEFAULT_LTX23_NODE_MAP_JSON = json.dumps(
    {
        "image": {"nodeId": "61", "fieldName": "image"},
        "audio": {"nodeId": "60", "fieldName": "audio"},
        # Node #4 is the workflow's CLIP Text Encode (Negative Prompt).
        # Mapping it as a positive prompt makes LTX suppress the requested
        # characters, setting, actions, and camera direction.
        "negative_prompt": {"nodeId": "4", "fieldName": "text"},
        "fps": {"nodeId": "62", "fieldName": "value"},
    },
    separators=(",", ":"),
)


def node_map_json_from_env(
    variable_name: str,
    *,
    configured_webapp_id: str,
    default_webapp_id: str,
    default_json: str,
) -> str:
    """Return an explicit override, or the known mapping for the default app."""

    configured = os.getenv(variable_name, "").strip()
    if configured:
        return configured
    if configured_webapp_id == default_webapp_id:
        return default_json
    return ""


class RunningHubAPIError(RuntimeError):
    """Sanitized RunningHub error safe to return through MCP."""

    def __init__(self, message: str, *, code: str | int | None = None) -> None:
        super().__init__(message)
        self.code = code


class OpenAIFile(BaseModel):
    """ChatGPT attachment parameter supplied to a remote MCP tool."""

    model_config = ConfigDict(extra="forbid")

    download_url: str = Field(description="Temporary URL downloadable by the MCP server")
    file_id: str = Field(description="ChatGPT file identifier")
    mime_type: str = Field(default=None, description="File MIME type")  # type: ignore[assignment]
    file_name: str = Field(default=None, description="Original file name")  # type: ignore[assignment]


def finalize_openai_file_param_schema(tool: Any, *parameter_names: str) -> None:
    """Adjust FastMCP JSON Schema to ChatGPT's file-parameter contract."""

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
class AppConfig:
    name: str
    webapp_id: str
    access_password: str
    node_map: dict[str, NodeTarget] = field(default_factory=dict)
    extra_node_info: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    upload_path: str
    qwen: AppConfig
    ltx: AppConfig
    auto_discover_nodes: bool
    http_timeout_seconds: float
    poll_interval_seconds: float
    max_remote_file_bytes: int
    state_file: str
    state_max_records: int
    state_ttl_seconds: int

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> "Settings":
        api_key = os.getenv("RUNNINGHUB_API_KEY", "").strip()
        if require_credentials and not api_key:
            raise ConfigurationError("缺少 RUNNINGHUB_API_KEY。")

        base_url = os.getenv("RUNNINGHUB_BASE_URL", "https://www.runninghub.cn").strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigurationError("RUNNINGHUB_BASE_URL 必须是有效的 HTTP(S) 地址。")

        upload_path = os.getenv(
            "RUNNINGHUB_UPLOAD_PATH", "/openapi/v2/media/upload/binary"
        ).strip()
        if not upload_path.startswith("/"):
            upload_path = f"/{upload_path}"

        qwen_id = os.getenv("QWEN_TTS_WEBAPP_ID", DEFAULT_QWEN_TTS_WEBAPP_ID).strip()
        ltx_id = os.getenv(
            "LTX23_WEBAPP_ID",
            os.getenv("RUNNINGHUB_WEBAPP_ID", DEFAULT_LTX23_WEBAPP_ID),
        ).strip()
        if require_credentials and not qwen_id:
            raise ConfigurationError("缺少 QWEN_TTS_WEBAPP_ID。")
        if require_credentials and not ltx_id:
            raise ConfigurationError("缺少 LTX23_WEBAPP_ID。")

        qwen = AppConfig(
            name="qwen_tts",
            webapp_id=qwen_id,
            access_password=os.getenv("QWEN_TTS_ACCESS_PASSWORD", "").strip(),
            node_map=parse_node_map(
                node_map_json_from_env(
                    "QWEN_TTS_NODE_MAP_JSON",
                    configured_webapp_id=qwen_id,
                    default_webapp_id=DEFAULT_QWEN_TTS_WEBAPP_ID,
                    default_json=DEFAULT_QWEN_TTS_NODE_MAP_JSON,
                ),
                TTS_ROLE_ALIASES,
                "QWEN_TTS_NODE_MAP_JSON",
            ),
            extra_node_info=parse_extra_node_info(
                os.getenv("QWEN_TTS_EXTRA_NODE_INFO_JSON", ""),
                "QWEN_TTS_EXTRA_NODE_INFO_JSON",
            ),
        )
        ltx = AppConfig(
            name="ltx23",
            webapp_id=ltx_id,
            access_password=os.getenv("LTX23_ACCESS_PASSWORD", "").strip(),
            node_map=parse_node_map(
                node_map_json_from_env(
                    "LTX23_NODE_MAP_JSON",
                    configured_webapp_id=ltx_id,
                    default_webapp_id=DEFAULT_LTX23_WEBAPP_ID,
                    default_json=DEFAULT_LTX23_NODE_MAP_JSON,
                ),
                LTX_ROLE_ALIASES,
                "LTX23_NODE_MAP_JSON",
            ),
            extra_node_info=parse_extra_node_info(
                os.getenv("LTX23_EXTRA_NODE_INFO_JSON", ""),
                "LTX23_EXTRA_NODE_INFO_JSON",
            ),
        )

        return cls(
            api_key=api_key,
            base_url=base_url,
            upload_path=upload_path,
            qwen=qwen,
            ltx=ltx,
            auto_discover_nodes=env_bool("RUNNINGHUB_AUTO_DISCOVER_NODES", True),
            http_timeout_seconds=env_float(
                "RUNNINGHUB_HTTP_TIMEOUT_SECONDS", 120.0, minimum=10.0, maximum=600.0
            ),
            poll_interval_seconds=env_float(
                "RUNNINGHUB_POLL_INTERVAL_SECONDS", 5.0, minimum=1.0, maximum=30.0
            ),
            max_remote_file_bytes=env_int(
                "MAX_REMOTE_FILE_BYTES",
                200 * 1024 * 1024,
                minimum=1 * 1024 * 1024,
                maximum=1024 * 1024 * 1024,
            ),
            state_file=os.getenv(
                "PIPELINE_STATE_FILE", "/tmp/runninghub-dialogue-pipelines.json"
            ).strip()
            or "/tmp/runninghub-dialogue-pipelines.json",
            state_max_records=env_int(
                "PIPELINE_STATE_MAX_RECORDS", 200, minimum=10, maximum=5000
            ),
            state_ttl_seconds=env_int(
                "PIPELINE_STATE_TTL_SECONDS", 604800, minimum=3600, maximum=2592000
            ),
        )


class RunningHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.api_key}",
            "User-Agent": "runninghub-qwen-ltx-orchestrator/2.0",
        }

    def _unwrap(self, body: Any) -> Any:
        if not isinstance(body, dict) or "code" not in body:
            return body
        code = body.get("code")
        if str(code) not in {"0", "200"}:
            message = str(body.get("msg") or body.get("message") or "未知错误")
            message = message.replace(self.settings.api_key, "***")
            raise RunningHubAPIError(f"RunningHub API 错误 {code}：{message}", code=code)
        return body.get("data")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        method = method.upper()
        request_payload = dict(payload or {})
        request_params = dict(params or {})
        if method == "GET":
            request_params["apiKey"] = self.settings.api_key
        else:
            request_payload["apiKey"] = self.settings.api_key

        headers = dict(self.headers)
        if method != "GET":
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
                        json=request_payload if method != "GET" else None,
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

    async def get_ai_app_demo(self, webapp_id: str) -> dict[str, Any]:
        data = await self.request_json(
            "GET", "/api/webapp/apiCallDemo", params={"webappId": webapp_id}
        )
        if not isinstance(data, dict):
            raise RunningHubAPIError("AI 应用调用示例返回格式不正确。")
        return data

    async def run_ai_app(
        self, app: AppConfig, node_info_list: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "webappId": app.webapp_id,
            "nodeInfoList": node_info_list,
        }
        if app.access_password:
            payload["accessPassword"] = app.access_password
        data = await self.request_json("POST", "/task/openapi/ai-app/run", payload=payload)
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

    async def get_outputs(self, task_id: str) -> Any:
        return await self.request_json(
            "POST", "/task/openapi/outputs", payload={"taskId": task_id}
        )

    async def upload_bytes(self, content: bytes, filename: str, content_type: str) -> str:
        if not content:
            raise ValueError(f"{filename} 是空文件。")
        headers = dict(self.headers)
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    base_url=self.settings.base_url,
                    timeout=max(self.settings.http_timeout_seconds, 300.0),
                    headers=headers,
                    follow_redirects=True,
                ) as http:
                    response = await http.post(
                        self.settings.upload_path,
                        files={"file": (filename, content, content_type)},
                    )
            except httpx.RequestError as exc:
                if attempt < 2:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
                raise RunningHubAPIError(
                    f"上传到 RunningHub 失败：{type(exc).__name__}。"
                ) from exc
            if response.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(1.5 * (2**attempt))
                continue
            if response.is_error:
                safe_text = response.text.replace(self.settings.api_key, "***")[:500]
                raise RunningHubAPIError(
                    f"RunningHub 上传 HTTP {response.status_code}：{safe_text}",
                    code=response.status_code,
                )
            try:
                data = self._unwrap(response.json())
            except ValueError as exc:
                raise RunningHubAPIError("RunningHub 上传接口返回了非 JSON 响应。") from exc
            if not isinstance(data, dict):
                raise RunningHubAPIError("RunningHub 上传接口没有返回文件信息。")
            uploaded = str(
                data.get("filename") or data.get("fileName") or data.get("name") or ""
            ).strip()
            if not uploaded:
                raise RunningHubAPIError("RunningHub 上传成功响应中缺少 filename。")
            return uploaded
        raise RunningHubAPIError("RunningHub 上传在重试后仍然失败。")


_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_pipeline_locks: dict[str, asyncio.Lock] = {}
_submit_lock = asyncio.Lock()


def get_store(settings: Settings) -> PipelineStore:
    return PipelineStore(
        settings.state_file,
        max_records=settings.state_max_records,
        ttl_seconds=settings.state_ttl_seconds,
    )


async def get_discovery(
    settings: Settings, client: RunningHubClient, app: AppConfig
) -> dict[str, Any]:
    cache_key = f"{settings.base_url}|{app.webapp_id}"
    cached = _discovery_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    demo = await client.get_ai_app_demo(app.webapp_id)
    _discovery_cache[cache_key] = (time.monotonic(), demo)
    return demo


async def resolve_app_nodes(
    settings: Settings,
    client: RunningHubClient,
    app: AppConfig,
    aliases: dict[str, tuple[str, ...]],
) -> tuple[dict[str, NodeTarget], dict[str, Any]]:
    demo = await get_discovery(settings, client, app)
    nodes = [item for item in demo.get("nodeInfoList") or [] if isinstance(item, dict)]
    resolved: dict[str, NodeTarget] = {}
    if settings.auto_discover_nodes:
        resolved.update(infer_node_map(nodes, aliases))
    resolved.update(app.node_map)
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


def _fallback_filename(url: str, preferred: str, kind: str) -> str:
    if preferred:
        return Path(preferred).name[:240]
    candidate = Path(unquote(urlparse(url).path)).name
    if candidate:
        return candidate[:240]
    return "input.png" if kind == "image" else "dialogue.wav"


def validate_media_type(filename: str, content_type: str, expected_kind: str) -> None:
    suffix = Path(filename).suffix.lower()
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    audio_extensions = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
    if expected_kind == "image":
        if not (mime.startswith("image/") or suffix in image_extensions):
            raise ValueError("双人首帧附件必须是 JPG、PNG 或 WebP 图片。")
    elif expected_kind == "audio":
        if not (mime.startswith("audio/") or suffix in audio_extensions):
            raise ValueError("对白附件必须是 WAV、MP3、M4A、AAC、FLAC、OGG 或 OPUS 音频。")
    else:
        raise ValueError("未知媒体类型。")


async def download_public_file(
    settings: Settings,
    url: str,
    *,
    preferred_filename: str = "",
    expected_kind: str,
) -> tuple[bytes, str, str]:
    current_url = _ensure_public_http_url(ensure_text("url", url, max_length=5000))
    async with httpx.AsyncClient(
        timeout=max(settings.http_timeout_seconds, 300.0), follow_redirects=False
    ) as http:
        for redirect_count in range(6):
            try:
                async with http.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "").strip()
                        if not location:
                            raise RunningHubAPIError("远程素材重定向缺少 Location。")
                        if redirect_count >= 5:
                            raise RunningHubAPIError("远程素材重定向次数过多。")
                        current_url = _ensure_public_http_url(urljoin(current_url, location))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    filename = _fallback_filename(
                        current_url, preferred_filename, expected_kind
                    )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > settings.max_remote_file_bytes:
                            raise ValueError(
                                f"远程文件超过 {settings.max_remote_file_bytes // (1024 * 1024)} MiB 限制。"
                            )
                        chunks.append(chunk)
                    break
            except httpx.HTTPError as exc:
                raise RunningHubAPIError(
                    f"下载远程素材失败：{type(exc).__name__}。"
                ) from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise RunningHubAPIError("远程素材下载失败。")
    validate_media_type(filename, content_type, expected_kind)
    guessed = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return b"".join(chunks), filename, guessed


async def upload_file_object(
    client: RunningHubClient,
    settings: Settings,
    file: OpenAIFile,
    *,
    expected_kind: str,
) -> str:
    content, filename, content_type = await download_public_file(
        settings,
        file.download_url,
        preferred_filename=file.file_name or "",
        expected_kind=expected_kind,
    )
    if file.mime_type:
        validate_media_type(filename, file.mime_type, expected_kind)
        content_type = file.mime_type
    return await client.upload_bytes(content, filename, content_type)


async def upload_url(
    client: RunningHubClient,
    settings: Settings,
    url: str,
    *,
    expected_kind: str,
) -> str:
    content, filename, content_type = await download_public_file(
        settings, url, expected_kind=expected_kind
    )
    return await client.upload_bytes(content, filename, content_type)


def tts_values(
    *,
    dialogue_script: str,
    voice_a_prompt: str,
    voice_b_prompt: str,
    sentence_pause: float,
    punctuation_pause: float,
    seed: int,
) -> dict[str, Any]:
    dialogue_script = ensure_text("dialogue_script", dialogue_script, max_length=12000)
    if not dialogue_script:
        raise ValueError("dialogue_script 不能为空。")
    if sentence_pause < 0 or sentence_pause > 10:
        raise ValueError("sentence_pause 必须在 0–10 之间；0 表示使用应用默认值。")
    if punctuation_pause < 0 or punctuation_pause > 10:
        raise ValueError("punctuation_pause 必须在 0–10 之间；0 表示使用应用默认值。")
    if seed < -1:
        raise ValueError("tts_seed 必须为 -1 或非负整数。")
    return {
        "script": dialogue_script,
        "voice_a": ensure_text("voice_a_prompt", voice_a_prompt, max_length=2000),
        "voice_b": ensure_text("voice_b_prompt", voice_b_prompt, max_length=2000),
        "sentence_pause": sentence_pause if sentence_pause > 0 else None,
        "punctuation_pause": punctuation_pause if punctuation_pause > 0 else None,
        "seed": seed if seed >= 0 else None,
    }


def ltx_values(image_filename: str, audio_filename: str, inputs: dict[str, Any]) -> dict[str, Any]:
    validate_ltx_numbers(
        width=int(inputs.get("width") or 0),
        height=int(inputs.get("height") or 0),
        duration_seconds=int(inputs.get("duration_seconds") or 0),
        frame_count=int(inputs.get("frame_count") or 0),
        fps=int(inputs.get("fps") or 0),
        seed=int(inputs.get("ltx_seed", -1)),
    )
    full_prompt = compose_ltx_prompt(
        prompt=str(inputs.get("video_prompt") or ""),
        left_character=str(inputs.get("left_character") or ""),
        right_character=str(inputs.get("right_character") or ""),
        speaker_timeline=str(inputs.get("speaker_timeline") or ""),
        action=str(inputs.get("action") or ""),
        camera=str(inputs.get("camera") or ""),
    )
    return {
        "image": image_filename,
        "audio": audio_filename,
        "prompt": full_prompt,
        "negative_prompt": str(inputs.get("negative_prompt") or ""),
        "width": int(inputs.get("width") or 0) or None,
        "height": int(inputs.get("height") or 0) or None,
        "aspect_ratio": str(inputs.get("aspect_ratio") or ""),
        "duration_seconds": int(inputs.get("duration_seconds") or 0) or None,
        "frame_count": int(inputs.get("frame_count") or 0) or None,
        "fps": int(inputs.get("fps") or 0) or None,
        "seed": int(inputs.get("ltx_seed", -1)) if int(inputs.get("ltx_seed", -1)) >= 0 else None,
    }


def pipeline_inputs(
    *,
    dialogue_script: str,
    voice_a_prompt: str,
    voice_b_prompt: str,
    sentence_pause: float,
    punctuation_pause: float,
    tts_seed: int,
    video_prompt: str,
    left_character: str,
    right_character: str,
    speaker_timeline: str,
    action: str,
    camera: str,
    negative_prompt: str,
    width: int,
    height: int,
    aspect_ratio: str,
    duration_seconds: int,
    frame_count: int,
    fps: int,
    ltx_seed: int,
    auto_start_ltx: bool,
) -> dict[str, Any]:
    # Validation occurs before a paid task is submitted.
    tts_values(
        dialogue_script=dialogue_script,
        voice_a_prompt=voice_a_prompt,
        voice_b_prompt=voice_b_prompt,
        sentence_pause=sentence_pause,
        punctuation_pause=punctuation_pause,
        seed=tts_seed,
    )
    validate_ltx_numbers(
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        frame_count=frame_count,
        fps=fps,
        seed=ltx_seed,
    )
    if not ensure_text("video_prompt", video_prompt, max_length=6000):
        raise ValueError("video_prompt 不能为空。")
    return {
        "dialogue_script": dialogue_script.strip(),
        "voice_a_prompt": voice_a_prompt.strip(),
        "voice_b_prompt": voice_b_prompt.strip(),
        "sentence_pause": sentence_pause,
        "punctuation_pause": punctuation_pause,
        "tts_seed": tts_seed,
        "video_prompt": video_prompt.strip(),
        "left_character": ensure_text("left_character", left_character, max_length=1200),
        "right_character": ensure_text("right_character", right_character, max_length=1200),
        "speaker_timeline": ensure_text("speaker_timeline", speaker_timeline, max_length=3000),
        "action": ensure_text("action", action, max_length=3000),
        "camera": ensure_text("camera", camera, max_length=800),
        "negative_prompt": ensure_text("negative_prompt", negative_prompt, max_length=3000),
        "width": width,
        "height": height,
        "aspect_ratio": ensure_text("aspect_ratio", aspect_ratio, max_length=30),
        "duration_seconds": duration_seconds,
        "frame_count": frame_count,
        "fps": fps,
        "ltx_seed": ltx_seed,
        "auto_start_ltx": bool(auto_start_ltx),
    }


def pipeline_summary(record: PipelineRecord) -> dict[str, Any]:
    next_action = ""
    if record.status in {"TTS_SUBMITTED", "TTS_RUNNING", "LTX_SUBMITTED", "LTX_RUNNING"}:
        next_action = "稍后再次调用 query_dialogue_pipeline，并传入同一个 pipeline_id。"
    elif record.status == "WAITING_FOR_LTX":
        next_action = (
            "音频已经生成。检查 audio_url 后调用 continue_dialogue_pipeline_to_ltx；"
            "可以同时补充准确的 speaker_timeline。"
        )
    return {
        "ok": record.status != "FAILED",
        "pipeline_id": record.pipeline_id,
        "status": record.status,
        "tts_task_id": record.tts_task_id,
        "ltx_task_id": record.ltx_task_id,
        "audio_url": record.audio_url,
        "video_urls": record.video_urls,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "next_action": next_action,
    }


async def submit_tts_pipeline(
    *, image_filename: str, inputs: dict[str, Any]
) -> dict[str, Any]:
    settings = Settings.from_env()
    client = RunningHubClient(settings)
    qwen_map, _ = await resolve_app_nodes(
        settings, client, settings.qwen, TTS_ROLE_ALIASES
    )
    if "script" not in qwen_map:
        raise ConfigurationError(
            "无法确定 Qwen3-TTS 的剧本节点。请先调用 inspect_dialogue_pipeline，"
            "再配置 QWEN_TTS_NODE_MAP_JSON。"
        )

    values = tts_values(
        dialogue_script=str(inputs["dialogue_script"]),
        voice_a_prompt=str(inputs.get("voice_a_prompt") or ""),
        voice_b_prompt=str(inputs.get("voice_b_prompt") or ""),
        sentence_pause=float(inputs.get("sentence_pause") or 0),
        punctuation_pause=float(inputs.get("punctuation_pause") or 0),
        seed=int(inputs.get("tts_seed", -1)),
    )
    nodes = make_node_info_list(qwen_map, values, settings.qwen.extra_node_info)
    used_optional = {
        role for role in ("voice_a", "voice_b", "sentence_pause", "punctuation_pause", "seed")
        if values.get(role) is not None and values.get(role) != ""
    }
    unmapped_optional = sorted(role for role in used_optional if role not in qwen_map)

    async with _submit_lock:
        task = await client.run_ai_app(settings.qwen, nodes)

    now = time.time()
    record = PipelineRecord(
        pipeline_id=secrets.token_urlsafe(18),
        status="TTS_SUBMITTED",
        created_at=now,
        updated_at=now,
        image_filename=image_filename,
        tts_task_id=str(task["taskId"]),
        inputs=inputs,
    )
    await get_store(settings).put(record)
    result = pipeline_summary(record)
    result["warnings"] = (
        [
            "以下可选 Qwen 输入没有映射，将使用应用默认值："
            + ", ".join(unmapped_optional)
        ]
        if unmapped_optional
        else []
    )
    result["generation_will_consume_credits"] = True
    return result


async def submit_ltx_for_record(
    settings: Settings,
    client: RunningHubClient,
    record: PipelineRecord,
) -> PipelineRecord:
    ltx_map, _ = await resolve_app_nodes(settings, client, settings.ltx, LTX_ROLE_ALIASES)
    # This deployed LTX workflow exposes its reference image and driving audio,
    # but node #4 is a *negative* prompt rather than a positive prompt. Do not
    # require a positive prompt node unless the workflow is later changed and
    # an explicit mapping is configured for one.
    missing = [role for role in ("image", "audio") if role not in ltx_map]
    if missing:
        raise ConfigurationError(
            "无法确定 LTX 2.3 必需节点："
            + ", ".join(missing)
            + "。请调用 inspect_dialogue_pipeline 并配置 LTX23_NODE_MAP_JSON。"
        )
    values = ltx_values(record.image_filename, record.audio_filename, record.inputs)
    nodes = make_node_info_list(ltx_map, values, settings.ltx.extra_node_info)
    async with _submit_lock:
        task = await client.run_ai_app(settings.ltx, nodes)
    record.ltx_task_id = str(task["taskId"])
    record.status = "LTX_SUBMITTED"
    return await get_store(settings).put(record)


async def runninghub_task_state(
    client: RunningHubClient, task_id: str, media_kind: str
) -> tuple[str, list[str], str]:
    try:
        outputs = await client.get_outputs(task_id)
    except RunningHubAPIError as exc:
        code = str(exc.code)
        if code == "804":
            return "RUNNING", [], ""
        if code == "805":
            return "FAILED", [], "RunningHub 任务被中断、取消或内部状态异常。"
        raise
    urls = extract_media_urls(outputs, media_kind)
    if urls:
        return "SUCCESS", urls, ""
    reason = extract_failure_reason(outputs)
    if reason:
        return "FAILED", [], reason
    return "RUNNING", [], ""


async def advance_pipeline(record: PipelineRecord) -> PipelineRecord:
    settings = Settings.from_env()
    client = RunningHubClient(settings)
    store = get_store(settings)

    if record.status in {"SUCCESS", "FAILED"}:
        return record

    if record.status in {"TTS_SUBMITTED", "TTS_RUNNING"}:
        state, urls, reason = await runninghub_task_state(client, record.tts_task_id, "audio")
        if state == "FAILED":
            record.status = "FAILED"
            record.error = f"Qwen3-TTS 阶段失败：{reason}"
            return await store.put(record)
        if state == "RUNNING":
            record.status = "TTS_RUNNING"
            return await store.put(record)

        record.audio_url = urls[0]
        try:
            content, filename, content_type = await download_public_file(
                settings, record.audio_url, expected_kind="audio"
            )
            record.audio_filename = await client.upload_bytes(content, filename, content_type)
            record.status = "TTS_READY"
            record = await store.put(record)
            if not bool(record.inputs.get("auto_start_ltx", True)):
                record.status = "WAITING_FOR_LTX"
                return await store.put(record)
            return await submit_ltx_for_record(settings, client, record)
        except Exception as exc:
            record.status = "FAILED"
            record.error = f"音频转交给 LTX 失败：{str(exc)[:800]}"
            return await store.put(record)

    if record.status == "TTS_READY":
        try:
            return await submit_ltx_for_record(settings, client, record)
        except Exception as exc:
            record.status = "FAILED"
            record.error = f"LTX 提交失败：{str(exc)[:800]}"
            return await store.put(record)

    if record.status == "WAITING_FOR_LTX":
        return record

    if record.status in {"LTX_SUBMITTED", "LTX_RUNNING"}:
        state, urls, reason = await runninghub_task_state(client, record.ltx_task_id, "video")
        if state == "FAILED":
            record.status = "FAILED"
            record.error = f"LTX 2.3 阶段失败：{reason}"
        elif state == "SUCCESS":
            record.status = "SUCCESS"
            record.video_urls = urls
        else:
            record.status = "LTX_RUNNING"
        return await store.put(record)

    unknown_status = record.status
    record.status = "FAILED"
    record.error = f"未知流水线状态：{unknown_status}"
    return await store.put(record)


async def inspect_one_app(
    settings: Settings,
    client: RunningHubClient,
    app: AppConfig,
    aliases: dict[str, tuple[str, ...]],
    required_roles: tuple[str, ...],
) -> dict[str, Any]:
    resolved, demo = await resolve_app_nodes(settings, client, app, aliases)
    nodes = [item for item in demo.get("nodeInfoList") or [] if isinstance(item, dict)]
    missing = [role for role in required_roles if role not in resolved]
    return {
        "ok": not missing,
        "webapp_id": app.webapp_id,
        "webapp_name": str(demo.get("webappName", "")),
        "access_encrypted": bool(demo.get("accessEncrypted", False)),
        "resolved_node_map": {
            role: target.to_dict() for role, target in sorted(resolved.items())
        },
        "missing_required_roles": missing,
        "available_nodes": summarize_nodes(nodes),
    }


SERVER_INSTRUCTIONS = (
    "This server orchestrates a RunningHub Qwen3-TTS dialogue task followed by an "
    "LTX 2.3 two-person dialogue video task. Call inspect_dialogue_pipeline once "
    "after deployment. For a ChatGPT image attachment, call "
    "start_dialogue_video_from_script. It returns a pipeline_id. Repeatedly call "
    "query_dialogue_pipeline with that pipeline_id until SUCCESS or FAILED. "
    "Both stages consume RunningHub credits."
)

mcp = FastMCP("RunningHub Qwen3-TTS + LTX 2.3", instructions=SERVER_INSTRUCTIONS)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def inspect_dialogue_pipeline() -> dict[str, Any]:
    """只读检查 Qwen3-TTS 与 LTX 2.3 两个应用的节点映射，不消耗生成额度。"""

    settings = Settings.from_env()
    client = RunningHubClient(settings)
    qwen_result, ltx_result = await asyncio.gather(
        inspect_one_app(
            settings, client, settings.qwen, TTS_ROLE_ALIASES, ("script",)
        ),
        inspect_one_app(
            settings, client, settings.ltx, LTX_ROLE_ALIASES, ("image", "audio")
        ),
    )
    return {
        "ok": qwen_result["ok"] and ltx_result["ok"],
        "base_url": settings.base_url,
        "qwen_tts": qwen_result,
        "ltx23": ltx_result,
        "state_file": settings.state_file,
        "generation_will_consume_credits": True,
    }


@mcp.tool(
    meta={"openai/fileParams": ["image_file"]},
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def start_dialogue_video_from_script(
    image_file: OpenAIFile,
    dialogue_script: str,
    video_prompt: str,
    voice_a_prompt: str = "年轻中国女性，清亮自然，普通话，语速中等，情绪自然",
    voice_b_prompt: str = "年轻中国男性，温和略低沉，普通话，语速中等，情绪自然",
    sentence_pause: float = 0.0,
    punctuation_pause: float = 0.0,
    left_character: str = "",
    right_character: str = "",
    speaker_timeline: str = "",
    action: str = "说话者自然张嘴并有小幅表情和手势；未说话者自然倾听",
    camera: str = "固定中近景，轻微自然呼吸感，不切镜",
    negative_prompt: str = "字幕，文字，水印，角色换位，身份变化，两人同时开口，口型错位，面部畸形，闪烁",
    width: int = 0,
    height: int = 0,
    aspect_ratio: str = "",
    duration_seconds: int = 0,
    frame_count: int = 0,
    fps: int = 0,
    tts_seed: int = -1,
    ltx_seed: int = -1,
    auto_start_ltx: bool = True,
) -> dict[str, Any]:
    """上传双人图片并启动“台词→Qwen音频→LTX视频”流水线。会消耗 RH 币。"""

    inputs = pipeline_inputs(
        dialogue_script=dialogue_script,
        voice_a_prompt=voice_a_prompt,
        voice_b_prompt=voice_b_prompt,
        sentence_pause=sentence_pause,
        punctuation_pause=punctuation_pause,
        tts_seed=tts_seed,
        video_prompt=video_prompt,
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
        ltx_seed=ltx_seed,
        auto_start_ltx=auto_start_ltx,
    )
    settings = Settings.from_env()
    client = RunningHubClient(settings)
    image_filename = await upload_file_object(
        client, settings, image_file, expected_kind="image"
    )
    return await submit_tts_pipeline(image_filename=image_filename, inputs=inputs)


finalize_openai_file_param_schema(start_dialogue_video_from_script, "image_file")


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def start_dialogue_video_from_script_url(
    image_url: str,
    dialogue_script: str,
    video_prompt: str,
    voice_a_prompt: str = "年轻中国女性，清亮自然，普通话，语速中等，情绪自然",
    voice_b_prompt: str = "年轻中国男性，温和略低沉，普通话，语速中等，情绪自然",
    sentence_pause: float = 0.0,
    punctuation_pause: float = 0.0,
    left_character: str = "",
    right_character: str = "",
    speaker_timeline: str = "",
    action: str = "说话者自然张嘴并有小幅表情和手势；未说话者自然倾听",
    camera: str = "固定中近景，轻微自然呼吸感，不切镜",
    negative_prompt: str = "字幕，文字，水印，角色换位，身份变化，两人同时开口，口型错位，面部畸形，闪烁",
    width: int = 0,
    height: int = 0,
    aspect_ratio: str = "",
    duration_seconds: int = 0,
    frame_count: int = 0,
    fps: int = 0,
    tts_seed: int = -1,
    ltx_seed: int = -1,
    auto_start_ltx: bool = True,
) -> dict[str, Any]:
    """下载公网双人图片并启动完整对话视频流水线。会消耗 RH 币。"""

    inputs = pipeline_inputs(
        dialogue_script=dialogue_script,
        voice_a_prompt=voice_a_prompt,
        voice_b_prompt=voice_b_prompt,
        sentence_pause=sentence_pause,
        punctuation_pause=punctuation_pause,
        tts_seed=tts_seed,
        video_prompt=video_prompt,
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
        ltx_seed=ltx_seed,
        auto_start_ltx=auto_start_ltx,
    )
    settings = Settings.from_env()
    client = RunningHubClient(settings)
    image_filename = await upload_url(
        client, settings, image_url, expected_kind="image"
    )
    return await submit_tts_pipeline(image_filename=image_filename, inputs=inputs)


@mcp.tool(
    meta={"openai/fileParams": ["image_file", "audio_file"]},
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def start_ltx23_from_ready_media(
    image_file: OpenAIFile,
    audio_file: OpenAIFile,
    video_prompt: str,
    left_character: str = "",
    right_character: str = "",
    speaker_timeline: str = "",
    action: str = "说话者自然张嘴并有小幅表情和手势；未说话者自然倾听",
    camera: str = "固定中近景，轻微自然呼吸感，不切镜",
    negative_prompt: str = "字幕，文字，水印，角色换位，身份变化，两人同时开口，口型错位，面部畸形，闪烁",
    width: int = 0,
    height: int = 0,
    aspect_ratio: str = "",
    duration_seconds: int = 0,
    frame_count: int = 0,
    fps: int = 0,
    ltx_seed: int = -1,
) -> dict[str, Any]:
    """跳过 Qwen 阶段，使用现成图片和完整对白音频启动 LTX。会消耗 RH 币。"""

    inputs = pipeline_inputs(
        dialogue_script="已提供现成音频",
        voice_a_prompt="",
        voice_b_prompt="",
        sentence_pause=0.0,
        punctuation_pause=0.0,
        tts_seed=-1,
        video_prompt=video_prompt,
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
        ltx_seed=ltx_seed,
        auto_start_ltx=True,
    )
    settings = Settings.from_env()
    client = RunningHubClient(settings)
    image_filename, audio_filename = await asyncio.gather(
        upload_file_object(client, settings, image_file, expected_kind="image"),
        upload_file_object(client, settings, audio_file, expected_kind="audio"),
    )
    now = time.time()
    record = PipelineRecord(
        pipeline_id=secrets.token_urlsafe(18),
        status="TTS_READY",
        created_at=now,
        updated_at=now,
        image_filename=image_filename,
        audio_filename=audio_filename,
        inputs=inputs,
    )
    await get_store(settings).put(record)
    record = await submit_ltx_for_record(settings, client, record)
    result = pipeline_summary(record)
    result["generation_will_consume_credits"] = True
    return result


finalize_openai_file_param_schema(start_ltx23_from_ready_media, "image_file", "audio_file")


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def continue_dialogue_pipeline_to_ltx(
    pipeline_id: str,
    speaker_timeline: str = "",
    video_prompt: str = "",
) -> dict[str, Any]:
    """将暂停在音频阶段的流水线继续提交给 LTX；可补充准确说话时间线。"""

    pipeline_id = ensure_text("pipeline_id", pipeline_id, max_length=200)
    if not pipeline_id:
        raise ValueError("pipeline_id 不能为空。")
    settings = Settings.from_env()
    store = get_store(settings)
    lock = _pipeline_locks.setdefault(pipeline_id, asyncio.Lock())
    async with lock:
        record = await store.get(pipeline_id)
        if record is None:
            return {
                "ok": False,
                "pipeline_id": pipeline_id,
                "status": "NOT_FOUND",
                "error": "找不到该流水线。",
            }
        if record.status != "WAITING_FOR_LTX":
            return {
                **pipeline_summary(record),
                "ok": False,
                "error": "只有 WAITING_FOR_LTX 状态可以调用此工具。",
            }
        if speaker_timeline.strip():
            record.inputs["speaker_timeline"] = ensure_text(
                "speaker_timeline", speaker_timeline, max_length=3000
            )
        if video_prompt.strip():
            record.inputs["video_prompt"] = ensure_text(
                "video_prompt", video_prompt, max_length=6000
            )
        record.inputs["auto_start_ltx"] = True
        record.status = "TTS_READY"
        await store.put(record)
        try:
            record = await submit_ltx_for_record(
                settings, RunningHubClient(settings), record
            )
        except Exception as exc:
            record.status = "FAILED"
            record.error = f"LTX 提交失败：{str(exc)[:800]}"
            record = await store.put(record)
        return pipeline_summary(record)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def query_dialogue_pipeline(
    pipeline_id: str, wait_seconds: int = 0
) -> dict[str, Any]:
    """推进并查询完整流水线；成功时返回 audio_url 和 video_urls。"""

    pipeline_id = ensure_text("pipeline_id", pipeline_id, max_length=200)
    if not pipeline_id:
        raise ValueError("pipeline_id 不能为空。")
    if not 0 <= wait_seconds <= 55:
        raise ValueError("wait_seconds 必须在 0–55 之间。")
    settings = Settings.from_env()
    store = get_store(settings)
    lock = _pipeline_locks.setdefault(pipeline_id, asyncio.Lock())
    deadline = time.monotonic() + wait_seconds

    async with lock:
        record = await store.get(pipeline_id)
        if record is None:
            return {
                "ok": False,
                "pipeline_id": pipeline_id,
                "status": "NOT_FOUND",
                "error": "找不到该流水线。若 Railway 刚重新部署，请检查是否配置了持久化 Volume。",
            }
        while True:
            record = await advance_pipeline(record)
            if record.status in {"SUCCESS", "FAILED", "WAITING_FOR_LTX"} or time.monotonic() >= deadline:
                return pipeline_summary(record)
            await asyncio.sleep(settings.poll_interval_seconds)


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_recent_dialogue_pipelines(limit: int = 10) -> dict[str, Any]:
    """列出最近的流水线 ID 和状态，便于恢复查询；不会访问 RunningHub。"""

    if not 1 <= limit <= 50:
        raise ValueError("limit 必须在 1–50 之间。")
    settings = Settings.from_env()
    records = await get_store(settings).list_recent(limit)
    return {
        "ok": True,
        "pipelines": [pipeline_summary(record) for record in records],
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
            "service": "runninghub-qwen-ltx-orchestrator",
            "mcp_path_configured": bool(os.getenv("MCP_PATH", "/mcp").strip()),
            "runninghub_api_key_configured": bool(settings and settings.api_key),
            "qwen_tts_webapp_id": settings.qwen.webapp_id if settings else "",
            "ltx23_webapp_id": settings.ltx.webapp_id if settings else "",
            "state_file": settings.state_file if settings else "",
            "configuration_error": config_error,
        }
    )


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "RunningHub Qwen3-TTS + LTX 2.3 Orchestrator",
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
