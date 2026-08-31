# Deployment

D[Ai]NO deploys an existing Docker Compose application to an existing Linux host. It does not
provision the host, DNS, TLS, a reverse proxy, or a container registry.

## Configure a target

Add a target to `.daino/config.yaml`:

```yaml
deployment:
  targets:
    production:
      type: ssh
      host: app.example.com
      port: 22
      username: deployer
      auth:
        key_path: ~/.ssh/id_ed25519
        known_hosts: ~/.ssh/known_hosts
      deployment_path: /opt/apps/example
      strategy: docker-compose
      compose_file: compose.yaml
      environment: production
      health_url: https://app.example.com/health
      health_commands: []
      retain_releases: 5
```

The remote host needs Docker Compose and write access to `deployment_path`. SSH keys remain local;
their contents are never copied into prompts. Normal host-key verification applies, or use an
explicit `known_hosts` file as shown.

For local Compose release testing, a target may use `type: local-docker` and omit SSH fields.

## Inspect and plan

Start with the read-only operations:

```bash
daino deploy inspect --target production
daino deploy plan --target production
```

Inspection gathers a bounded set of host and Compose facts. Planning returns structured release,
risk, health, and rollback information without uploading or starting anything.

## Deploy a release

Production apply requires an explicit flag:

```bash
daino deploy apply --target production --approve
daino deploy verify --target production
daino deploy status --target production
```

The workflow creates an immutable source bundle, uploads and extracts it under a versioned release
directory, starts Compose, checks container state and configured health checks, and only then
promotes `current`.

```text
/opt/apps/example/
  releases/<release-id>/
  current -> releases/<healthy-release>
  shared/
```

Environment files, TLS keys, and other secrets belong in operator-controlled shared storage and
are not placed in the release bundle.

## Logs and rollback

Retrieve bounded Compose logs or return to the previous healthy release:

```bash
daino deploy logs --target production
daino deploy rollback --target production --approve
```

If startup or health verification fails during apply, D[Ai]NO stops the failed Compose release,
restores `current`, restarts the previous release, and retains the failed deployment evidence.
Manual rollback uses the same health-gated release records and requires explicit approval.

!!! warning

    Validate a target with `inspect` and `plan` before its first apply. D[Ai]NO's release workflow
    is not a substitute for backups of persistent volumes or external databases.
