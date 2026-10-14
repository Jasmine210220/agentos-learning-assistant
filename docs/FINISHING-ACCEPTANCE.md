# 0.6.9 收尾验收与复现

## 本轮结果（2026-10-14）

结论：**开发预览，未验收通过**。Windows 操作链通过，内容仍有误报与漏报；公开源码不代表稳定版发布。

- 安装包对应源码提交：`1ad9c9227b0e51492ac3203a49a0851d6811b11b`。后续仅验收脚本与记录变更，不包含在应用二进制中。
- 本地产物：`release-v0.6.9/AgentOS Lite Setup 0.6.9.exe`。
- SHA256：`BBE4F2AE2D5D0D16E06129ECC66D1988AEFDB43327879560E004D3E8D8294256`。
- 146 项程序回归通过、无跳过；前端构建、真实事件进度状态回归通过。测试日志 `test-output/tests-069.xml`；构建记录 `test-output/build-069-r1.log`。
- 安装器退出 0；安装后 app.asar 与后端 exe 均和候选文件哈希一致。Windows 签名状态为 NotSigned。
- 独立目录完成模型配置、真实学习任务、引用高亮、数据图、导出、退出释放后端端口、重启恢复完整成果及加密设置；历史无运行动画。测试运行时移除 Python/Node PATH，测试结束清除该目录的加密密钥副本。不是干净机器验收，未测试 SmartScreen 与卸载。

### 真实内容核对

固定仓库与输入见下文夹具，模型为 `deepseek-flash`。最新安装版任务 `89d71e75b8984a7d8477c6e6d82c6bac` 使用 5 次调用，输入 32965、输出 3214 token；自动审核首次未发现问题，状态 succeeded。12 项结构检查全部通过，但独立源码核对未通过：

1. `analysis.modules.1.title` 的“不可变语义”没有依据；源码只有普通 `@dataclass`，没有 `frozen=True`。
2. 图只有字符串节点向 ModuleData 传入数据，遗漏 `extra_sys_path` 和 `module_paths` 的来源（discover.py:61–64）。
3. “中文字符不影响”为额外且未限定范围的事实断言。
4. 程序已读 discover.py 全文，限制说明仍写“未读消费者”，混淆文件读取与讲解范围。

路径解析、文件且 stem 为 `__init__` 的条件、父包循环与 break、join、三字段返回均逐条对照 discover.py:45–65。三个练习指向真实文件/符号，适合 Python 基础用户的静态阅读，未执行或评分。阅读路线可用，但不能因此忽略上述解读与图中的错误。这里的复核由助手逐条阅读源码完成，不代表用户本人签字验收。

本地证据：`test-output/finishing-windows-r3/{evidence,task,content-evidence,manual-review}.json` 与截图、report.md。内容门禁实际返回非零，未将 succeeded 当内容通过。[脱敏记录](acceptance-0.6.9.json)包含预期、实际、源码提交、目标提交、模型、输入和证据哈希；原始数据目录不公开。

### 失败记录保留

| 记录 | 实际结果 |
| --- | --- |
| finishing-live-r1 | 缺少 Electron 渲染运行时；补齐依赖并加入调用前检查 |
| finishing-live-r2 | 系统成功，源码复核发现虚构去重、文件/目录混淆 |
| finishing-live-r3 | 12 次调用、一次有限修正后仍需关注；未越过调用上限 |
| finishing-review-r1 / r2 / r3 | 4/5、5/6、5/6；r2 存在夹具目标范围不一致，修正后 r3 仍误判正确用例的练习范围 |
| finishing-windows-r1 | 默认安装位置是旧版 0.6.1；版本检查阻断，随后从注册表定位实际安装目录 |
| finishing-windows-r2 | 0.6.8 真实任务保留争议；测试误关成果面板，修正定位方式 |
| finishing-windows-r3 | 0.6.9 完整操作通过；内容复核失败，未隐藏或修改自动状态 |

当前改动已缩小审核自动通过的含义，并保留争议、无效输出和预算不足状态；仍不能保证模型事实正确。未用增加无限调用或反复挑选成功样例来解决质量问题。

按用户授权公开本轮源码及脱敏验收记录；不发布安装包或原始测试数据。状态以最终验收记录为准，构建成功不代表安装或内容通过。

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

如果 npm 的安装脚本策略阻止了 Electron 下载，先运行 `node node_modules/electron/install.js`；真实验收在模型调用前检查渲染运行时。

独立复核使用既有第二次调用，对原疑点提供编号、关闭理由、引用与逐字源码反证。只有这些字段通过校验且无保留问题时才可有据关闭；空 findings 仍不自动通过。

## Windows 候选

把代码提交到本地 Git（无需推送），`npm ci`，安装 Python 开发依赖，执行 `npm run dist`。构建脚本记录源码提交、每个源码哈希与安装包 SHA256，跟踪源码未提交时拒绝打包。默认使用项目 `.venv`；构建机器也可通过 `AGENTOS_BUILD_PYTHON` 指向已有构建解释器，此路径不是运行时依赖。

安装后将 `AGENTOS_TEST_EXE` 指向该安装目录的 exe，设置 `LEARNING_DEPLOYMENT=installed`、`LEARNING_UI_OUTPUT` 为全新目录，再运行 `python scripts/verify_learning_desktop.py`。测试使用隔离数据、移除 Python/Node PATH，通过设置界面保存测试配置，运行固定目标、查看来源与数据图、导出并重启。保存后的测试密钥文件在退出时删除，不上传。测试成功仅表示列明的桌面链路，内容复核单独记录。

本机 Windows 即使移除开发 PATH，也不等于干净 Windows 虚拟机；真实安装、原生界面、模型质量和卸载分别记录，不复用旧版本截图作新版本证据。
