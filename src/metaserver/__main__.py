"""python -m metaserver: API pública (DFS_META_PUBLIC_PORT, HTTPS) e interna (DFS_META_INTERNAL_PORT, mTLS)."""

from dfs_common.config import Settings
from dfs_common.serve import run
from metaserver.internal_api import app as internal_app
from metaserver.public_api import app as public_app


def main() -> None:
    settings = Settings()
    run(settings, (public_app, settings.meta_public_port), (internal_app, settings.meta_internal_port))


if __name__ == "__main__":
    main()
