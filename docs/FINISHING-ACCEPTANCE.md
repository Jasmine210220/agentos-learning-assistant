# 0.6.7 收尾验收与复现

本轮仅本地修改与验收，不自动推送或发布。状态以最终验收记录为准，构建成功不代表安装或内容通过。

## 四类证据必须分开

| 类型 | 输入 | 成功含义 |
| --- | --- | --- |
| 模拟回归 | pytest 中的固定模型与文件桩 | 程序状态、约束和失败保留符合预期，不证明真实模型质量 |
| 审核校准 | 固定公开源码 + 人为标记的正确/错误主张 + 真实模型 | 该次校准用例命中预期，不是用户任务演示 |
| 真实完整任务 | GitHub 固定提交 + 用户目标 + 真实模型 | 结构和操作检查通过后仍需内容复核 |
| 人工式源码复核 | 事先写下的事实清单 + 当次输出及源码 | 逐项记录结论与依据；不等于目标项目已运行，不等于用户本人签字批准 |

## 从干净克隆准备公开夹具

```powershell
python scripts/prepare_learning_fixture.py
python scripts/verify_learning_review.py test-output/review-r1
```

准备脚本仅下载 `fastapi/fastapi-cli` 固定提交 `093be9ca19f2ef44ba523aae0a98d6c9ea2e0888` 的三个文件，逐个核对 `tests/fixtures/fastapi-cli.json` 中的 SHA256。无私人数据依赖；网络失败或哈希不符直接失败。源码保存在忽略上传的 `test-output/public-fixture`。模型脚本使用自己环境中的 `DEEPSEEK_API_KEY` 或 Windows 已有设置，不应提交任何密钥或数据目录。校准记录含源码提交与文件哈希、目标提交、模型、输入、预期、实际审核及消耗；退出 0 必须所有校准用例命中预期。

## 真实任务预期（运行前固定）

只解释 `get_module_data_from_path` 这一小函数，不扩展整个 CLI。目标、基础与六项预期事实已固定在同一 JSON 中：路径解析、`__init__` 文件条件、父包循环中断、模块名拼接、ModuleData 三字段及静态验证边界。重点防止将调用者的 `sys.path.insert` 或 ImportData 返回归给此函数。

```powershell
python scripts/verify_learning_live.py --output test-output/live-r1
```

默认退出 2 表示结构检查通过但缺少内容复核；1 表示失败；不会仅因 `succeeded` 退出 0。若只检查结构可显式添加 `--structural-only`，记录仍标注内容验收待完成。人工复核 JSON 须绑定 evidence.json 的 SHA256，逐项填写固定事实 ID 的 `passed`、`evidence`，以及 `analysis_checked`、`learning_checked`、`diagram_checked`、`exercises_suitable`、`passed` 布尔值。

```powershell
python scripts/check_learning_acceptance.py test-output/live-r1/evidence.json --manual test-output/live-r1/manual-review.json
```

该门禁核对哈希、结构检查与完整内容核对项，不会凭任务状态通过。所有尝试使用新的输出目录，保留失败。

## Windows 候选

把代码提交到本地 Git（无需推送），`npm ci`，安装 Python 开发依赖，执行 `npm run dist`。构建脚本记录源码提交、每个源码哈希与安装包 SHA256，跟踪源码未提交时拒绝打包。默认使用项目 `.venv`；构建机器也可通过 `AGENTOS_BUILD_PYTHON` 指向已有构建解释器，此路径不是运行时依赖。

安装后将 `AGENTOS_TEST_EXE` 指向该安装目录的 exe，设置 `LEARNING_DEPLOYMENT=installed`、`LEARNING_UI_OUTPUT` 为全新目录，再运行 `python scripts/verify_learning_desktop.py`。测试使用隔离数据、移除 Python/Node PATH，通过设置界面保存测试配置，运行固定目标、查看来源与数据图、导出并重启。保存后的测试密钥文件在退出时删除，不上传。测试成功仅表示列明的桌面链路，内容复核单独记录。

本机 Windows 即使移除开发 PATH，也不等于干净 Windows 虚拟机；真实安装、原生界面、模型质量和卸载分别记录，不复用旧版本截图作新版本证据。
