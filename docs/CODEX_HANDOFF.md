# 给本机 Codex 的完整接手指令

将整个 ZIP 交给本机 Codex，复制下面正文。当前包已经包含可运行源码，不要从零重写成一个庞大系统。

---

请接手附件中的 RelayAudit 0.1.0，把它作为一个可复现的 LLM API 中转站能力审计开源项目，完成本机验证与 GitHub 发布。我的 GitHub 账号是 `zhouey314-cloud`，请先通过当前 GitHub 登录状态核验。

这次已授权：在合适的项目目录解压、修复必要问题、运行不收费的 Mock / loopback 测试、建立新的公开 GitHub 仓库并推送本项目。不要改我其他项目。当前阶段不需要购买 API、创建新的 Key、使用历史聊天里的 Key、运行付费外部测试或部署收费服务器。真实 API 校准等待我提供明确的测试端点与环境变量配置后再做；这不影响先完成源码发布。

先完整读取 README.md、docs/METHODOLOGY.md、SECURITY.md 和 docs/VALIDATION.md。遵守本项目的重要定位：报告能力与异常证据，不承诺“100% 检出掺水”，不冒充模型身份认证器。不要给报告添加虚构的“纯度分”、伪造准确率、黑名单或“全网首创”文案。

按以下步骤把事情做完：

1. 在项目目录识别 Python 版本。需要 Python 3.10+；本项目运行时不依赖第三方包。保持当前包结构，除非测试证明有必要修复。
2. 运行 `python3 -m unittest discover -s tests -v`。Windows 使用 `py -3`。当前应有 19 项测试；修复必要问题后记录真实测试数和结果。
3. 运行 `python3 -m relay_audit demo --out reports/local-check-001`，确保页面与数据标有 `synthetic=true`；相同的模型标签应出现模拟能力差异，身份结论仍为 `not_verified`。如果目录已存在，使用新目录。
4. 运行 `python3 -m relay_audit plan --config config.example.json`。这个命令不得读取 Key 或发网络请求。默认 2 个端点、3 个变体的计划为 54 次请求。
5. 检查即将提交的文件：代码、文档、测试和明确模拟的示例报告可提交；本机配置、环境文件、真实报告、Key、客户数据不能提交。密钥扫描只输出发现问题的文件名，不显示秘密值。
6. 检查是否已有本地 Git 仓库和 origin，避免覆盖其他项目。如果这是本包的新项目，初始化 Git，创建 main 分支并提交。先确认仓库路径是本项目。
7. 检查 GitHub CLI 或可用的 GitHub 连接，核验账号。在具有权限时新建 `zhouey314-cloud/relay-audit` 公开仓库并推送。如果这个名字已被无关项目使用，使用 `relay-audit-lite` 或带日期的新名称；不能覆盖无关仓库。
8. 仓库描述：`Local-first, reproducible capability audits for LLM API relays. Evidence, not identity certification.`。保留 MIT License 和相关工作归属。无需开通域名或服务器。
9. 若 GitHub CLI 可用且账号、目录和远端都已核验，新仓库可使用 `gh repo create zhouey314-cloud/relay-audit --public --source=. --remote=origin --push`。这条命令只能在确认是新项目、没有冲突远端时执行。命令失败先检查状态，不要盲目重复创建。
10. 推送后读取远端仓库及 commit，检查源文件确已上传。等待 GitHub Actions 完成，核对测试结果；如果工作流因环境或真实问题失败，修复并重新验证。配置里写了工作流不等于 CI 通过。
11. 准备 0.1.0 发布说明：明确运行方式、19 项原始本地测试、当前 CLI 范围、仅模拟与本地 HTTP 验证、真实身份校准未完成。只有实际创建了 release，才写“release 已发布”。
12. 根据 docs/X_DRAFT.md 准备最终 X 文案，把已经核实的真实 GitHub URL 插入。保留“初版”和限制说明。当前未提供 X 账号发布连接时，交付可复制文案即可，不要声称已发 X。

如果 GitHub 登录或权限不足，请保存全部已完成成果，明确指出缺少的具体能力和下一步，不要把“本地完成”写成“已开源上线”。不因无法真实测模型就停下不收费的源码、演示、测试和文档交付。

最后交付一份简短的真实结果：项目路径、GitHub URL、commit SHA、本机测试结果、CI 结果、演示报告路径、真实 API 校准是否执行、发布说明和 X 文案。不要只回计划或一串让我手动执行的命令。
