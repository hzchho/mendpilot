# MendPilot

支持多模型的 Python 代码修复工具：读取代码副本，通过 bash 查看、修改和测试，导出补丁与修复文件。基于 `mini-swe-agent==2.4.6` 的 Agent 循环开发，增加上下文裁剪、预算收尾、工具格式反馈和自动测试重放。上游来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

需要 Python 3.11+、Linux/macOS（Windows 使用 WSL 或 Linux 服务器）、bash 和 git。未指定 `--image` 时使用当前 Python 环境；修复对象应已能在该环境中运行。Agent 在副本上执行命令，本地执行具有当前用户权限。

## 安装

```bash
python -m pip install -e .             # CLI 修复
python -m pip install -e '.[web]'       # 可选网页
python -m pip install -e '.[eval]'      # 可选 SWE-bench 评测
python -m pip install -e '.[dev,web,eval]'  # 开发和完整程序测试
```

普通安装无需下载数据集、模型权重或准备 Docker。模型通过用户配置的 OpenAI 兼容 Chat Completions / Responses 接口调用。

## 快速开始

激活自己的 Python 环境，先启动本机模型服务，或准备远端服务配置。

```bash
mendpilot --file examples/buggy_average/average.py \
  --task 'average([]) 应返回 0，保留非空列表行为' \
  --base-url http://127.0.0.1:8002/v1 --model qwen2.5-7b-instruct \
  --output artifacts/average-01
```

`--output` 必须是输入目录外的新目录。原文件不覆盖；结果见 `fixed/`、`submitted.patch`、`result.json` 和 `trajectory.json`。

```bash
# 整个项目，附带用户可见测试
mendpilot --project examples/buggy_average --file average.py \
  --task-file examples/average-task.md \
  --test-command 'python -m unittest -v test_average' \
  --output artifacts/project-01

# 只检查输入与配置，不调用模型
mendpilot --file examples/buggy_average/average.py \
  --task '修复空列表' --output artifacts/preview-01 --dry-run

# 可选 Docker：代码复制到容器，不挂载输入目录
docker build -t mendpilot-workspace -f docker/Dockerfile .
mendpilot --project examples/buggy_average --task-file examples/average-task.md \
  --image mendpilot-workspace --output artifacts/docker-01

# 本地网页，仅支持单个 Python 文件
mendpilot-web
```

浏览器打开 `http://127.0.0.1:8765`，拖入一个 `.py` 文件，填写问题及模型接口信息后启动。支持查看日志、停止任务和下载文件/补丁。网页不提供 Docker 或项目上传；这些功能使用 CLI。

## 模型配置

四份模板位于 `src/mendpilot/profiles/`。复制一份为自己的 YAML，填写模型名称和 `base_url`，再使用 `--config path/to/model.yaml`。Qwen/Llama 是本机服务示例；远端模板的 `.example` 地址必须更换。GPT/DeepSeek 的模型名称请以自己的供应商实际支持的标识为准。

密钥不写入 YAML。CLI 可在运行时隐藏输入；自动化可使用配置指定的 `MENDPILOT_API_KEY` 环境变量或标准输入。程序会在启动代码执行前消费该变量。网页密钥只通过子进程标准输入传递，不写配置与日志。不要将实际凭据提交到仓库。

## 使用与评测文档

- [CLI 参数与输出](docs/cli.md)
- [网页功能与运行方式](docs/web.md)
- [项目结构与修复流程](docs/architecture.md)
- [数据、固定划分与独立评分](docs/evaluation.md)
- DeepSeek：mini-swe-agent 与 MendPilot：[CSV](results/deepseek-comparison.csv) · [通过数](results/deepseek-comparison-passed.png) · [调用次数](results/deepseek-comparison-calls.png) · [用时](results/deepseek-comparison-time.png)
- 四种模型实验：[CSV](results/model-comparison.csv) · [通过数](results/model-comparison-passed.png) · [调用次数](results/model-comparison-calls.png) · [用时](results/model-comparison-time.png)

评测使用 `mendpilot-bench prepare/run/evaluate`，仅执行 MendPilot。公开结果表不随新运行改写，也不是程序测试的输入；10 题结果是小样本回归结果。

## 开发测试

```bash
python -m pytest -q
# 准备包含 bash/git/Python 的空 /testbed 镜像后，开启 Docker 集成测试
MENDPILOT_TEST_IMAGE=mendpilot-workspace python -m pytest -q
python -m build
```
