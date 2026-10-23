# SWE-bench 自身评测

可选依赖：`python -m pip install -e '.[eval]'`，并准备 Docker。评测只运行 MendPilot，不依赖相邻的 mini-swe-agent 或 SWE-agent 源码目录。框架版本从安装包元数据记录。

## 获取数据

固定数据来自 `princeton-nlp/SWE-bench_Lite`，revision 为 `6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2`。例如下载到用户自行选择的目录：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='princeton-nlp/SWE-bench_Lite', repo_type='dataset', revision='6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2', local_dir='datasets/swe-bench-lite')"
printf '%s\n' '6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2' > datasets/swe-bench-lite/REVISION.txt
```

目录应包含 `REVISION.txt` 及 `data/dev-*.parquet`、`data/test-*.parquet`。已有数据可通过 `--dataset-dir` 指定，无需再次下载。

`benchmarks/swebench-lite/split.json` 的 SHA256 为：`5c4c8b13ca35420f301dc8d9ac95293226ae247c04742b2dfc3da71e196c388c`。固定 8 dev 用于开发，10 test 来自 5 个仓库各 2 题。正式 test 必须按固定顺序执行全部 10 题；dev 可指定 `--ids` 子集。加载时校验校验和、划分互斥、数据 revision 和任务存在性。

## 三个入口

```bash
# 准备官方任务镜像；--check-only 仅检查
mendpilot-bench prepare --dataset-dir datasets/swe-bench-lite --split test

# 生成补丁；模型模板沿用通用 CLI 格式
mendpilot-bench run --dataset-dir datasets/swe-bench-lite --split test \
  --config my-model.yaml --output artifacts/test-new-01

# 本地 Chat 模型还须指定本地 tokenizer，离线加载
mendpilot-bench run --dataset-dir datasets/swe-bench-lite --split test \
  --config my-qwen.yaml --tokenizer-path /path/to/Qwen2.5-7B-Instruct \
  --output artifacts/qwen-new-01

# 独立评分；不调用模型，不需要模型凭据
mendpilot-bench evaluate --dataset-dir datasets/swe-bench-lite \
  --output artifacts/test-new-01
```

生成进程只读取 `instance_id/repo/base_commit/problem_statement`，不读取目标补丁、测试补丁和 FAIL_TO_PASS。各题在官方任务镜像内部恢复基准 commit，保存 `mendpilot/<instance-id>/submitted.patch`、工作区差异、轨迹、计数与验证记录。`manifest.json` 记录实际配置、源码指纹、框架版本、tokenizer 文件校验和及协议，`source.zip` 保存本次源码和协议快照。必须使用新输出目录；新运行不会覆盖结果表。

独立评分读取隐藏字段与已生成补丁，用 SWE-bench 4.1.0 评分。采用离线 editable reinstall 参数，逐题保存改动记录，测试和评分命令保持原定义。评分日志、`scores.json`、`evaluation-summary.json` 位于 `evaluation/primary/`。重评分需指定新的 `--score-name`，原评分保留。评分器存在进程锁，运行后恢复工作目录。官方评分器部分任务配置可能需要访问网络。

## 评测协议与结果解释

评测参数在 `benchmarks/swebench-lite/protocol.yaml`：24k 输入、8k 单次输出、30 次调用、900 秒。Chat 使用对应本地 tokenizer、temperature=0、seed=42、不增加 HTTP 重试；Responses 使用保守输入计数、服务默认推理设置、累计输出 65,536 上限和最多两次重试。默认模板沿用主要预算，实际改动均记录在新 manifest 中；应保持预算一致再比较。

Agent 调用与 HTTP 尝试分开统计。总用时是任务生成用时之和，包含模型/网络、工具及容器开销，不含模型服务启动和独立评分。通过数来自官方隐藏测试；工具自身测试通过或正常提交不能代替官方通过。

公开结果表见 [DeepSeek 对比 CSV](../results/deepseek-comparison.csv) 和 [四模型对比 CSV](../results/model-comparison.csv)。柱状图按指标分别保存：

| 对比 | 通过数 | 调用次数 | 用时 |
|---|---|---|---|
| DeepSeek：mini-swe-agent 与 MendPilot | [查看](../results/deepseek-comparison-passed.png) | [查看](../results/deepseek-comparison-calls.png) | [查看](../results/deepseek-comparison-time.png) |
| 四模型 | [查看](../results/model-comparison-passed.png) | [查看](../results/model-comparison-calls.png) | [查看](../results/model-comparison-time.png) |

两轮 DeepSeek 的配置不完全一致，mini-swe-agent 与 MendPilot 的单次输出上限分别为 32,768 和 8,192；不能将差额全部归因于 Agent 改进。四模型结果来自重构前的同一实现，重构后未重新运行；计数、采样与接口约束存在差异，服务内部权重版本未独立验证。这些是已使用的小规模回归样例，没有多种子重复或置信区间，不应泛化到完整基准。

公开结果表仅用于展示，不参与安装、运行或程序测试。复测请自行获取上述公开数据，填写模型配置，并使用新的输出目录；每次运行会独立保存配置、源码快照、补丁和评分记录。
