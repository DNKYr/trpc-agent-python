多租户与节点部署

    设计租户模型，至少包含 
    tenant_id、应用配置、模型配置、工具权限、IM 通道配置、数据后端配置、审计策略。
    设计节点部署拓扑，说明 Agent Gateway、Agent Worker、Channel Adapter、Storage Adapter、Admin API、Telemetry Collector 等组件如何协作。
    支持多节点水平扩展，说明用户消息如何路由到正确租户和正确 session。
    说明是否需要 sticky session；如果不需要，说明如何依赖共享 Session / Memory 后端实现无状态 Worker。
    设计租户隔离机制，包括配置隔离、数据隔离、工具权限隔离、日志脱敏和密钥管理。

详细要求

数据同步与多后端支持

    支持不同租户选择不同数据后端，例如 InMemory、Redis、SQL、向量库、对象存储或外部 Memory 服务。
    设计统一的数据访问抽象，说明 Session、Memory、Summary、Artifact、Knowledge、Audit Log 分别如何存储。
    设计数据同步策略，至少覆盖：
    多节点并发写入同一 session 的一致性。
     Session event、state、summary 的更新顺序。
     Memory 写入后的跨节点可见性。
     后端从 Redis 迁移到 SQL 或从本地向量库迁移到远端向量库时的数据迁移方案。
     IM 消息重复投递时的幂等处理。
    说明不同后端的一致性取舍，例如强一致、最终一致、读写延迟、成本和运维复杂度。
    给出一个最小数据模型或表结构示例，至少包含 tenant、agent app、session、message/event、memory、summary、channel binding、audit log。

IM 软件接入

    设计 IM Channel Adapter，支持企业微信、微信客服、微信公众号、Telegram 或其他 IM 通道中的至少两类。
    说明外部 IM 消息如何转换为 tRPC-Agent 的用户输入，Agent Event 如何转换为 IM 回复、流式消息或卡片消息。
    设计 IM 账号和租户绑定方式，包括 webhook URL、token、secret、回调验签、消息去重、用户身份映射。
    说明群聊和单聊的 session_id 生成规则，以及用户跨群、跨租户时的隔离策略。
    考虑 IM 平台限制，例如消息长度、频率限制、异步回复、图片 / 文件消息、撤回或失败重试。

治理、监控和安全

    使用 Filter 设计租户级治理策略，例如工具白名单、敏感信息脱敏、预算限制、危险工具二次确认、IM 用户权限校验。
    设计监控指标，例如请求量、模型调用耗时、工具调用耗时、IM 投递成功率、错误率、token 消耗、每租户成本、Session 后端延迟。
    说明如何接入 OpenTelemetry 或等价 tracing，要求 trace 能串起 IM callback、Runner 执行、Tool 调用、Session / Memory 读写和 IM 回复。
    设计审计日志字段，至少包含 tenant_id、channel、user_id、session_id、agent_name、tool_name、decision、latency、error_type、cost、trace_id。
    说明密钥管理和脱敏策略，IM token、模型 API key、数据库密码不能明文出现在日志、trace 或错误报告中。

故障恢复与运维

    设计节点故障、IM 重试、数据库短暂不可用、模型超时、工具执行失败时的降级策略。
    说明如何做灰度发布和租户级配置回滚。
    说明如何做容量评估，例如每节点并发 session 数、平均 token 消耗、Redis / SQL QPS、IM 回调峰值。
    设计最小可运行部署方案和生产推荐部署方案，可以使用 Docker Compose、Kubernetes 或等价部署方式描述。
