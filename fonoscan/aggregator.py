"""
fonoscan.aggregator
===================

Convierte la secuencia de matches por ventana en **pases** (emisiones
consolidadas), que es la unidad que la entidad de gestión efectivamente
liquida.

Invariante que hace todo el trabajo: mientras un mismo fonograma siga sonando
sin cortes, el offset absoluto ``offset = t_stream - t_fonograma`` permanece
constante. Cambia cuando:

  * empieza otro tema (otro ``track_id``);
  * el mismo tema se vuelve a pasar más tarde (mismo ``track_id``, offset muy
    distinto) — son dos pases y se liquidan como dos;
  * hay un salto de edición dentro del tema (mezcla del DJ, corte a tanda).

Salida por pase:

  * ``started_at`` / ``ended_at``    — tiempo de emisión (UTC) con evidencia.
  * ``detected_seconds``            — segundos efectivamente reconocidos.
  * ``work_coverage``               — fracción del fonograma emitida.
  * ``kind``                        — ``full`` | ``partial`` | ``fragment``.
  * ``confidence``                  — score agregado + estado de decisión.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Iterable, Optional

from .matcher import DecisionPolicy, MatchResult


@dataclasses.dataclass
class Play:
    channel_id: str
    track_id: int
    ref_id: int
    speed: float
    offset_s: float

    first_window_s: float
    last_window_s: float
    window_length_s: float

    hits: int = 0
    score_sum: int = 0
    best_score: int = 0
    coverage_sum: float = 0.0
    min_margin: float = 999.0
    review_hits: int = 0

    track_duration_s: float = 0.0
    stream_epoch: Optional[dt.datetime] = None

    # ------------------------------------------------------------------

    @property
    def detected_seconds(self) -> float:
        return max(self.last_window_s + self.window_length_s - self.first_window_s, 0.0)

    @property
    def work_coverage(self) -> float:
        if self.track_duration_s <= 0:
            return 0.0
        return min(self.detected_seconds / self.track_duration_s, 1.0)

    @property
    def mean_score(self) -> float:
        return self.score_sum / self.hits if self.hits else 0.0

    @property
    def kind(self) -> str:
        c = self.work_coverage
        if c >= 0.80:
            return "full"
        if c >= 0.25 or self.detected_seconds >= 30:
            return "partial"
        return "fragment"

    def started_at(self) -> Optional[dt.datetime]:
        if self.stream_epoch is None:
            return None
        return self.stream_epoch + dt.timedelta(seconds=self.first_window_s)

    def ended_at(self) -> Optional[dt.datetime]:
        if self.stream_epoch is None:
            return None
        return self.stream_epoch + dt.timedelta(
            seconds=self.last_window_s + self.window_length_s
        )

    def as_dict(self) -> dict:
        d = {
            "channel_id": self.channel_id,
            "track_id": self.track_id,
            "ref_id": self.ref_id,
            "speed": self.speed,
            "offset_s": round(self.offset_s, 2),
            "detected_seconds": round(self.detected_seconds, 2),
            "work_coverage": round(self.work_coverage, 3),
            "kind": self.kind,
            "hits": self.hits,
            "best_score": self.best_score,
            "mean_score": round(self.mean_score, 1),
            "min_margin": round(self.min_margin, 2),
            "review_hits": self.review_hits,
            "needs_review": self.review_hits * 2 > self.hits or self.hits < 2,
            "track_duration_s": self.track_duration_s,
        }
        s, e = self.started_at(), self.ended_at()
        if s and e:
            d["started_at"] = s.isoformat()
            d["ended_at"] = e.isoformat()
        return d


class PlayAggregator:
    """Máquina de estados por canal. Es *stateful*: instanciar una por canal
    monitoreado y persistir el pase abierto si el worker se reinicia."""

    def __init__(
        self,
        channel_id: str,
        *,
        window_length_s: float = 10.0,
        offset_tolerance_s: float = 1.5,
        max_gap_s: float = 12.0,
        min_hits: int = 2,
        min_detected_seconds: float = 8.0,
        policy: DecisionPolicy | None = None,
        stream_epoch: Optional[dt.datetime] = None,
        track_durations: Optional[dict[int, float]] = None,
    ):
        self.channel_id = channel_id
        self.window_length_s = window_length_s
        self.offset_tolerance_s = offset_tolerance_s
        self.max_gap_s = max_gap_s
        self.min_hits = min_hits
        self.min_detected_seconds = min_detected_seconds
        self.policy = policy or DecisionPolicy()
        self.stream_epoch = stream_epoch
        self.track_durations = track_durations or {}
        self.open_play: Optional[Play] = None
        self._last_seen_s: float = -1e9

    # ------------------------------------------------------------------

    def push(self, window_start_s: float, best: Optional[MatchResult]) -> list[Play]:
        """Alimenta el resultado de una ventana. Devuelve los pases cerrados."""
        closed: list[Play] = []
        decision = self.policy.decide(best)

        if decision == "reject" or best is None:
            if self.open_play and window_start_s - self._last_seen_s > self.max_gap_s:
                closed.extend(self._close())
            return closed

        if self.open_play and self._continues(best, window_start_s):
            self._extend(best, window_start_s, decision)
        else:
            closed.extend(self._close())
            self._start(best, window_start_s, decision)

        self._last_seen_s = window_start_s
        return closed

    def flush(self) -> list[Play]:
        return self._close()

    # ------------------------------------------------------------------

    def _continues(self, m: MatchResult, window_start_s: float) -> bool:
        p = self.open_play
        assert p is not None
        if m.track_id != p.track_id:
            return False
        if window_start_s - self._last_seen_s > self.max_gap_s:
            return False
        return abs(m.offset_s - p.offset_s) <= self.offset_tolerance_s

    def _start(self, m: MatchResult, window_start_s: float, decision: str) -> None:
        self.open_play = Play(
            channel_id=self.channel_id,
            track_id=m.track_id,
            ref_id=m.ref_id,
            speed=m.speed,
            offset_s=m.offset_s,
            first_window_s=window_start_s,
            last_window_s=window_start_s,
            window_length_s=self.window_length_s,
            hits=1,
            score_sum=m.score,
            best_score=m.score,
            coverage_sum=m.coverage,
            min_margin=m.margin,
            review_hits=1 if decision == "review" else 0,
            track_duration_s=self.track_durations.get(m.track_id, 0.0),
            stream_epoch=self.stream_epoch,
        )

    def _extend(self, m: MatchResult, window_start_s: float, decision: str) -> None:
        p = self.open_play
        assert p is not None
        p.last_window_s = window_start_s
        p.hits += 1
        p.score_sum += m.score
        p.best_score = max(p.best_score, m.score)
        p.coverage_sum += m.coverage
        p.min_margin = min(p.min_margin, m.margin)
        if decision == "review":
            p.review_hits += 1
        # Promedio móvil del offset: absorbe deriva de reloj del encoder.
        p.offset_s = 0.8 * p.offset_s + 0.2 * m.offset_s

    def _close(self) -> list[Play]:
        p = self.open_play
        self.open_play = None
        if p is None:
            return []
        if p.hits < self.min_hits and p.detected_seconds < self.min_detected_seconds:
            return []   # ráfaga aislada: se descarta como ruido
        return [p]
