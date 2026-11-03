"""python -m datanode: API pública (DFS_DN_PUBLIC_PORT, HTTPS) e interna (DFS_DN_INTERNAL_PORT, mTLS)."""

from datanode.internal_api import app as internal_app
from datanode.public_api import app as public_app
from dfs_common.config import Settings
from dfs_common.serve import run


def main() -> None:
    settings = Settings()
    settings.require("node_id", "node_ip", "zone", "meta_seeds")
    run(settings, (public_app, settings.dn_public_port), (internal_app, settings.dn_internal_port))


if __name__ == "__main__":
    main()
