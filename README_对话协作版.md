# 当前 ChatGPT 对话负责分镜、图片与画面检查

本版删除 film_engine.py 中所有服务器端 OpenAI API 调用。Railway 不再需要 OPENAI_API_KEY、FILM_TEXT_MODEL、FILM_IMAGE_MODEL、FILM_IMAGE_QUALITY。分镜、图片由当前 ChatGPT 对话生成或用户提供，画面由当前对话实际查看后给出检查结果。对话中图片生成仍受你当前套餐的功能和额度限制；这不是将 ChatGPT 订阅额度转成服务器 API 额度。

服务器只调用 RunningHub 生成 Qwen 配音与 LTX 视频，并用 FFmpeg 处理音轨、抽帧和剪辑。RunningHub 和 Railway 自身的费用照常。

## 文件与部署

这是现有仓库的增量更新包。替换 server.py、film_engine.py；保留原 orchestrator_core.py、requirements.txt 及其他原项目依赖。Dockerfile.full、requirements-full.txt、railway.json 安装 FFmpeg 并沿用原依赖。不要把 requirements-full.txt 改名覆盖 requirements.txt。

两套 UI 工作流、API JSON 与上一版相同：

| RunningHub 导入文件 | GitHub API 文件 | 用途 |
|---|---|---|
| LTX_film_i2v_workflow.json | film_i2v_api.json | 普通动作和对白 |
| LTX_film_flf_workflow.json | film_flf_api.json | 首尾图状态变形 |

如果已发布这两套完整制片工作流且未改节点，不需要重新发布；未发布则分别导入运行后发布，填写两个新工作流 ID。只发布过更早的单独关键帧测试工作流时，不要把它当作普通 I2V 工作流。

保留原 RunningHub/Qwen/旧 LTX 配置；新流程只需：

```env
FILM_I2V_WORKFLOW_ID=普通视频工作流ID
FILM_FLF_WORKFLOW_ID=首尾帧工作流ID
FILM_PUBLIC_BASE_URL=https://你的Railway域名
FILM_DOWNLOAD_SECRET=独立的至少32位随机字符串
FILM_DATA_DIR=/data/films
```

有工作流访问密码时另设 FILM_I2V_ACCESS_PASSWORD / FILM_FLF_ACCESS_PASSWORD。公开地址不带 /mcp。挂载持久化 Volume 到 /data；使用一份服务副本。原单段任务状态文件继续使用原设置。

可以删除上版为此功能新增的 OpenAI 变量；本版不会读取它们。更新后刷新 ChatGPT 连接器，确保 start_full_film 参数已从 script 改成 plan_json。

## 当前对话中的执行顺序

1. 调用 inspect_full_film 检查服务器配置，再调用 get_film_plan_schema 获取分镜格式。
2. ChatGPT 根据你的文字要求编写分镜 JSON，包括人物形象、声线、场景、动作、对白和逐镜头时长。
3. 调用 start_full_film(plan_json=JSON字符串, duration_seconds=90)，服务器验证总时长并创建 WAITING_ASSETS 任务。
4. ChatGPT 在当前对话生成人物/场景参考图，复用同一人物形象生成每镜头首图；变形镜头再编辑出尾图。同一机位的连续镜头用上一段实际尾帧，不另生成首图。
5. 调用 upload_film_frame(job_id, shot_index, role, frame_file) 上传；shot_index 从0开始，role 为 start 或 end。上传不生成新图片。批量准备的多个镜头图片可以在等待素材时逐张上传。
6. 调用 query_full_film 启动/查询后台配音和视频。首图、尾图缺失时会停在 WAITING_ASSETS。
7. 视频出片后停在 WAITING_REVIEW，返回 clip.mp4 和首、1/4、中间、3/4、尾帧，以及输入首尾图的带签名链接。
8. ChatGPT 必须下载并实际查看返回素材，结合当前对话中的人物/场景参考图检查，再调用 submit_film_review。无需每段都让用户重复确认，检查可以由当前对话中的助手执行；但不能只看任务成功状态就提交通过。
9. 所有镜头处理完，自动原速合成，返回 final.mp4、storyboard.json、review_report.json。无字幕，不慢放补时。

**服务器不能在没有当前对话参与的情况下自行完成分镜、生成参考图和画面检查。** 当前对话中工具不可用、额度不足或中途停止时，任务会保存在相应等待阶段，之后继续处理。

## 分镜范围

总时长5–120秒、24fps、16:9、每镜头1–5秒，最多24镜头/4名主要人物/6场景。每镜头最多2名主要人物、1人发言；对话轮次拆镜头。每秒最多3个对白字符是保守输入限制，不能代替实际音频长度检查。

模式 i2v 用于普通动作和对白；flf 用于有明确目标尾图的状态变化。transition=cut 用于换景/换机位；continue 必须与前镜头同场景同人物，并复用前段实际尾帧。第一镜头必须 cut。

Qwen 配音使用固定人物声线描述；长对白会 BLOCKED_AUDIO，当前对话可以调用 revise_blocked_dialogue 缩短该句。短音频补静音，成片保留原配音，不自动加速或截断句子。该机制不保证口型准确。

## 检查结果格式

get_film_plan_schema 同时返回 review_schema。示例：

```json
{
  "identity_ok": true,
  "costume_ok": true,
  "action_ok": false,
  "background_ok": true,
  "no_text": true,
  "boundary_ok": true,
  "issues": ["人物只是躺下，没有变成扁平状态"],
  "retry_instruction": "Keep the same person lying still; show actual continuous loss of body thickness."
}
```

把结果序列化为 review_json，action 为以下之一：
- advance：检查项全部通过，继续下一段。
- retry：按修正提示重做当前镜头，消耗 RH；默认最多1次重试，可在创建时设0–2次。
- draft：明确保留未通过镜头，继续制作带问题的草稿。最终不会标为检查通过。

首尾图本身有偏差时，可在 WAITING_REVIEW 上传修改后的首/尾图，再提交 retry。已经完成的镜头不允许被后续图片上传悄悄覆盖。同机位 continue 的首图由实际上一段尾帧提供，不允许单独上传替换。

## 状态与结果

- WAITING_ASSETS：当前对话需要提供图片。
- RUNNING：后台正在处理配音、视频或剪辑。
- WAITING_REVIEW：当前对话需要实际检查视频并提交结果。
- COMPLETED_CHAT_REVIEW：对话提交的检查均通过；仍不代表逐帧动态或口型已获严格认证。
- DRAFT_REVIEW_REQUIRED：存在明确保留的未通过镜头。
- BLOCKED_AUDIO：对白过长。
- NEEDS_ATTENTION：配置/提供商/媒体错误。修复后用 resume_full_film；外部请求结果不明时先核对 RH 历史，避免重复扣费。

视频、抽帧和报告的下载链接默认24小时有效，重新查询可刷新。链接仅开放约定的媒体文件，不开放整个任务目录。

背景音乐可选：将有权使用的音频放到 /data/films/music/，启动时填 music_name。默认对白或静音，不在服务器生成音乐。

## 验证范围

本版测试覆盖：没有 OpenAI API 调用代码、UI/API一致性、上传图片→普通视频→等待检查→失败重试→实际尾帧续接→首尾帧视频→明确草稿→6秒合成。配音和服务仍依赖原部署；本次未调用真实 RH 生成，也未验证任意动作、人物一致性或口型。

测试：python -m unittest -v test_chat_film.py
