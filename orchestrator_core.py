"""Pure-Python core helpers for the RunningHub dialogue orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


class ConfigurationError(ValueError):
    """Raised when deployment configuration is incomplete or invalid."""


@dataclass(frozen=True)
class NodeTarget:
    node_id: str
    field_name: str

    def to_dict(self) -> dict[str, str]:
        return {"nodeId": self.node_id, "fieldName": self.field_name}


TTS_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "script": (
        "dialogue_script",
        "dialogue_text",
        "script_text",
        "conversation",
        "script",
        "对话剧本",
        "剧本内容",
        "人物对话",
        "对话内容",
        "台词内容",
        "剧本",
        "台词",
        "text",
    ),
    "voice_a": (
        "speaker_1_instruct",
        "speaker1_instruct",
        "speaker_1_prompt",
        "speaker1_prompt",
        "speaker_a_prompt",
        "role_1_instruct",
        "role1_instruct",
        "role_1_prompt",
        "role1_prompt",
        "voice_1_instruct",
        "voice1_instruct",
        "voice_1_prompt",
        "voice1_prompt",
        "voice_a_prompt",
        "speaker_1_style",
        "voice_1_style",
        "角色1声音",
        "角色一声音",
        "角色1音色",
        "角色1提示词",
        "音色提示词1",
        "说话人1",
        "音色1",
        "声音1",
        "女声风格",
    ),
    "voice_b": (
        "speaker_2_instruct",
        "speaker2_instruct",
        "speaker_2_prompt",
        "speaker2_prompt",
        "speaker_b_prompt",
        "role_2_instruct",
        "role2_instruct",
        "role_2_prompt",
        "role2_prompt",
        "voice_2_instruct",
        "voice2_instruct",
        "voice_2_prompt",
        "voice2_prompt",
        "voice_b_prompt",
        "speaker_2_style",
        "voice_2_style",
        "角色2声音",
        "角色二声音",
        "角色2音色",
        "角色2提示词",
        "音色提示词2",
        "说话人2",
        "音色2",
        "声音2",
        "男声风格",
    ),
    "sentence_pause": (
        "sentence_pause",
        "sentence_gap",
        "sentence_silence",
        "inter_sentence_pause",
        "interval_between_sentences",
        "句间停顿",
        "句子停顿",
        "句间隔",
    ),
    "punctuation_pause": (
        "punctuation_pause",
        "punctuation_gap",
        "comma_pause",
        "punctuation_silence",
        "标点停顿",
        "逗号停顿",
    ),
    "seed": ("seed", "random_seed", "随机种子", "种子"),
}


LTX_ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "image": (
        "input_image",
        "reference_image",
        "start_image",
        "first_frame",
        "image",
        "双人首帧",
        "首帧",
        "双人图",
        "参考图",
        "上传图像",
        "上传图片",
        "图片",
        "图像",
    ),
    "audio": (
        "dialogue_audio",
        "driving_audio",
        "reference_audio",
        "input_audio",
        "audio",
        "完整对白音频",
        "对话音频",
        "驱动音频",
        "参考音频",
        "上传音频",
        "音频",
        "声音",
    ),
    "prompt": (
        "video_prompt",
        "positive_prompt",
        "text_prompt",
        "prompt",
        "视频提示词",
        "正向提示词",
        "提示词",
    ),
    "negative_prompt": (
        "negative_prompt",
        "negative",
        "反向提示词",
        "负面提示词",
    ),
    "width": ("video_width", "width", "视频宽度", "宽度"),
    "height": ("video_height", "height", "视频高度", "高度"),
    "aspect_ratio": ("aspect_ratio", "ratio", "宽高比", "画幅", "比例"),
    "duration_seconds": (
        "duration_seconds",
        "video_length",
        "duration",
        "seconds",
        "视频时长",
        "时长",
        "秒数",
    ),
    "frame_count": (
        "frame_count",
        "num_frames",
        "video_frames",
        "总帧数",
        "帧数",
    ),
    "fps": ("frame_rate", "framerate", "fps", "帧率"),
    "seed": ("seed", "random_seed", "随机种子", "种子"),
}


def ensure_text(name: str, value: str, *, max_length: int) -> str:
    result = str(value or "").strip()
    if len(result) > max_length:
        raise ValueError(f"{name} 不能超过 {max_length} 个字符。")
    return result


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


def parse_node_map(raw: str, allowed_roles: Iterable[str], variable_name: str) -> dict[str, NodeTarget]:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{variable_name} 不是有效 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{variable_name} 必须是 JSON 对象。")

    allowed = set(allowed_roles)
    result: dict[str, NodeTarget] = {}
    for role, target in payload.items():
        if role not in allowed:
            raise ConfigurationError(
                f"{variable_name} 包含不支持的角色 {role!r}；允许值：{', '.join(sorted(allowed))}"
            )
        if isinstance(target, str) and ":" in target:
            node_id, field_name = target.split(":", 1)
        elif isinstance(target, dict):
            node_id = str(target.get("nodeId") or target.get("node_id") or "")
            field_name = str(target.get("fieldName") or target.get("field_name") or "")
        else:
            raise ConfigurationError(
                f"{variable_name} 的 {role!r} 应写成 '节点ID:字段名' 或对象。"
            )
        node_id, field_name = node_id.strip(), field_name.strip()
        if not node_id or not field_name:
            raise ConfigurationError(f"{variable_name} 的 {role!r} 缺少 nodeId 或 fieldName。")
        result[role] = NodeTarget(node_id, field_name)
    return result


def parse_extra_node_info(raw: str, variable_name: str) -> list[dict[str, Any]]:
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{variable_name} 不是有效 JSON：{exc.msg}") from exc
    if not isinstance(payload, list):
        raise ConfigurationError(f"{variable_name} 必须是 JSON 数组。")

    result: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ConfigurationError(f"{variable_name} 第 {index} 项必须是对象。")
        node_id = str(item.get("nodeId", "")).strip()
        field_name = str(item.get("fieldName", "")).strip()
        if not node_id or not field_name or "fieldValue" not in item:
            raise ConfigurationError(
                f"{variable_name} 第 {index} 项必须包含 nodeId、fieldName、fieldValue。"
            )
        result.append(
            {"nodeId": node_id, "fieldName": field_name, "fieldValue": item["fieldValue"]}
        )
    return result


def normalize_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s\-./:]+", "_", text)


def _score_node(node: dict[str, Any], role: str, aliases: dict[str, tuple[str, ...]]) -> int:
    field_name = normalize_token(node.get("fieldName"))
    node_name = normalize_token(node.get("nodeName"))
    description = normalize_token(
        f"{node.get('description', '')} {node.get('descriptionEn', '')}"
    )
    field_type = normalize_token(node.get("fieldType"))
    score = 0
    for raw_alias in aliases[role]:
        alias = normalize_token(raw_alias)
        if field_name == alias:
            score = max(score, 120)
        elif alias and alias in field_name:
            score = max(score, 80)
        if alias and alias in description:
            score = max(score, 65)
        if alias and alias in node_name:
            score = max(score, 35)

    if role in {"image", "audio"} and field_type == role:
        score += 80
    if role in {"script", "voice_a", "voice_b", "prompt", "negative_prompt"}:
        if field_type in {"string", "text"}:
            score += 12
    if role in {"sentence_pause", "punctuation_pause"} and field_type in {
        "float",
        "double",
        "int",
        "integer",
        "number",
    }:
        score += 20
    if role in {"width", "height", "duration_seconds", "frame_count", "fps", "seed"}:
        if field_type in {"float", "double", "int", "integer", "number"}:
            score += 15
    return score


def infer_node_map(
    nodes: list[dict[str, Any]],
    aliases: dict[str, tuple[str, ...]],
    *,
    minimum_score: int = 60,
) -> dict[str, NodeTarget]:
    result: dict[str, NodeTarget] = {}
    used_targets: set[tuple[str, str]] = set()
    for role in aliases:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for node in nodes:
            node_id = str(node.get("nodeId", "")).strip()
            field_name = str(node.get("fieldName", "")).strip()
            if not node_id or not field_name:
                continue
            target_key = (node_id, field_name)
            if target_key in used_targets:
                continue
            candidates.append((_score_node(node, role, aliases), node))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if candidates and candidates[0][0] >= minimum_score:
            best = candidates[0][1]
            target = NodeTarget(str(best["nodeId"]), str(best["fieldName"]))
            result[role] = target
            used_targets.add((target.node_id, target.field_name))
    return result


def make_node_info_list(
    node_map: dict[str, NodeTarget],
    values: dict[str, Any],
    extras: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for role, target in node_map.items():
        if role not in values:
            continue
        value = values[role]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        result.append(
            {
                "nodeId": target.node_id,
                "fieldName": target.field_name,
                "fieldValue": value,
            }
        )
    result.extend(dict(item) for item in extras)
    return result


def summarize_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for node in nodes:
        field_data = node.get("fieldData")
        choices: Any = None
        if isinstance(field_data, str) and field_data.strip():
            try:
                field_data = json.loads(field_data)
            except json.JSONDecodeError:
                pass
        if isinstance(field_data, list):
            choices = field_data[:30]
        elif isinstance(field_data, dict):
            choices = {
                str(key): value[:30] if isinstance(value, list) else value
                for key, value in list(field_data.items())[:20]
            }
        result.append(
            {
                "nodeId": str(node.get("nodeId", "")),
                "nodeName": str(node.get("nodeName", "")),
                "fieldName": str(node.get("fieldName", "")),
                "fieldType": str(node.get("fieldType", "")),
                "description": str(node.get("description", "")),
                "descriptionEn": str(node.get("descriptionEn", "")),
                "defaultValue": node.get("fieldValue"),
                "choices": choices,
            }
        )
    return result


def compose_ltx_prompt(
    *,
    prompt: str,
    left_character: str,
    right_character: str,
    speaker_timeline: str,
    action: str,
    camera: str,
) -> str:
    prompt = ensure_text("video_prompt", prompt, max_length=6000)
    if not prompt:
        raise ValueError("video_prompt 不能为空。")
    parts = [prompt]
    if left_character:
        parts.append(f"首帧左侧人物：{left_character}")
    if right_character:
        parts.append(f"首帧右侧人物：{right_character}")
    if speaker_timeline:
        parts.append(f"说话时间线：{speaker_timeline}")
    if action:
        parts.append(f"人物动作：{action}")
    if camera:
        parts.append(f"镜头：{camera}")
    parts.append("未说话者自然倾听；两人保持首帧左右站位，不交换身份。")
    return "\n".join(parts)


def validate_ltx_numbers(
    *,
    width: int,
    height: int,
    duration_seconds: int,
    frame_count: int,
    fps: int,
    seed: int,
) -> None:
    if width and (width < 256 or width > 4096 or width % 32):
        raise ValueError("width 必须在 256–4096 之间且能被 32 整除；0 表示使用应用默认值。")
    if height and (height < 256 or height > 4096 or height % 32):
        raise ValueError("height 必须在 256–4096 之间且能被 32 整除；0 表示使用应用默认值。")
    if duration_seconds and not 1 <= duration_seconds <= 120:
        raise ValueError("duration_seconds 必须在 1–120 之间；0 表示跟随音频或应用默认值。")
    if frame_count and (frame_count < 9 or frame_count > 2881 or frame_count % 8 != 1):
        raise ValueError("frame_count 必须在 9–2881 之间并满足 frame_count % 8 == 1；0 表示默认。")
    if fps and not 1 <= fps <= 60:
        raise ValueError("fps 必须在 1–60 之间；0 表示使用应用默认值。")
    if seed < -1:
        raise ValueError("seed 必须为 -1 或非负整数。")


def flatten_output_items(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if any(key in value for key in ("fileUrl", "url", "outputUrl")):
            result.append(value)
        for key, child in value.items():
            if key not in {"fileUrl", "url", "outputUrl"}:
                result.extend(flatten_output_items(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(flatten_output_items(child))
    return result


def extract_media_urls(outputs: Any, kind: str) -> list[str]:
    extensions = {
        "audio": {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"},
        "video": {".mp4", ".mov", ".webm", ".mkv", ".m4v"},
        "image": {".jpg", ".jpeg", ".png", ".webp", ".gif"},
    }[kind]
    all_urls: list[str] = []
    matches: list[str] = []
    for item in flatten_output_items(outputs):
        url = str(item.get("fileUrl") or item.get("url") or item.get("outputUrl") or "").strip()
        if not url or url in all_urls:
            continue
        all_urls.append(url)
        declared = normalize_token(
            item.get("fileType") or item.get("outputType") or item.get("mimeType") or ""
        )
        suffix = Path(urlparse(url).path).suffix.lower()
        if kind in declared or suffix in extensions:
            matches.append(url)
    if matches:
        return matches
    return all_urls if len(all_urls) == 1 else []


def extract_failure_reason(outputs: Any) -> str:
    if isinstance(outputs, dict):
        status = normalize_token(outputs.get("status") or outputs.get("taskStatus"))
        if status in {"failed", "failure", "cancel", "cancelled", "canceled"}:
            return str(
                outputs.get("failedReason")
                or outputs.get("errorReason")
                or outputs.get("error")
                or outputs.get("message")
                or outputs.get("msg")
                or "RunningHub 任务失败。"
            )[:1000]
        for value in outputs.values():
            reason = extract_failure_reason(value)
            if reason:
                return reason
    elif isinstance(outputs, list):
        for value in outputs:
            reason = extract_failure_reason(value)
            if reason:
                return reason
    return ""


@dataclass
class PipelineRecord:
    pipeline_id: str
    status: str
    created_at: float
    updated_at: float
    image_filename: str
    tts_task_id: str = ""
    ltx_task_id: str = ""
    audio_url: str = ""
    audio_filename: str = ""
    video_urls: list[str] = field(default_factory=list)
    error: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PipelineRecord":
        return cls(
            pipeline_id=str(payload["pipeline_id"]),
            status=str(payload["status"]),
            created_at=float(payload["created_at"]),
            updated_at=float(payload["updated_at"]),
            image_filename=str(payload["image_filename"]),
            tts_task_id=str(payload.get("tts_task_id") or ""),
            ltx_task_id=str(payload.get("ltx_task_id") or ""),
            audio_url=str(payload.get("audio_url") or ""),
            audio_filename=str(payload.get("audio_filename") or ""),
            video_urls=[str(item) for item in payload.get("video_urls") or []],
            error=str(payload.get("error") or ""),
            inputs=dict(payload.get("inputs") or {}),
        )


class PipelineStore:
    """Small JSON-backed state store intended for one Railway worker."""

    def __init__(self, path: str, *, max_records: int = 200, ttl_seconds: int = 604800) -> None:
        self.path = Path(path)
        self.max_records = max_records
        self.ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()

    def _load_unlocked(self) -> dict[str, PipelineRecord]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, PipelineRecord] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            try:
                record = PipelineRecord.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            result[str(key)] = record
        return result

    def _prune_unlocked(self, records: dict[str, PipelineRecord]) -> dict[str, PipelineRecord]:
        cutoff = time.time() - self.ttl_seconds
        kept = [record for record in records.values() if record.updated_at >= cutoff]
        kept.sort(key=lambda record: record.updated_at, reverse=True)
        return {record.pipeline_id: record for record in kept[: self.max_records]}

    def _write_unlocked(self, records: dict[str, PipelineRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: value.to_dict() for key, value in records.items()}
        fd, temporary = tempfile.mkstemp(prefix="pipelines-", suffix=".json", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    async def put(self, record: PipelineRecord) -> PipelineRecord:
        async with self._lock:
            records = self._load_unlocked()
            record.updated_at = time.time()
            records[record.pipeline_id] = record
            records = self._prune_unlocked(records)
            self._write_unlocked(records)
            return record

    async def get(self, pipeline_id: str) -> PipelineRecord | None:
        async with self._lock:
            return self._load_unlocked().get(pipeline_id)

    async def list_recent(self, limit: int = 20) -> list[PipelineRecord]:
        async with self._lock:
            records = list(self._load_unlocked().values())
        records.sort(key=lambda record: record.updated_at, reverse=True)
        return records[:limit]
