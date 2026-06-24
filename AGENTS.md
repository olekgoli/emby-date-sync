# AGENTS.md

## Project

Emby Date Sync is a small Python container that periodically synchronizes Emby
`DateCreated` values from Radarr/Sonarr file `dateAdded` metadata.

## Rules

- Do not print API keys or Emby access tokens.
- Treat Radarr and Sonarr as the source of truth.
- Update Emby only through the Emby HTTP API; do not write to Emby SQLite files.
- Keep the container dependency-light and suitable for Kubernetes CronJob usage.
- Before finishing changes, run:

```sh
python -m compileall app tests
python -m unittest discover -s tests
```
