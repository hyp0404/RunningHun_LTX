# RunningHub Qwen3-TTS → LTX 2.3 双应用编排版 MCP

版本：2.0（2026-08-26）

这套服务把两个 RunningHub AI 应用串联为一条可恢复的异步流水线：

```text
双人首帧图 + 台词 + 两种声线 + 视频提示词
                    ↓
Qwen3-TTS 多人对话生成完整音频
                    ↓
自动下载并重新上传音频
                    ↓
LTX 2.3 双人对话视频
                    ↓
MP4 链接
```

默认应用：

- Qwen3-TTS：<https://www.runninghub.cn/ai-detail/2051851200194195458>
- LTX 2.3：<https://www.runninghub.ai/zh-cn/ai-detail/2048763193677324290>

两个阶段都可能消耗 RH 币。第一次请用 5～12 秒的短台词测试。

## 1. 与旧 LTX-only 版本的区别

旧版必须先手工准备完整音频。新版支持：

- ChatGPT 上传一张双人图片；
- 直接输入双人台词；
- 自动调用 Qwen3-TTS；
- 自动取得生成的 WAV/MP3 等音频；
- 自动把音频转交给 LTX 2.3；
- 自动查询并返回 MP4；
- 分别检查和映射两个 AI 应用的节点；
- 把流水线状态保存到 JSON 文件；
- 可挂载 Railway Volume，部署重启后仍能找回 `pipeline_id`；
- 仍支持使用已经准备好的图片和对白音频，直接跳过 Qwen 阶段。

服务不会在一次 MCP 调用中等待十几分钟。启动工具只提交第一阶段，随后通过 `query_dialogue_pipeline` 推进任务，因此不会因为 ChatGPT 单次工具调用超时而丢失整个流程。

## 2. 项目文件

```text
server.py                 MCP、RunningHub API 和流水线编排
orchestrator_core.py      节点识别、输出解析和状态存储
requirements.txt          Python 依赖
Dockerfile                Railway 容器部署
.env.example              环境变量模板
.dockerignore
.gitignore
README_DEPLOY_CN.md        本文档
tests/test_core.py         核心单元测试
tests/test_pipeline.py     状态转换测试
```

上传到 GitHub 时不要上传真实 `.env` 文件。

## 3. 工作流程

### 全自动模式

1. `start_dialogue_video_from_script` 上传双人图片；
2. 服务提交 Qwen3-TTS 任务，返回 `pipeline_id`；
3. 调用 `query_dialogue_pipeline`；
4. 如果 TTS 尚未完成，状态为 `TTS_RUNNING`；
5. TTS 完成后，服务自动下载并上传音频；
6. 服务自动提交 LTX 任务；
7. 继续查询；
8. 成功时返回 `status: SUCCESS` 和 `video_urls`。

### 先检查音频再生成视频

启动时设置：

```text
auto_start_ltx=false
```

TTS 完成后，流水线暂停为：

```text
WAITING_FOR_LTX
```

返回结果中会有 `audio_url`。检查音频并记录实际时间线后调用：

```text
continue_dialogue_pipeline_to_ltx
```

此时可以补充准确的 `speaker_timeline`，再启动 LTX。对于复杂的多轮对白，这种模式更稳。

## 4. 部署前准备

准备：

1. RunningHub 中国站账号；
2. RunningHub API Key；
3. RunningHub 余额；
4. GitHub 账号和一个仓库；
5. Railway 账号；
6. ChatGPT 自定义 MCP 应用权限；
7. 一张清晰的双人首帧图片。

不要把 RunningHub API Key、应用访问密码或完整 MCP URL 发到聊天、GitHub 或公开截图中。

## 5. 上传代码到 GitHub

### 新建服务

1. 解压本部署包；
2. 新建 GitHub 仓库，例如 `runninghub-qwen-ltx-orchestrator`；
3. 将本目录中的文件放到仓库根目录；
4. 提交并推送。

### 覆盖旧 LTX 服务

如果要直接升级原 Railway 服务，把下面文件放到旧仓库根目录：

```text
server.py
orchestrator_core.py
requirements.txt
Dockerfile
.dockerignore
.gitignore
README_DEPLOY_CN.md
```

旧版没有 `orchestrator_core.py`，升级时一定要新增它，否则 Railway 会报：

```text
ModuleNotFoundError: No module named 'orchestrator_core'
```

## 6. 部署到 Railway

1. Railway 点击 `New Project`；
2. 选择从 GitHub 仓库部署；
3. 选择代码仓库；
4. Railway 根据 `Dockerfile` 构建；
5. 打开 Service 的 `Variables`；
6. 添加下一节变量；
7. 在 Public Networking 中生成 Domain；
8. 等待部署完成。

不要手工填写 `PORT`，Railway 会自动提供。

## 7. Railway Variables

### 必填

```text
RUNNINGHUB_API_KEY=你的RunningHub_API_Key
RUNNINGHUB_BASE_URL=https://www.runninghub.cn

QWEN_TTS_WEBAPP_ID=2051851200194195458
LTX23_WEBAPP_ID=2048763193677324290

RUNNINGHUB_AUTO_DISCOVER_NODES=true
RUNNINGHUB_UPLOAD_PATH=/openapi/v2/media/upload/binary

MCP_PATH=/mcp/至少32位随机字符串
```

生成随机串：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 推荐

```text
RUNNINGHUB_HTTP_TIMEOUT_SECONDS=120
RUNNINGHUB_POLL_INTERVAL_SECONDS=5
MAX_REMOTE_FILE_BYTES=209715200
PIPELINE_STATE_MAX_RECORDS=200
PIPELINE_STATE_TTL_SECONDS=604800
LOG_LEVEL=INFO
```

### 节点映射

第一次先留空：

```text
QWEN_TTS_NODE_MAP_JSON=
LTX23_NODE_MAP_JSON=
QWEN_TTS_EXTRA_NODE_INFO_JSON=
LTX23_EXTRA_NODE_INFO_JSON=
```

部署后调用 `inspect_dialogue_pipeline`。只有自动识别失败或识别错误时才填写。

### 加密应用

检查结果如果显示 `access_encrypted: true`，填写对应密码：

```text
QWEN_TTS_ACCESS_PASSWORD=Qwen应用密码
LTX23_ACCESS_PASSWORD=LTX应用密码
```

未加密的应用保持空白。

### 从旧版升级时删除或替换

旧变量：

```text
RUNNINGHUB_WEBAPP_ID
RUNNINGHUB_NODE_MAP_JSON
RUNNINGHUB_EXTRA_NODE_INFO_JSON
RUNNINGHUB_ACCESS_PASSWORD
```

新版分别使用：

```text
QWEN_TTS_WEBAPP_ID
QWEN_TTS_NODE_MAP_JSON
QWEN_TTS_EXTRA_NODE_INFO_JSON
QWEN_TTS_ACCESS_PASSWORD

LTX23_WEBAPP_ID
LTX23_NODE_MAP_JSON
LTX23_EXTRA_NODE_INFO_JSON
LTX23_ACCESS_PASSWORD
```

代码仍会把旧 `RUNNINGHUB_WEBAPP_ID` 当作 LTX ID 的后备值，但建议完成迁移，避免以后混淆。

## 8. 建议添加 Railway Volume

不使用 Volume 时，默认状态文件是：

```text
/tmp/runninghub-dialogue-pipelines.json
```

它能应对正常的多次 MCP 调用，但 Railway 重新部署后可能消失。为了让长任务在重启后可恢复：

1. 在 Railway Project Canvas 新建 Volume；
2. 连接到本服务；
3. Mount Path 填：

   ```text
   /data
   ```

4. 添加变量：

   ```text
   PIPELINE_STATE_FILE=/data/runninghub-dialogue-pipelines.json
   ```

这里只存储任务 ID、输入参数和输出 URL，不存储 RunningHub API Key。Volume 并不能阻止 RunningHub 链接过期，生成完成后仍建议及时下载音频和视频。

## 9. 健康检查

假设域名为：

```text
https://your-service.up.railway.app
```

浏览器打开：

```text
https://your-service.up.railway.app/health
```

正常示例：

```json
{
  "status": "ok",
  "service": "runninghub-qwen-ltx-orchestrator",
  "mcp_path_configured": true,
  "runninghub_api_key_configured": true,
  "qwen_tts_webapp_id": "2051851200194195458",
  "ltx23_webapp_id": "2048763193677324290",
  "configuration_error": null
}
```

`/health` 成功只代表服务启动，不代表两个 RunningHub 应用节点已经正确映射。

## 10. 重新连接 ChatGPT MCP

这次工具名称和输入 Schema 已经变化，不能只依赖旧工具缓存。

推荐：

1. 在 ChatGPT 的自定义应用管理中刷新/重新扫描旧应用；
2. 如果仍显示旧工具，删除旧开发应用并重新创建；
3. MCP URL：

   ```text
   https://your-service.up.railway.app/mcp/你的真实随机串
   ```

4. 扫描后应看到：
   - `inspect_dialogue_pipeline`
   - `start_dialogue_video_from_script`
   - `start_dialogue_video_from_script_url`
   - `start_ltx23_from_ready_media`
   - `continue_dialogue_pipeline_to_ltx`
   - `query_dialogue_pipeline`
   - `list_recent_dialogue_pipelines`

如果缺少 `start_dialogue_video_from_script` 的图片附件参数，确认 Railway 部署的是本包的 `server.py`，并重新扫描应用。

## 11. 第一次必须检查两个应用

在 ChatGPT 发送：

```text
调用 inspect_dialogue_pipeline，只检查 Qwen3-TTS 和 LTX 2.3 的节点，不生成内容。
```

成功条件：

```text
ok: true
qwen_tts.missing_required_roles: []
ltx23.missing_required_roles: []
```

Qwen 必需角色只有：

```text
script
```

Qwen 可选角色：

```text
voice_a
voice_b
sentence_pause
punctuation_pause
seed
```

LTX 必需角色：

```text
image
audio
prompt
```

LTX 可选角色：

```text
negative_prompt
width
height
aspect_ratio
duration_seconds
frame_count
fps
seed
```

## 12. 手动配置节点映射

从检查结果的 `available_nodes` 中复制真实 `nodeId` 和 `fieldName`。

Qwen 格式示例：

```text
QWEN_TTS_NODE_MAP_JSON={"script":"10:text","voice_a":"11:instruct","voice_b":"12:instruct","sentence_pause":"13:value"}
```

LTX 格式示例：

```text
LTX23_NODE_MAP_JSON={"image":"21:image","audio":"22:audio","prompt":"23:text"}
```

这些 `10/11/12/13/21/22/23` 全部是格式示例，不能直接照抄。

注意：

- 如果 Qwen 应用只有一个剧本节点，至少映射 `script`；
- 声线节点找不到时，Qwen 会使用应用默认声线；
- 如果传入了声线描述但没有映射，启动结果的 `warnings` 会提示；
- 应用作者更新工作流后，应重新检查节点；
- 手动映射优先于自动识别。

### 固定节点

某些应用要求一个未被自动识别的固定选项，可以用额外节点：

```text
QWEN_TTS_EXTRA_NODE_INFO_JSON=[{"nodeId":"30","fieldName":"language","fieldValue":"Chinese"}]
```

```text
LTX23_EXTRA_NODE_INFO_JSON=[{"nodeId":"40","fieldName":"mode","fieldValue":"default"}]
```

必须从实际 API 调用示例复制值，不要猜枚举。

## 13. 第一次完整生成

在 ChatGPT 上传一张 `two_people.png`，然后发送：

```text
使用双应用编排版生成一段双人对话视频。

台词：
女孩：你来啦？
男孩：嗯，等很久了吗？
女孩：没有，我也刚到。走吧，电影快开始了。
男孩：好，这次爆米花我来买。

女孩声线：22岁左右，清亮自然，带一点笑意，普通话，语速中等。
男孩声线：25岁左右，温和略低沉，普通话，语速中等。

首帧左侧人物：短发中国女孩，米白色风衣。
首帧右侧人物：中国男孩，深蓝色休闲夹克。

视频场景：傍晚北京胡同口，暖黄色路灯，灰砖墙，电影感写实风格。
动作：说话者自然张嘴、轻微点头；未说话者看向对方并自然倾听。
镜头：固定中近景，不切镜。
不要字幕、文字、水印、人物换位或同时开口。

先使用应用默认停顿、尺寸、时长和随机种子。
auto_start_ltx=true。
```

ChatGPT 应调用：

```text
start_dialogue_video_from_script
```

返回示例：

```text
pipeline_id: abcdef...
status: TTS_SUBMITTED
tts_task_id: 123456...
```

接着发送：

```text
继续查询刚才的对话视频流水线，等待最多45秒。
```

ChatGPT 应调用：

```text
query_dialogue_pipeline(pipeline_id="abcdef...", wait_seconds=45)
```

可能依次看到：

```text
TTS_RUNNING
LTX_SUBMITTED
LTX_RUNNING
SUCCESS
```

成功结果包含：

```text
audio_url: Qwen生成的完整对白音频
video_urls: [LTX生成的MP4]
```

## 14. 每次生成的输入

### 必填

| 参数 | 内容 |
| --- | --- |
| `image_file` | ChatGPT 中上传的双人首帧图 |
| `dialogue_script` | 包含角色前缀的完整台词 |
| `video_prompt` | 场景、风格、人物关系和镜头说明 |

### 推荐

| 参数 | 内容 |
| --- | --- |
| `voice_a_prompt` | 第一种角色声音风格 |
| `voice_b_prompt` | 第二种角色声音风格 |
| `left_character` | 图片左侧人物身份和服装 |
| `right_character` | 图片右侧人物身份和服装 |
| `action` | 说话和倾听动作 |
| `camera` | 景别和镜头运动 |
| `negative_prompt` | 字幕、换位、口型错位等排除项 |

### 可选

| 参数 | 默认值 | 说明 |
| --- | ---: | --- |
| `sentence_pause` | `0` | 使用 Qwen 应用默认值；只有节点已映射时才发送 |
| `punctuation_pause` | `0` | 使用应用默认值 |
| `speaker_timeline` | 空 | 可写准确时间或大致轮次 |
| `auto_start_ltx` | `true` | `false` 表示先检查音频 |
| `width` / `height` | `0` | 使用应用默认；手填时必须能被 32 整除 |
| `duration_seconds` | `0` | 跟随音频或应用默认 |
| `frame_count` | `0` | 使用应用默认；手填时需满足 `8n+1` |
| `fps` | `0` | 使用应用默认 |
| `tts_seed` / `ltx_seed` | `-1` | 使用应用默认/随机种子 |

## 15. 台词格式

通常使用：

```text
女孩：第一句。
男孩：第二句。
女孩：第三句。
男孩：第四句。
```

但是具体 Qwen 应用可能规定自己的角色标记。先在 RunningHub 页面手动运行一次，并以该应用输入框自带示例为准。

建议：

- 角色名始终一致；
- 每行只有一个说话者；
- 不安排重叠说话；
- 首次控制在 5～12 秒；
- 多于 12～15 秒时拆成多个镜头；
- Qwen 中的角色顺序与视频提示词左右人物保持一致。

## 16. 恢复任务

如果忘记 `pipeline_id`，调用：

```text
list_recent_dialogue_pipelines
```

找到最近任务后继续查询。

如果 Railway 重启后返回 `NOT_FOUND`：

- 检查是否挂载 Volume；
- 检查 `PIPELINE_STATE_FILE` 是否指向 Volume；
- 在 RunningHub 的 API 调用记录中使用 `tts_task_id` 或 `ltx_task_id` 检查原任务；
- 没有 Volume 且状态文件丢失时，服务无法自动恢复原输入参数，但 RunningHub 中已经提交的任务不会因为 MCP 状态丢失而自动取消。

## 17. 常见错误

### `missing_required_roles`

原因：自动识别不了应用节点。

处理：从 `available_nodes` 建立对应的 Qwen 或 LTX 手动映射。

### Qwen 声线描述没有生效

检查 `resolved_node_map` 是否包含 `voice_a` 和 `voice_b`。没有时配置 `QWEN_TTS_NODE_MAP_JSON`，或使用应用默认声线。

### 任务一直 `TTS_RUNNING` 或 `LTX_RUNNING`

不要重复启动新流水线。继续查询当前 `pipeline_id`，同时在 RunningHub API 调用记录中查看相应 task ID。

### RunningHub 804

通常表示任务还在运行。代码会把它转换为 `TTS_RUNNING` 或 `LTX_RUNNING`。

### RunningHub 805

代码会把它视为任务中断、取消或内部异常，并将流水线标记为 `FAILED`。

### 音频生成成功但 LTX 没启动

检查：

- 是否设置了 `auto_start_ltx=false`；
- 状态是否为 `WAITING_FOR_LTX`；
- LTX 必需节点是否完整；
- 音频输出 URL 是否仍可下载；
- RunningHub 上传是否返回 `filename`。

### 视频人物说反或乱动嘴

- 图片左右站位明确；
- `left_character`、`right_character` 不要含糊；
- 先使用 `auto_start_ltx=false`；
- 听完音频后填写准确 `speaker_timeline`；
- 避免同时说话；
- 先用固定中近景和小动作测试。

### Railway 日志出现 Application failed to respond

- 确认 `Dockerfile` 在仓库根目录；
- 确认 `orchestrator_core.py` 已上传；
- 不要手工设置 `PORT`；
- 不要覆盖 Dockerfile 的 Start Command；
- 查看部署日志最早出现的 Python 错误。

## 18. 本地检查

安装依赖：

```bash
python -m pip install -r requirements.txt
```

运行测试：

```bash
python -m unittest discover -s tests -v
```

启动：

```bash
export RUNNINGHUB_API_KEY="你的Key"
export MCP_PATH="/mcp/随机字符串"
python server.py
```

不要在公开终端录屏或日志中显示真实 Key。

## 19. 安全说明

- API Key 只存在 Railway Variables；
- 所有 RunningHub 请求同时使用 Bearer 认证；
- 日志不会主动输出完整 API Key；
- 公网文件下载拒绝 localhost 和私有网络地址；
- 每次重定向都会重新检查目标地址；
- 下载文件有大小上限；
- 流水线 ID 使用随机值，不能顺序猜测；
- 状态文件权限设置为仅服务用户可读写；
- 声音克隆必须获得声音权利人授权；
- `MCP_PATH` 是私密入口，不要公开。

## 20. 参考资料

- RunningHub Qwen3-TTS 应用：<https://www.runninghub.cn/ai-detail/2051851200194195458>
- RunningHub LTX 2.3 应用：<https://www.runninghub.ai/zh-cn/ai-detail/2048763193677324290>
- RunningHub AI 应用任务接口：<https://www.runninghub.cn/runninghub-api-doc-cn/api-425749010>
- RunningHub AI 应用节点示例：<https://www.runninghub.ai/runninghub-api-doc-en/api-425761097>
- RunningHub 新文件上传接口：<https://www.runninghub.ai/runninghub-api-doc-en/api-425761098>
- RunningHub 查询任务输出：<https://www.runninghub.cn/runninghub-api-doc-en/api-425761034>
- Railway 部署：<https://docs.railway.com/quick-start>
- Railway Volumes：<https://docs.railway.com/volumes>
- OpenAI MCP 文档：<https://developers.openai.com/api/docs/mcp>

