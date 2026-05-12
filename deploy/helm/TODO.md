# deploy/helm — Kubernetes Helm chart

## Structure to create

```
helm/
    Chart.yaml
    values.yaml
    values.prod.yaml
    templates/
        deployment.yaml
        service.yaml
        ingress.yaml
        hpa.yaml
        pdb.yaml
        configmap.yaml
        secrets-backend-config.yaml
```

## Key values to parameterize

- `replicaCount` per region
- `secretBackend`: `postgres_encrypted` | `vault` | `aws_secrets_manager`
- `litellm.requestTimeoutSeconds`, `litellm.maxRetries`: SDK-level tunables
  (no gateway URL — LiteLLM is in-process)
- `langfuse.host`: self-hosted Langfuse URL
- `resources.requests/limits` for CPU + memory
- `autoscaling.enabled`, `autoscaling.targetCPUUtilizationPercentage`

## HPA + PDB

- HPA on CPU + custom `llm_tokens_per_second` metric (via Prometheus adapter)
- PodDisruptionBudget: `minAvailable: 1` to prevent full outage during rolling deploys

## Phase

Phase 2+
