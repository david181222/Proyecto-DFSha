"""Resumen por condición del CSV de scripts/pruebas/perf.sh (tarea 25). Solo biblioteca estándar.

Uso:  python scripts/pruebas/perf_summary.py docs/pruebas/datos/25-rendimiento-aws.csv

Imprime una tabla en Markdown con una fila por condición: entorno, W, caché, operación, tamaño y
clientes. Usa solo las repeticiones medidas (repeticion >= 1); las de calentamiento (0) quedan en el
CSV pero no entran aquí.

- n: filas de cliente con tiempo (con 4 clientes y 5 repeticiones, n = 20).
- Mediana y p95 de los segundos de cada cliente, y mediana de su MiB/s.
- p95 por rango más cercano: el valor en la posición ceil(0,95 · n) de la lista ordenada. Con n = 5
  es la posición 5, es decir, el máximo; con n = 20, la posición 19. Con tan pocas muestras, el p95
  no es una estimación fina de la cola: es la peor repetición (o casi), y así se reporta.
- Agregado: con más de un cliente, la mediana del MiB/s de las filas "todos" (el total de MiB entre
  el tiempo del cliente más lento, según el propio dfs).
- SHA-256: cuántos get coincidieron con el original, de cuántos.
"""

import csv
import math
import statistics
import sys
from pathlib import Path


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}".replace(".", ",")


def summarize(rows: list[dict]) -> list[str]:
    groups: dict[tuple, dict[str, list]] = {}
    for row in rows:
        if int(row["repeticion"]) < 1:
            continue
        key = (row["entorno"], row["w"], row.get("cache", "caliente"), row["operacion"],
               int(row["tamano_mib"]), int(row["clientes"]))
        group = groups.setdefault(key, {"seconds": [], "rates": [], "total": [], "sha": []})
        if row["cliente"] == "todos":
            group["total"].append(float(row["mib_s"]))
        elif row["segundos"]:
            group["seconds"].append(float(row["segundos"]))
            group["rates"].append(float(row["mib_s"]))
        if row["operacion"] == "get" and row["cliente"] != "todos":
            group["sha"].append(row["sha_ok"] == "true")
    lines = [
        "| Entorno | W | Caché | Operación | MiB | Clientes | n | Mediana (s) | p95 (s) | MiB/s por cliente (mediana) | MiB/s agregado (mediana) | SHA-256 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2], k[4], k[5], k[3] != "put")):
        env, w, cache, op, mib, clients = key
        group = groups[key]
        seconds = group["seconds"]
        median_s = statistics.median(seconds) if seconds else None
        p95_s = p95(seconds) if seconds else None
        median_rate = statistics.median(group["rates"]) if group["rates"] else None
        total = statistics.median(group["total"]) if group["total"] else None
        sha = f"{sum(group['sha'])}/{len(group['sha'])}" if op == "get" else "-"
        lines.append(
            f"| {env} | {w} | {cache} | {op} | {mib} | {clients} | {len(seconds)} | {fmt(median_s)} "
            f"| {fmt(p95_s)} | {fmt(median_rate, 1)} | {fmt(total, 1)} | {sha} |"
        )
    return lines


def main() -> int:
    if len(sys.argv) != 2:
        print("uso: python scripts/pruebas/perf_summary.py ARCHIVO.csv", file=sys.stderr)
        return 2
    with Path(sys.argv[1]).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    print("\n".join(summarize(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
