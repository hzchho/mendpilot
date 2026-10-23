# CLI

先激活用户自己的 Python 环境。`mendpilot` 默认在该环境修复代码副本，不要求 Docker。

| 参数 | 含义 |
|---|---|
| `--file PATH` | 单文件；同时指定项目时，作为项目内的关注文件 |
| `--project DIR` | 复制整个项目，保留相对导入和测试结构 |
| `--task TEXT` / `--task-file PATH` | 必选其一；描述症状、复现与期望行为 |
| `--test-command CMD` | 在副本根目录运行的用户测试，修复前后执行，并在提交时重放 |
| `--config PATH` | YAML 模型和预算配置；默认使用包内 Qwen 模板 |
| `--model NAME` | 覆盖配置中的服务模型标识 |
| `--base-url URL` | 覆盖接口地址，例如 `http://127.0.0.1:8002/v1` |
| `--api FORMAT` | `chat_completions` 或 `responses` |
| `--image IMAGE` | 可选容器镜像；省略或空字符串使用当前本地环境 |
| `--output DIR` | 输入目录外的新输出目录，拒绝覆盖既有运行 |
| `--max-source-mb N` | 输入副本总大小上限，默认 50 MiB |
| `--dry-run` | 校验与复制输入，不启动模型或容器 |

配置示例：

```yaml
model:
  name: your-model
  base_url: https://your-provider.example/v1
  api: responses
  api_key_env: MENDPILOT_API_KEY
limits:
  input_tokens: 24000
  output_tokens: 8192
  steps: 30
  seconds: 900
  tool_timeout: 60
```

`input_tokens` 是 API 输入保护预算，通用修复接口采用保守计数，不等同于模型准确 token 数；必须大于 8,192。`output_tokens` 是每次请求输出上限。`steps` 至少为 8；`seconds` 是 Agent 阶段的总期限；`tool_timeout` 是单条工具命令期限。均为正整数。通用 CLI 不设置评测专用的 65,536 累计输出限制。

默认从配置的密钥环境变量读取；远端服务未提供时，从终端隐藏输入或标准输入读取。本机服务可使用占位凭据。输入过滤会排除 `.git`、密钥文件、缓存和符号链接；项目内可用 `.mendpilotignore` 追加排除规则，详见 `source.py`。请检查 `manifest.json` 的实际文件清单。

输出包括：`source/` 输入快照、`workspace/` 本地执行副本、`runtime/` 本地工具状态、`fixed/` 修复文件、`submitted.patch` 正式提交、`worktree.diff` 最终工作区差异、`trajectory.json` 完整轨迹、`test-before.json`/`test-after.json` 用户测试、`result.json` 计数和状态。Docker 不生成本地 workspace/runtime。命令返回 0 表示提交且导出成功、用户提供的测试未失败；返回 1 表示修复未完成，2 表示输入错误。成功提交不保证未提供的测试全部正确。

Docker 镜像应预装目标依赖并包含 bash/git/Python，`/testbed` 必须为空。容器禁网，设置 CPU/内存/进程限制，结束时同步清理。项目依赖不同，可修改 `docker/Dockerfile` 后自行构建。
