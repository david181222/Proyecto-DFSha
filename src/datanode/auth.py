"""Verificación del pase de bloque en el DataNode (DA-10, docs/contratos.md sección 7.2).

El DataNode no consulta al MetaServer: comprueba la firma HMAC del pase con la clave
compartida de DFS_PASS_KEY_FILE y que sea para el hash de la ruta y la operación de
la ruta. El usuario del pase dice en qué carpeta (/data/{user}/) está el bloque.
"""

from typing import Annotated

from fastapi import Depends, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from datanode.deps import PassKeyDep
from dfs_common.errors import DFSError, ErrorCode
from dfs_common.models import SHA256_PATTERN, BlockPass
from dfs_common.security import verify_pass

# auto_error=False: si falta el pase, require_pass (no FastAPI) responde 401 E_AUTH.
pass_auth = HTTPBearer(
    auto_error=False,
    scheme_name="BlockPass",
    bearerFormat="base64url(json).base64url(hmac_sha256(json))",
    description=(
        "Pase del bloque que entrega el MetaServer, en Authorization: Bearer (DA-10). Ausente o vencido "
        "(con firma válida): 401 E_AUTH. Mal formado, con firma inválida, de otro hash o de otra "
        "operación: 403 E_FORBIDDEN."
    ),
)

HashPath = Annotated[str, Path(pattern=SHA256_PATTERN, description="SHA-256 del bloque sin cifrar (DA-02).")]


def require_pass(op: str):
    """Dependencia que exige un pase válido de la operación op ("r" o "w") para el hash de la ruta.

    Sin el encabezado, o si no es Bearer: 401 E_AUTH. Los demás errores los da verify_pass:
    vencido, 401; mal formado, firma inválida, otro hash u otra operación, 403.
    """

    def check_pass(
        hash: HashPath,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(pass_auth)],
        pass_key: PassKeyDep,
    ) -> BlockPass:
        if credentials is None:
            raise DFSError(ErrorCode.E_AUTH, "falta el pase del bloque")
        return verify_pass(credentials.credentials, pass_key, hash=hash, op=op)

    return check_pass
