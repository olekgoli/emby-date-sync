# Emby Date Sync

Small worker container for keeping Emby's `DateCreated` metadata aligned with
the actual import dates from Radarr and Sonarr.

The app is intentionally independent from Chronarr and the Chronarr Emby DLL.
Radarr/Sonarr are the source of truth; Emby is updated through its public HTTP
API.

## Behavior

- reads Radarr and Sonarr API keys from their `config.xml` files,
- reads an active Emby user token from Emby's `authentication.db`,
- fetches movies, series and episodes from Emby,
- matches movies by IMDb/TMDb/path,
- matches episodes by file path, then by series IMDb/TVDB + season/episode,
- updates only `DateCreated` through `POST /Items/{id}`,
- supports dry-run and update limits.

## Local Run

```sh
python3 -m venv .venv
.venv/bin/python -m compileall app tests
.venv/bin/python -m unittest discover -s tests
```

Against a mounted homelab-style filesystem:

```sh
EMBY_URL=http://emby:8096/emby \
RADARR_URL=http://radarr:7878 \
SONARR_URL=http://sonarr:8989 \
EMBY_CONFIG_DIR=/emby-config \
RADARR_CONFIG_DIR=/radarr-config \
SONARR_CONFIG_DIR=/sonarr-config \
SYNC_DRY_RUN=true \
python app/emby_date_sync.py
```

## Container

```sh
docker build -t ghcr.io/olekgoli/emby-date-sync:dev .
docker run --rm \
  -e EMBY_URL=http://emby:8096/emby \
  -e RADARR_URL=http://radarr:7878 \
  -e SONARR_URL=http://sonarr:8989 \
  -v /srv/docker/arr/emby/config:/emby-config:ro \
  -v /srv/docker/arr/radarr/config:/radarr-config:ro \
  -v /srv/docker/arr/sonarr/config:/sonarr-config:ro \
  ghcr.io/olekgoli/emby-date-sync:dev
```

## Release

Releases are tag-driven:

```sh
git tag v$(cat VERSION)
git push origin v$(cat VERSION)
```

The release workflow builds and pushes
`ghcr.io/olekgoli/emby-date-sync:<tag>`, resolves the digest, and opens a
deployment PR against `olekgoli/homelab`.
