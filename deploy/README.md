# 部署指南

多租户 tRPC-Agent 的部署配置与运维说明。详见 `docs/superpowers/specs/2026-09-06-sp6-ops-recovery-design.md`。

## 目录

- `docker-compose.yml` — 最小可运行拓扑（Gateway + Worker + Channel Adapter + Redis + MySQL）
- `k8s/` — 生产推荐拓扑（Deployment/Service/HPA + Secret）

## 最小可运行（Docker Compose）

```bash
export MODEL_API_KEY=...
export IM_TOKEN=...
export IM_SECRET=...
docker compose up -d
```

组件：`gateway`（入口/路由）、`worker`（Runner，`replicas: 2`）、`channel-adapter`（IM webhook）、`redis`、`mysql`。

> `gateway`/`worker`/`channel-adapter` 镜像基于本 SDK 构建（应用层），此处为拓扑示例。

## 生产推荐（Kubernetes）

```bash
kubectl apply -f deploy/k8s/secret.yaml
kubectl apply -f deploy/k8s/redis.yaml
kubectl apply -f deploy/k8s/gateway.yaml
kubectl apply -f deploy/k8s/worker.yaml
kubectl apply -f deploy/k8s/adapter.yaml
kubectl apply -f deploy/k8s/hpa.yaml
```

- Gateway/Worker/Channel Adapter 无状态，HPA 按 CPU 自动扩缩。
- Redis/SQL/向量库用托管服务（或 StatefulSet）；OTel Collector 以 sidecar/DaemonSet 注入。
- 密钥经 `Secret`（`tenant-secrets`）注入，生产用 SealedSecret/ExternalSecrets。

## 关键运维要点

- **灰度/回滚**：`TenantRegistry.rollback(tenant_id, version)` 一键回退；`RunnerPool` 按 `tenant.version` 热加载。
- **降级**：节点故障靠无状态 + readiness probe；IM 重试靠指数退避 + 幂等去重（`ChannelRouter`）；模型超时靠 `ModelRetryConfig`。
- **容量**：见运维手册 §3 估算公式。
