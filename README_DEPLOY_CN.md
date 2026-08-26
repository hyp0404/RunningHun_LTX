# RunningHub LTX 2.3 双人对话 MCP

这是一套面向 ChatGPT + Railway + RunningHub 的 LTX 2.3 双人对话视频服务。

它支持：

- ChatGPT 直接上传一张双人首帧图和一段对白音频；
- 使用公网图片 URL 和音频 URL；
- 自动上传素材到 RunningHub；
- 自动读取 AI 应用可修改节点；
- 生成双人连续对话、对口型视频；
- 查询任务并返回 MP4 链接；
- 阻止同一服务并发提交，减少 RunningHub 804/805 错误；
- Railway 手动节点映射，适配不同作者发布的 LTX 2.3 应用。

## 1. 默认选用的 RunningHub 应用

默认使用中国站应用：

- 名称：`双人对话数字人（LTX 2.3 + 图 + 音频驱动）`
- 页面：<https://www.runninghub.cn/ai-detail/2048763193677324290>
- WebApp ID：`2048763193677324290`
- 输入思路：一张双人首帧图 + 一段完整对白音频 + 双人视频提示词

在 RunningHub 登录后先打开该应用，确认页面可以点击“立即运行”和“API调用”。如果该应用被作者下架、改成私有或你的账户无法访问，请在中国站搜索同类 LTX 2.3 双人对话应用，把新页面 URL 最后一段数字填入 `RUNNINGHUB_WEBAPP_ID`。代码不依赖固定节点编号。

## 2. 生成视频需要输入什么

### 必需输入

1. `image_file`：双人首帧图片
   - 推荐 JPG、PNG 或 WebP；
   - 两个人必须同时出现在同一张图中；
   - 建议正脸或半身，脸部无遮挡；
   - 左右位置明确，避免两人重叠；
   - 竖屏推荐 `768×1280`，横屏推荐 `1280×768`；
   - 宽度和高度最好都能被 32 整除。

2. `audio_file`：完整对白音频
   - 推荐 WAV、MP3、M4A、AAC、FLAC 或 OGG；
   - 两个人的所有台词按实际顺序放在同一个音频文件中；
   - 推荐第一次测试控制在 5–12 秒；
   - 人声清晰，背景音乐尽量小，避免两个人同时讲话；
   - 音频有多长，最终视频通常就接近多长。

3. `prompt`：视频场景与总体表演说明
   - 写明场景、情绪、人物关系、动作和镜头；
   - 不需要把音频内容逐字重复一遍，但可以提供对白文本辅助模型理解。

### 强烈建议输入

- `left_character`：首帧左侧人物身份与服装；
- `right_character`：首帧右侧人物身份与服装；
- `speaker_timeline`：谁在什么时间说话；
- `action`：说话和倾听时的表情、手势；
- `camera`：固定镜头、近景或轻微推镜。

### 可选输入

- `negative_prompt`：不希望出现的内容；
- `width` / `height`：仅在应用暴露尺寸节点时有效，且必须能被 32 整除；
- `aspect_ratio`：仅在应用暴露画幅节点时有效；
- `duration_seconds`：通常建议留 `0`，让视频跟随音频；
- `frame_count`：仅在应用暴露总帧数节点时填写，必须满足 `8n+1`；
- `fps`：仅在应用暴露帧率节点时填写；
- `seed`：`-1` 表示使用应用默认/随机种子。

### 帧数参考

如果应用要求手动设置总帧数，必须满足 `总帧数 % 8 == 1`：

| 目标时长 | 24 fps 附近建议值 |
| --- | ---: |
| 5 秒 | 121 帧 |
| 8 秒 | 193 帧 |
| 10 秒 | 241 帧 |

只有 `inspect_ltx23_app` 确认应用提供 `frame_count` 节点时才填写，否则保持 `0`。

## 3. 输入示例

首帧图：左边是短发女孩，右边是穿深蓝夹克的男孩。

对白音频内容：

```text
0.0–1.6 秒，女孩：你来啦？
1.6–3.8 秒，男孩：嗯，等很久了吗？
3.8–5.5 秒，女孩：没有，我也刚到。
```

工具参数示例：

```text
prompt：傍晚的北京胡同口，两位年轻人见面，暖黄色路灯，电影感写实短视频。
left_character：短发中国女孩，米白色风衣，性格轻快。
right_character：中国男孩，深蓝色休闲夹克，性格温和。
speaker_timeline：0.0–1.6秒左侧女孩说话；1.6–3.8秒右侧男孩回答；3.8–5.5秒左侧女孩回应。
action：女孩先抬头微笑；男孩点头回答；女孩最后轻轻笑一下。未说话者自然倾听。
camera：固定中近景，轻微自然呼吸感，不切镜。
duration_seconds：0
frame_count：0
seed：-1
```

## 4. 部署前准备

准备以下内容：

1. RunningHub 中国站账号；
2. RunningHub API Key；
3. Railway 账号；
4. 一个 GitHub 仓库，或可以上传代码到 Railway 的方式；
5. ChatGPT Business/Enterprise/Edu 工作区中的自定义 MCP 应用权限。

不要把 `RUNNINGHUB_API_KEY` 写入 GitHub 文件。它只能放在 Railway Variables 中。

## 5. 方式 A：替换现有 Railway 服务

如果你准备直接替换当前的 Wan2.2 MCP：

1. 下载并解压本项目；
2. 把下面三个文件放到现有 GitHub 仓库根目录并覆盖旧文件：
   - `server.py`
   - `Dockerfile`
   - `requirements.txt`
3. 同时可以上传 `.dockerignore`；
4. 提交并推送到 GitHub；
5. Railway 检测到新提交后会自动重新部署；
6. 如果没有自动部署，在 Railway 的 Deployments 页面点击 `Redeploy`。

## 6. 方式 B：创建新的 Railway 服务

1. 新建一个 GitHub 仓库，例如 `runninghub-ltx23-mcp`；
2. 将本项目中的文件上传到仓库根目录；
3. 打开 Railway；
4. 点击 `New Project`；
5. 选择 `Deploy from GitHub repo`；
6. 选择刚创建的仓库；
7. Railway 会识别 `Dockerfile` 并开始构建；
8. 进入新 Service 的 `Variables` 页面，添加下一节中的变量；
9. 进入 `Settings → Networking`，点击 `Generate Domain` 创建公网域名。

## 7. Railway Variables 完整配置

### 必填变量

```text
RUNNINGHUB_API_KEY=你的RunningHub_API_Key
RUNNINGHUB_WEBAPP_ID=2048763193677324290
RUNNINGHUB_BASE_URL=https://www.runninghub.cn
RUNNINGHUB_AUTO_DISCOVER_NODES=true
MCP_PATH=/mcp/替换成至少32位的随机字符串
```

### 推荐变量

```text
RUNNINGHUB_HTTP_TIMEOUT_SECONDS=120
RUNNINGHUB_POLL_INTERVAL_SECONDS=5
RUNNINGHUB_UPLOAD_PATH=/openapi/v2/media/upload/binary
MAX_REMOTE_FILE_BYTES=209715200
LOG_LEVEL=INFO
```

Railway 会自动提供 `PORT`，不要手工添加 `PORT`。

`MCP_PATH` 中的随机字符串相当于一段私密连接路径。建议使用密码生成器创建至少 32 位随机字符，不要使用姓名、手机号或上面的示例文字，也不要公开截图。代码不会在 `/health` 或首页返回这段路径。

### 需要删除的旧变量

如果你是从 Wan2.2 服务升级，先删除这些旧映射，避免旧节点污染 LTX 应用：

```text
RUNNINGHUB_PROMPT_NODE_ID
RUNNINGHUB_PROMPT_FIELD_NAME
RUNNINGHUB_NODE_MAP_JSON
RUNNINGHUB_EXTRA_NODE_INFO_JSON
```

第一次部署时让代码自动识别节点。只有检查结果不正确时才重新添加 `RUNNINGHUB_NODE_MAP_JSON`。

### 应用加密时的变量

如果 `inspect_ltx23_app` 返回：

```text
access_encrypted: true
```

还需要添加：

```text
RUNNINGHUB_ACCESS_PASSWORD=该AI应用的访问密码
```

## 8. 检查 Railway 是否部署成功

假设 Railway 域名为：

```text
https://你的项目.up.railway.app
```

浏览器打开：

```text
https://你的项目.up.railway.app/health
```

正常结果类似：

```json
{
  "status": "ok",
  "service": "runninghub-ltx23-dialogue-mcp",
  "mcp_path_configured": true,
  "runninghub_api_key_configured": true,
  "runninghub_webapp_id": "2048763193677324290",
  "configuration_error": null
}
```

如果 Railway 日志显示 `Application failed to respond`：

- 确认 Railway 使用 Dockerfile 构建；
- 确认没有手工写死端口；
- 确认 `CMD ["python", "server.py"]` 没有被 Start Command 覆盖；
- 查看部署日志最早出现的 Python 错误。

## 9. 将新 MCP 连接到 ChatGPT

ChatGPT 当前的官方连接流程是：开启 Developer mode，在 `Settings/Workspace Settings → Apps → Create` 中填写远程 MCP endpoint，选择认证方式，然后点击 `Scan Tools`，扫描成功后再创建应用：

<https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta>

操作步骤：

1. 打开 ChatGPT 网页版；
2. 进入 `Settings → Apps → Advanced Settings`，确认 Developer mode 已开启；
3. 进入 `Settings → Apps → Create`；
4. 应用名称填写 `RunningHub LTX 2.3`；
5. MCP Server URL 填写：

   ```text
   https://你的项目.up.railway.app/mcp/与你在Railway的MCP_PATH中相同的随机字符串
   ```

6. Authentication 选择 `No Auth`；
7. 勾选自定义 MCP 风险确认；
8. 点击 `Scan Tools`；
9. 扫描成功后应看到：
   - `inspect_ltx23_app`
   - `generate_ltx23_dialogue`
   - `generate_ltx23_dialogue_from_chatgpt_attachments`
   - `generate_ltx23_dialogue_from_urls`
   - `upload_ltx23_media_from_chatgpt`
   - `query_ltx23_task`
10. 点击 `Create`。

如果你直接替换了旧 Railway 服务，但工具定义从 Wan2.2 变成了 LTX2.3，ChatGPT 不会自动采用全部新工具。应在应用管理中执行 Refresh/重新扫描；如果仍显示旧工具，删除旧的开发应用并重新创建。官方说明指出，已批准应用的工具定义可能是冻结快照，服务端更新后需要管理员刷新或重新发布。

## 10. 第一次必须先检查节点

连接成功后，在 ChatGPT 中发送：

```text
调用 inspect_ltx23_app，检查当前 LTX 2.3 应用配置，不要生成视频。
```

理想结果：

```text
ok: true
webapp_id: 2048763193677324290
missing_required_roles: []
resolved_node_map:
  image: ...
  audio: ...
  prompt: ...
```

如果 `missing_required_roles` 不是空数组，从 `available_nodes` 找到：

- 上传双人首帧图对应的 `nodeId` 和 `fieldName`；
- 上传驱动音频对应的 `nodeId` 和 `fieldName`；
- 视频提示词对应的 `nodeId` 和 `fieldName`。

然后在 Railway 添加一行 JSON。例如检查结果显示：

```text
图片节点：nodeId=12, fieldName=image
音频节点：nodeId=34, fieldName=audio
提示词节点：nodeId=56, fieldName=text
```

则填写：

```text
RUNNINGHUB_NODE_MAP_JSON={"image":"12:image","audio":"34:audio","prompt":"56:text"}
```

注意：上面的 `12/34/56` 只是格式示例，不能直接照抄，必须使用你实际检查到的节点。

如果还需要映射尺寸或帧数，可以扩展：

```text
RUNNINGHUB_NODE_MAP_JSON={"image":"12:image","audio":"34:audio","prompt":"56:text","width":"78:width","height":"78:height","frame_count":"90:value"}
```

保存 Railway Variables 后等待重新部署，再次调用 `inspect_ltx23_app`。

## 11. 第一次测试生成

在 ChatGPT 对话框同时附加：

- `two_people.png`
- `dialogue.wav`

然后发送：

```text
使用 RunningHub LTX 2.3 生成双人对话视频。
左边是短发女孩，右边是穿深蓝夹克的男孩。
场景是傍晚的北京胡同口，暖黄色路灯，写实电影感。
0–1.6秒左侧女孩说话，1.6–3.8秒右侧男孩说话，3.8秒以后左侧女孩回应。
未说话的人自然倾听，不要同时开口，不要交换位置，不要字幕。
固定中近景，先按应用默认分辨率生成。
```

ChatGPT 应调用：

```text
generate_ltx23_dialogue_from_chatgpt_attachments
```

任务提交后返回 `task_id`，继续调用：

```text
query_ltx23_task(task_id=刚才的任务ID, wait_seconds=45)
```

直到返回：

```text
status: SUCCESS
video_urls: [...]
```

## 12. 工具说明

### `inspect_ltx23_app`

只读检查配置和节点，不消耗生成额度。部署后第一个调用必须是它。

### `generate_ltx23_dialogue_from_chatgpt_attachments`

推荐工具。直接读取 ChatGPT 附件，上传图片和音频，并提交视频任务。

### `generate_ltx23_dialogue_from_urls`

当素材已有公网 HTTPS 地址时使用。服务会先下载，再上传到 RunningHub。

### `upload_ltx23_media_from_chatgpt`

只上传一个文件，不生成视频。适合先验证附件上传，或重复使用同一素材。

### `generate_ltx23_dialogue`

使用已经上传到 RunningHub 的 `image_filename` 和 `audio_filename` 生成视频。

### `query_ltx23_task`

查询任务结果。`wait_seconds` 推荐填写 30–45。

## 13. 常见错误

### 1. `missing_required_roles`

原因：应用节点命名特殊，自动识别失败。

处理：根据 `available_nodes` 配置 `RUNNINGHUB_NODE_MAP_JSON`。

### 2. `RunningHub API 错误 804`

通常表示任务还在运行。继续查询，不要重复提交。

### 3. `RunningHub API 错误 805`

可能是任务中断、取消、内部状态异常或短时间重复并发提交。等待已有任务结束，再重新提交一次。

### 4. 上传接口返回 404

确认：

```text
RUNNINGHUB_UPLOAD_PATH=/openapi/v2/media/upload/binary
RUNNINGHUB_BASE_URL=https://www.runninghub.cn
```

如果 RunningHub 后续升级上传路径，以其“文件上传”API 文档为准，然后只修改 `RUNNINGHUB_UPLOAD_PATH`，不需要改代码。

### 5. ChatGPT 附件工具没有出现

- 确认扫描工具时显示了 `generate_ltx23_dialogue_from_chatgpt_attachments`；
- Refresh/重新扫描自定义应用；
- 如果应用已发布并冻结了旧工具定义，重新创建开发应用；
- 确认 `requirements.txt` 安装的是 FastMCP 2.x。

### 6. 人物说反、交换位置

- 在图片中明确左右站位；
- `left_character` 与 `right_character` 描述不要含糊；
- `speaker_timeline` 写出每段时间；
- 音频不要让两人重叠说话；
- 先用 5–8 秒短音频测试。

### 7. 输出带字幕

提示词和 negative prompt 都写明“无字幕、无文字”；如果应用本身固定生成字幕，需要复制/修改工作流或换一个无字幕版 LTX 应用，MCP 无法覆盖未暴露的内部固定节点。

## 14. 安全说明

- 不要在 GitHub、截图或聊天中公开 RunningHub API Key；
- 不要公开 Railway 中的完整 `MCP_PATH` 或包含该路径的 ChatGPT 连接 URL；
- 不要把 `.env` 提交到仓库；
- 只连接你自己部署并检查过的 MCP 服务；
- 生成工具会消耗 RH 币，提交前确认输入无误；
- 默认最大附件为 200 MiB，可通过 `MAX_REMOTE_FILE_BYTES` 调整；
- URL 下载会拒绝 localhost 和私有网络地址，防止服务被用于访问 Railway 内网。
