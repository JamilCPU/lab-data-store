# scripts

| Script | Where it runs | What it does |
| --- | --- | --- |
| `build-push.sh` | Primary node, from the repo root | Builds the `ingest-api` image for `linux/amd64` and `linux/arm64` with buildx and pushes it to the lab registry (`REGISTRY`, default `registry.lab:5000`, a lab-internal name). Refuses a dirty tree unless `ALLOW_DIRTY=1`; prints `TAG=<tag>`. |
