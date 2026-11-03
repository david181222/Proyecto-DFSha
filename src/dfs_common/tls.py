"""Contexto TLS de cliente con la CA propia (DA-06, DA-11).

Lo usan el MetaServer hacia etcd (tarea 06), los DataNodes (tareas 14 y 15) y el
cliente dfs (tarea 16).
"""

import ssl


def client_ssl_context(ca_file, cert_file=None, key_file=None) -> ssl.SSLContext:
    """Verifica al servidor con la CA propia y exige TLS 1.3.

    create_default_context ya trae verify_mode=CERT_REQUIRED y check_hostname=True:
    el certificado del servidor debe estar firmado por la CA y su SAN debe tener la
    IP o el nombre al que se conecta. Con cert_file y key_file, el cliente presenta
    ese certificado (mTLS).
    """
    if (cert_file is None) != (key_file is None):
        raise ValueError("cert_file y key_file van juntos: falta uno de los dos")
    context = ssl.create_default_context(cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if cert_file is not None:
        context.load_cert_chain(str(cert_file), str(key_file))
    return context
