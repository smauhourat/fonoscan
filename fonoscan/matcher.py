"""
fonoscan.matcher
================

Etapa de decisión: dado un conjunto de landmarks de consulta y un índice
invertido, decide qué fonograma suena y en qué posición.

El criterio es el histograma de offsets: si la consulta corresponde realmente a
la referencia R, todos los landmarks coincidentes comparten el mismo
desplazamiento `delta = t_referencia - t_consulta`. Las coincidencias casuales
se reparten de forma aproximadamente uniforme sobre todos los offsets posibles,
así que un pico neto en el histograma es la evidencia de un match.

Además del conteo bruto se calculan métricas que la entidad de gestión necesita
para auditar y para fijar umbrales defendibles:

* ``score``          — landmarks alineados en el mismo offset (±1 trama).
* ``coverage``       — fracción del tramo de consulta cubierto por landmarks
                       alineados (detecta matches concentrados en un golpe de
                       batería compartido entre dos temas).
* ``margin``         — cociente entre el mejor y el segundo mejor candidato de
                       *otro* fonograma. Es el mejor predictor de falso positivo.
* ``expected``       — conteo esperado bajo la hipótesis nula (casualidad).
* ``z``              — (score - expected) / sqrt(expected). Umbral operativo.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Sequence

import numpy as np

from .fingerprint import DEFAULT_CONFIG, FingerprintConfig
from .index import BaseIndex

_DELTA_OFFSET = 1 << 22          # permite deltas negativos en la clave
_DELTA_SPAN = 1 << 23


@dataclasses.dataclass
class MatchResult:
    ref_id: int
    track_id: int
    speed: float
    score: int
    coverage: float
    margin: float
    expected: float
    z: float
    # Posición dentro del fonograma en la que comienza la consulta (segundos).
    position_s: float
    # Offset absoluto: t_stream - t_fonograma. Constante mientras el mismo
    # pase siga sonando; es la clave para agregar ventanas consecutivas.
    offset_s: float
    n_query_landmarks: int

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def match(
    q_hashes: np.ndarray,
    q_times: np.ndarray,
    index: BaseIndex,
    cfg: FingerprintConfig = DEFAULT_CONFIG,
    *,
    query_start_s: float = 0.0,
    top_k: int = 3,
    max_candidates_per_hash: int = 400,
) -> list[MatchResult]:
    """Devuelve los `top_k` mejores candidatos, ordenados por score."""
    q_hashes = np.asarray(q_hashes, dtype=np.uint32)
    q_times = np.asarray(q_times, dtype=np.int32)
    n_query = int(len(q_hashes))
    if n_query == 0:
        return []

    q_pos, ref_ids, ref_times = index.lookup(q_hashes)
    if len(q_pos) == 0:
        return []

    # Defensa contra hashes "tóxicos" (silencio, tonos, ruido rosa) que
    # aparecen en miles de referencias y dominan el costo de la consulta.
    if max_candidates_per_hash > 0:
        keep = _cap_per_query_position(q_pos, max_candidates_per_hash)
        q_pos, ref_ids, ref_times = q_pos[keep], ref_ids[keep], ref_times[keep]
        if len(q_pos) == 0:
            return []

    deltas = ref_times.astype(np.int64) - q_times[q_pos].astype(np.int64)
    keys = ref_ids.astype(np.int64) * _DELTA_SPAN + (deltas + _DELTA_OFFSET)

    uniq_keys, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    uk_ref = (uniq_keys // _DELTA_SPAN).astype(np.int64)
    uk_delta = (uniq_keys % _DELTA_SPAN) - _DELTA_OFFSET

    # Tolerancia de ±1 trama: sumar bins contiguos del mismo ref.
    tolerant = counts.astype(np.int64).copy()
    order = np.lexsort((uk_delta, uk_ref))
    o_ref, o_delta, o_cnt = uk_ref[order], uk_delta[order], counts[order]
    for shift in (1, -1):
        neighbour = np.zeros_like(o_cnt)
        if shift == 1:
            same = (o_ref[1:] == o_ref[:-1]) & (o_delta[1:] == o_delta[:-1] + 1)
            neighbour[1:][same] = o_cnt[:-1][same]
        else:
            same = (o_ref[:-1] == o_ref[1:]) & (o_delta[:-1] == o_delta[1:] - 1)
            neighbour[:-1][same] = o_cnt[1:][same]
        tolerant[order] += neighbour

    # Mejor bin por referencia.
    best_per_ref: dict[int, int] = {}
    for i in np.argsort(-tolerant):
        r = int(uk_ref[i])
        if r not in best_per_ref:
            best_per_ref[r] = int(i)
        if len(best_per_ref) >= max(top_k * 4, 8):
            break

    ranked = sorted(best_per_ref.items(), key=lambda kv: -tolerant[kv[1]])

    # Modelo nulo: si hay C coincidencias repartidas uniformemente sobre D
    # offsets plausibles, cada bin recibe C/D en promedio.
    total_candidates = float(len(q_pos))
    span = max(int(q_times.max() - q_times.min()), 1)
    plausible_offsets = max(span, 1) * 2.0
    expected = max(total_candidates / plausible_offsets, 1e-3)

    fps = cfg.frames_per_second
    results: list[MatchResult] = []
    for rank, (ref_id, i) in enumerate(ranked[:top_k]):
        ref = index.reference(ref_id)
        if ref is None:
            continue
        score = int(tolerant[i])
        delta = int(uk_delta[i])

        # Cobertura temporal: cuántas ventanas de 1 s del tramo de consulta
        # aportan al menos un landmark alineado.
        aligned = (ref_ids == ref_id) & (np.abs(deltas - delta) <= 1)
        if aligned.any():
            covered_frames = np.unique(q_times[q_pos[aligned]] // int(fps))
            total_buckets = max(int(math.ceil(span / fps)), 1)
            coverage = min(len(covered_frames) / total_buckets, 1.0)
        else:
            coverage = 0.0

        other = [tolerant[j] for r2, j in ranked if index.reference(r2) and
                 index.reference(r2).track_id != ref.track_id]
        margin = float(score / other[0]) if other else float("inf")

        z = (score - expected) / math.sqrt(expected)

        position_s = (delta + q_times.min()) / fps
        offset_s = query_start_s - position_s

        results.append(
            MatchResult(
                ref_id=int(ref_id),
                track_id=int(ref.track_id),
                speed=float(ref.speed),
                score=score,
                coverage=float(coverage),
                margin=float(min(margin, 999.0)),
                expected=float(expected),
                z=float(z),
                position_s=float(position_s),
                offset_s=float(offset_s),
                n_query_landmarks=n_query,
            )
        )
    return results


def _cap_per_query_position(q_pos: np.ndarray, cap: int) -> np.ndarray:
    """Máscara booleana que limita cuántos candidatos aporta cada landmark."""
    keep = np.ones(len(q_pos), dtype=bool)
    order = np.argsort(q_pos, kind="stable")
    sorted_pos = q_pos[order]
    _, starts, counts = np.unique(sorted_pos, return_index=True, return_counts=True)
    for s, c in zip(starts, counts):
        if c > cap:
            keep[order[s + cap : s + c]] = False
    return keep


@dataclasses.dataclass
class DecisionPolicy:
    """Umbrales de aceptación. Se calibran con el set de validación propio de
    la entidad (ver `docs/04-operacion-y-calidad.md`); estos valores son un
    punto de partida razonable para ventanas de 10 s de radio FM."""

    min_score: int = 25
    min_coverage: float = 0.35
    min_margin: float = 1.6
    min_z: float = 8.0

    # Zona gris: se acepta provisoriamente pero se encola para revisión humana.
    review_score: int = 15
    review_margin: float = 1.25

    def decide(self, m: MatchResult | None) -> str:
        """Devuelve 'accept', 'review' o 'reject'."""
        if m is None:
            return "reject"
        if (
            m.score >= self.min_score
            and m.coverage >= self.min_coverage
            and m.margin >= self.min_margin
            and m.z >= self.min_z
        ):
            return "accept"
        if m.score >= self.review_score and m.margin >= self.review_margin:
            return "review"
        return "reject"
