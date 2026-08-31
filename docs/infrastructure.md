# Infrastructure as code

D[Ai]NO can validate, plan, apply, and destroy Terraform or OpenTofu projects through the local
runtime. It selects `tofu` when available and otherwise uses `terraform`.

The current project must contain Terraform files and the matching executable must be installed:

```bash
daino infra validate
daino infra plan
```

Validation runs formatting checks and configuration validation. Planning uses the tool's normal
plan output without applying it.

## Apply changes

Apply is refused until the explicit approval flag is present:

```bash
daino infra apply --approve
```

Review the plan before approving. Infrastructure commands still pass through D[Ai]NO's policy
engine and configured timeout.

## Destroy infrastructure

Destroy has a stronger confirmation gate because it is destructive. Both approval and the exact
word `destroy` are required:

```bash
daino infra destroy --approve --confirm destroy
```

!!! danger

    Infrastructure deletion is intentionally separate from ordinary agent command execution.
    Never add a blanket allow rule for `terraform destroy` or `tofu destroy`.

For application releases to an existing Compose host, use [Deployment](deployment.md) instead.
