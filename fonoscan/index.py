"""
fonoscan.index
==============

Índice invertido `hash -> (referencia, trama)`.

Dos implementaciones intercambiables:

* :class:`MemoryIndex` — arreglos numpy ordenados + `searchsorted`. Muy rápido,
  sin dependencias. Sirve hasta ~100-150 k fonogramas por nodo (ver cálculo de
  dimensionamiento en `docs/01-arquitectura.md`). Se serializa a `.npz` y se
  carga en los workers de reconocimiento.
* :class:`PostgresIndex` — tabla particionada por prefijo de hash. Es la opción
  de referencia para el catálogo completo de una entidad de gestión y para el
  alta incremental diaria de novedades.

Concepto de **referencia** (`ref_id`): cada fonograma se indexa una o más veces.
La variante base es `speed = 1.0`; opcionalmente se indexan variantes con
tiempo alterado (±2 %, ±4 %), porque muchas emisoras comprimen o estiran la
programación para ajustar tandas. El algoritmo de landmarks no es invariante a
cambios de velocidad, así que la multi-indexación es la contramedida barata
(la alternativa cara es un motor tipo Panako/neural: ver comparativa).
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Iterable, Sequence

import numpy as np


@dataclasses.dataclass
class Reference:
    ref_id: int
    track_id: int
    speed: float = 1.0
    n_landmarks: int = 0
    duration_s: float = 0.0


class BaseIndex:
    def lookup(self, hashes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Dado un vector de hashes de consulta devuelve tres arreglos alineados:
        `(posicion_en_consulta, ref_id, t_referencia)`."""
        raise NotImplementedError

    def reference(self, ref_id: int) -> Reference | None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# En memoria
# ---------------------------------------------------------------------------


class MemoryIndex(BaseIndex):
    """Índice inmutable, ordenado por hash."""

    def __init__(
        self,
        hashes: np.ndarray,
        ref_ids: np.ndarray,
        times: np.ndarray,
        references: dict[int, Reference],
        config_id: str = "",
    ):
        order = np.argsort(hashes, kind="stable")
        self.hashes = hashes[order].astype(np.uint32)
        self.ref_ids = ref_ids[order].astype(np.int32)
        self.times = times[order].astype(np.int32)
        self.references = references
        self.config_id = config_id

    # -- construcción -------------------------------------------------------

    @classmethod
    def build(cls, entries: Iterable[tuple[np.ndarray, np.ndarray, Reference]], config_id: str = "") -> "MemoryIndex":
        hs, rs, ts = [], [], []
        refs: dict[int, Reference] = {}
        for hashes, times, ref in entries:
            if len(hashes) == 0:
                continue
            hs.append(np.asarray(hashes, dtype=np.uint32))
            ts.append(np.asarray(times, dtype=np.int32))
            rs.append(np.full(len(hashes), ref.ref_id, dtype=np.int32))
            ref.n_landmarks = int(len(hashes))
            refs[ref.ref_id] = ref
        if not hs:
            return cls(np.empty(0, np.uint32), np.empty(0, np.int32), np.empty(0, np.int32), {}, config_id)
        return cls(np.concatenate(hs), np.concatenate(rs), np.concatenate(ts), refs, config_id)

    # -- consulta -----------------------------------------------------------

    def lookup(self, hashes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if len(self.hashes) == 0 or len(hashes) == 0:
            e = np.empty(0, dtype=np.int64)
            return e, e.astype(np.int32), e.astype(np.int32)

        q = np.asarray(hashes, dtype=np.uint32)
        lo = np.searchsorted(self.hashes, q, side="left")
        hi = np.searchsorted(self.hashes, q, side="right")
        counts = hi - lo
        total = int(counts.sum())
        if total == 0:
            e = np.empty(0, dtype=np.int64)
            return e, e.astype(np.int32), e.astype(np.int32)

        # Expansión vectorizada de los rangos [lo, hi).
        q_pos = np.repeat(np.arange(len(q), dtype=np.int64), counts)
        starts = np.repeat(lo, counts)
        offsets = np.arange(total, dtype=np.int64) - np.repeat(
            np.concatenate([[0], np.cumsum(counts)[:-1]]), counts
        )
        idx = starts + offsets
        return q_pos, self.ref_ids[idx], self.times[idx]

    def reference(self, ref_id: int) -> Reference | None:
        return self.references.get(int(ref_id))

    # -- persistencia -------------------------------------------------------

    def save(self, path: str) -> None:
        np.savez_compressed(
            path,
            hashes=self.hashes,
            ref_ids=self.ref_ids,
            times=self.times,
            references=np.frombuffer(
                json.dumps([dataclasses.asdict(r) for r in self.references.values()]).encode(),
                dtype=np.uint8,
            ),
            config_id=np.frombuffer(self.config_id.encode(), dtype=np.uint8),
        )

    @classmethod
    def load(cls, path: str) -> "MemoryIndex":
        z = np.load(path, allow_pickle=False)
        refs_json = json.loads(bytes(z["references"]).decode())
        refs = {int(r["ref_id"]): Reference(**r) for r in refs_json}
        obj = cls(z["hashes"], z["ref_ids"], z["times"], refs, bytes(z["config_id"]).decode())
        return obj

    @property
    def n_landmarks(self) -> int:
        return int(len(self.hashes))

    def memory_bytes(self) -> int:
        return self.hashes.nbytes + self.ref_ids.nbytes + self.times.nbytes


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

SCHEMA_SQL = r"""
-- Ver deploy/schema.sql para el esquema completo (catálogo, detecciones,
-- evidencias y auditoría). Aquí sólo la tabla del índice invertido.

CREATE TABLE IF NOT EXISTS fp_reference (
    ref_id       BIGSERIAL PRIMARY KEY,
    track_id     BIGINT      NOT NULL,
    speed        REAL        NOT NULL DEFAULT 1.0,
    config_id    TEXT        NOT NULL,
    n_landmarks  INTEGER     NOT NULL DEFAULT 0,
    duration_s   REAL        NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (track_id, speed, config_id)
);

-- Particionada por prefijo de hash: permite re-indexar o vaciar por lotes y
-- reparte el índice B-tree en estructuras más chicas.
CREATE TABLE IF NOT EXISTS fp_landmark (
    hash     INTEGER NOT NULL,     -- uint32 almacenado como int4 con signo
    ref_id   BIGINT  NOT NULL,
    t        INTEGER NOT NULL
) PARTITION BY HASH (hash);

DO $$
BEGIN
  FOR i IN 0..31 LOOP
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS fp_landmark_p%s PARTITION OF fp_landmark
         FOR VALUES WITH (MODULUS 32, REMAINDER %s)', i, i);
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS fp_landmark_p%s_hash_idx ON fp_landmark_p%s (hash)', i, i);
  END LOOP;
END $$;
"""


def to_signed32(values: np.ndarray) -> np.ndarray:
    """uint32 -> int32 preservando bits (PostgreSQL no tiene enteros sin signo)."""
    return np.asarray(values, dtype=np.uint32).astype(np.int64).astype(np.int32)


class PostgresIndex(BaseIndex):
    """Índice respaldado en PostgreSQL. Requiere `psycopg` (v3)."""

    def __init__(self, conn, config_id: str):
        self.conn = conn
        self.config_id = config_id
        self._ref_cache: dict[int, Reference] = {}

    def create_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        self.conn.commit()

    def add_reference(self, track_id: int, speed: float, duration_s: float) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO fp_reference (track_id, speed, config_id, duration_s)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (track_id, speed, config_id) DO UPDATE SET duration_s = EXCLUDED.duration_s
                   RETURNING ref_id""",
                (track_id, speed, self.config_id, duration_s),
            )
            return int(cur.fetchone()[0])

    def insert_landmarks(self, ref_id: int, hashes: np.ndarray, times: np.ndarray) -> None:
        """Carga masiva con COPY (órdenes de magnitud más rápido que INSERT)."""
        h = to_signed32(hashes)
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM fp_landmark WHERE ref_id = %s", (ref_id,))
            with cur.copy("COPY fp_landmark (hash, ref_id, t) FROM STDIN") as copy:
                for hh, tt in zip(h.tolist(), np.asarray(times, dtype=np.int32).tolist()):
                    copy.write_row((hh, ref_id, tt))
            cur.execute(
                "UPDATE fp_reference SET n_landmarks = %s WHERE ref_id = %s",
                (int(len(h)), ref_id),
            )
        self.conn.commit()

    def lookup(self, hashes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if len(hashes) == 0:
            e = np.empty(0, dtype=np.int64)
            return e, e.astype(np.int32), e.astype(np.int32)

        uniq, inverse = np.unique(np.asarray(hashes, dtype=np.uint32), return_inverse=True)
        params = to_signed32(uniq).tolist()
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT hash, ref_id, t FROM fp_landmark WHERE hash = ANY(%s)",
                (params,),
            )
            rows = cur.fetchall()
        if not rows:
            e = np.empty(0, dtype=np.int64)
            return e, e.astype(np.int32), e.astype(np.int32)

        # Mapear cada fila a todas las posiciones de la consulta con ese hash.
        pos_by_hash: dict[int, list[int]] = {}
        for qpos, u in enumerate(inverse):
            pos_by_hash.setdefault(int(params[u]), []).append(qpos)

        q_pos, ref_ids, times = [], [], []
        for h, ref_id, t in rows:
            for qpos in pos_by_hash.get(int(h), ()):
                q_pos.append(qpos)
                ref_ids.append(ref_id)
                times.append(t)
        return (
            np.asarray(q_pos, dtype=np.int64),
            np.asarray(ref_ids, dtype=np.int32),
            np.asarray(times, dtype=np.int32),
        )

    def reference(self, ref_id: int) -> Reference | None:
        ref_id = int(ref_id)
        if ref_id in self._ref_cache:
            return self._ref_cache[ref_id]
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT ref_id, track_id, speed, n_landmarks, duration_s FROM fp_reference WHERE ref_id = %s",
                (ref_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        ref = Reference(int(row[0]), int(row[1]), float(row[2]), int(row[3]), float(row[4]))
        self._ref_cache[ref_id] = ref
        return ref
