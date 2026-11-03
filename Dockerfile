# Una sola imagen para MetaServer, DataNode y cliente (tarea 05).
# El servicio lo elige el command de compose: python -m metaserver, python -m datanode o sleep infinity.
FROM python:3.12-slim

# curl, para las comprobaciones de TLS desde dentro de los contenedores.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Solo lo necesario para instalar el paquete. Nunca COPY . . : copiaría secrets/
# dentro de la imagen. Los secretos llegan en /run/secrets/ al correr (compose).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
