# Deployment

The supported MVP strategy deploys an existing Docker Compose application to an existing Linux
host. Daino does not provision the host.

The sequence is read-only inspection, structured plan and risk, explicit approval, immutable bundle
upload, versioned extraction, Compose startup, container/health verification, promotion, and
recording. Production never deploys without `--approve`.

Remote layout:

```text
/opt/apps/example/
  releases/<release-id>/
  current -> releases/<healthy-release>
  shared/
```

If startup or health verification fails, the failed Compose release is stopped, `current` is
restored, the previous Compose project is restarted, and evidence is retained. Environment files,
TLS keys, and other secrets are expected in customer-controlled shared storage and are not bundled.
