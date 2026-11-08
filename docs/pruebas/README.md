# Evidencias de las pruebas

Una evidencia por prueba, con el nombre `NN-nombre.md`, donde `NN` es la tarea que la hizo. Los datos crudos de cada una (CSV o salidas de texto) están en [`datos/`](datos/).

## Pruebas mínimas (Tabla 21 de `docs/decisiones.md`)

| Prueba | Comprueba | Ensayo local | AWS |
|---|---|---|---|
| Carga y recuperación | G1, DA-05 | [25-carga-y-recuperacion](25-carga-y-recuperacion.md) §5.1 (64 MiB); [22-integracion-local](22-integracion-local.md) §5.1 (512 MiB) | [24-despliegue-aws](24-despliegue-aws.md) §5.3 (humo de 512 MiB desde la VM5, 3 corridas); [25-carga-y-recuperacion](25-carga-y-recuperacion.md) §5.2 (512 MiB, commit `7eedf22`) |
| Acceso RF3 | G2, G3, DA-14 | [25-acceso-rf3](25-acceso-rf3.md) §5.1; 22-integracion-local §5.3 | 25-acceso-rf3 §5.2 |
| Concurrencia | G4, DA-08 | [26-concurrencia](26-concurrencia.md) §5.1 | 26-concurrencia §5.2 |
| Fallos: un DataNode | G5, DA-12 | [27-fallos-un-datanode](27-fallos-un-datanode.md) §5.1; 22-integracion-local §5.4 y §5.5 | 27-fallos-un-datanode §5.2 |
| Fallos: zona c | G5, DA-05, DA-12 | [27-fallos-zona-c](27-fallos-zona-c.md) §5.1 | 27-fallos-zona-c §5.2 |
| Fallos: zona b | G5, DA-05 | [27-fallos-zona-b](27-fallos-zona-b.md) §5.1 y §5.2 | 27-fallos-zona-b §5.3 |
| Fallos: reinicio de sesión | DA-12 | [27-reinicio-de-sesion](27-reinicio-de-sesion.md) §5.1 | 27-reinicio-de-sesion §5.2; 24-despliegue-aws §5.6 (comprobación rápida) |
| Consistencia | G1, G2 | [27-consistencia](27-consistencia.md) §5.1 | 27-consistencia §5.2 |
| Seguridad | DA-09, DA-10 | [26-seguridad](26-seguridad.md) §5.1 | 24-despliegue-aws §5.4 (red: puertos y IP privadas); 26-seguridad §5.2 (acceso, canal, disco, discos EBS y red) |
| Rendimiento | DA-03, DA-04 | [25-rendimiento](25-rendimiento.md) §5.1; 22-integracion-local §5.2 (referencia) | 24-despliegue-aws §5.3 (referencia, 3 corridas); 25-rendimiento §5.2 (W=2; la comparación de W es del ensayo local) |

Todas las pruebas tienen su corrida en AWS. La comparación de rendimiento entre W=2 y W=3 se hizo solo en el ensayo local.

## Otras evidencias

| Evidencia | Qué muestra |
|---|---|
| [22-integracion-local](22-integracion-local.md) | El sistema completo en Compose: humo de 512 MiB, medición de referencia, RF3, re-réplica, copias que sobran y limpieza |
| [24-despliegue-aws](24-despliegue-aws.md) | Primer despliegue en las 5 VMs: etcd, MetaServers, líder, humo, red, reinicio de una VM y nueva sesión del laboratorio |

## Plantilla

Cada evidencia tiene las mismas 7 secciones:

1. **Objetivo:** qué se quiere comprobar.
2. **Qué comprueba:** una tabla con cada comprobación, la garantía o decisión que demuestra y su fila de la Tabla 21.
3. **Entorno:** dónde corrió (local o AWS), el commit y los valores de DA-16 que usó.
4. **Pasos:** los comandos exactos, para repetirla.
5. **Resultado esperado y resultado obtenido:** primero el esperado; después la salida de cada corrida (local y AWS) y cómo se lee.
6. **Conclusión.**
7. **Para el video:** qué mostrar.

Cuando quedan puntos abiertos, van en una sección final, "Pendientes".
