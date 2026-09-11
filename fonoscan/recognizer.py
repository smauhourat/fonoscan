"""
fonoscan.recognizer
===================

Worker de reconocimiento en tiempo real para **un** canal.

Flujo por canal:

    ffmpeg ──PCM 8k──> StreamFingerprinter ──landmarks──> buffer circular
                                                   │
                              cada `hop_s` segundos │
                                                   ▼
                                      matcher.match(ventana)
                                                   │
                                                   ▼
                                      PlayAggregator ──> pases cerrados
                                                   │
                                                   ▼
                                          sink (DB / cola / callback)

Un proceso puede atender N canales con `asyncio`/hilos, pero en producción
conviene un proceso por canal o por grupo pequeño: aísla fallas de red y
simplifica el reinicio. Con el índice en memoria compartido vía `MemoryIndex`
cargado desde un `.npz` en tmpfs, el costo por canal es de pocos MB.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import logging
import signal
import time
from typing import Callable, Optional

import numpy as np

from .aggregator import Play, PlayAggregator
from .capture import CaptureConfig, StreamCapture
from .fingerprint import QUERY_CONFIG, FingerprintConfig, Landmark, StreamFingerprinter
from .index import BaseIndex
from .matcher import DecisionPolicy, MatchResult, match

log = logging.getLogger(__name__)


@dataclasses.dataclass
class RecognizerConfig:
    window_s: float = 10.0     # audio considerado en cada decisión
    hop_s: float = 5.0         # cada cuánto se decide
    min_landmarks: int = 40    # por debajo: silencio o sólo voz -> no se consulta
    emit_window_events: bool = False   # útil para depurar / calibrar


PlaySink = Callable[[Play], None]
WindowSink = Callable[[float, Optional[MatchResult]], None]


class ChannelRecognizer:
    def __init__(
        self,
        capture_cfg: CaptureConfig,
        index: BaseIndex,
        *,
        fp_cfg: FingerprintConfig = QUERY_CONFIG,
        rec_cfg: RecognizerConfig | None = None,
        policy: DecisionPolicy | None = None,
        track_durations: Optional[dict[int, float]] = None,
        on_play: Optional[PlaySink] = None,
        on_window: Optional[WindowSink] = None,
    ):
        self.capture_cfg = capture_cfg
        self.index = index
        self.fp_cfg = fp_cfg
        self.rec_cfg = rec_cfg or RecognizerConfig()
        self.policy = policy or DecisionPolicy()
        self.track_durations = track_durations or {}
        self.on_play = on_play or (lambda p: log.info("PASE %s", p.as_dict()))
        self.on_window = on_window
        self._stop = False

        self.stats = collections.Counter()

    def stop(self, *_args) -> None:
        self._stop = True

    # ------------------------------------------------------------------

    def run(self) -> None:
        cfg = self.fp_cfg
        fps = cfg.frames_per_second
        window_frames = int(self.rec_cfg.window_s * fps)
        hop_s = self.rec_cfg.hop_s

        capture = StreamCapture(self.capture_cfg)
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        streamer = StreamFingerprinter(cfg, block_seconds=min(hop_s, 5.0))
        buf: collections.deque[Landmark] = collections.deque()
        aggregator: Optional[PlayAggregator] = None

        stream_seconds = 0.0
        next_decision_s = self.rec_cfg.window_s
        epoch: Optional[dt.datetime] = None

        try:
            for pcm in capture:
                if self._stop:
                    break
                if epoch is None:
                    epoch = capture.started_at or dt.datetime.now(dt.timezone.utc)
                    aggregator = PlayAggregator(
                        self.capture_cfg.channel_id,
                        window_length_s=self.rec_cfg.window_s,
                        policy=self.policy,
                        stream_epoch=epoch,
                        track_durations=self.track_durations,
                    )

                stream_seconds += len(pcm) / cfg.sample_rate
                buf.extend(streamer.push(pcm))

                # Descartar landmarks fuera de la ventana de decisión.
                cutoff = int((stream_seconds - self.rec_cfg.window_s) * fps)
                while buf and buf[0].time < cutoff:
                    buf.popleft()

                if stream_seconds < next_decision_s:
                    continue
                next_decision_s += hop_s

                window_start_s = max(stream_seconds - self.rec_cfg.window_s, 0.0)
                best = self._decide(buf, window_start_s)

                if self.on_window and self.rec_cfg.emit_window_events:
                    self.on_window(window_start_s, best)

                assert aggregator is not None
                for play in aggregator.push(window_start_s, best):
                    self.stats["plays"] += 1
                    self.on_play(play)
        finally:
            capture.close()
            if aggregator:
                for play in aggregator.flush():
                    self.on_play(play)

    # ------------------------------------------------------------------

    def _decide(self, buf, window_start_s: float) -> Optional[MatchResult]:
        if len(buf) < self.rec_cfg.min_landmarks:
            self.stats["windows_too_sparse"] += 1
            return None
        arr = np.asarray(buf, dtype=np.int64)
        hashes = arr[:, 0].astype(np.uint32)
        times = arr[:, 1].astype(np.int32)

        t0 = time.perf_counter()
        results = match(
            hashes, times, self.index, self.fp_cfg,
            query_start_s=window_start_s, top_k=3,
        )
        self.stats["query_ms"] += int((time.perf_counter() - t0) * 1000)
        self.stats["windows"] += 1

        if not results:
            self.stats["windows_no_match"] += 1
            return None
        best = results[0]
        decision = self.policy.decide(best)
        self.stats[f"decision_{decision}"] += 1
        return best if decision != "reject" else None


# ---------------------------------------------------------------------------
# Reconocimiento offline (archivos: reproceso, auditoría, peritaje)
# ---------------------------------------------------------------------------


def recognize_file(
    path: str,
    index: BaseIndex,
    *,
    fp_cfg: FingerprintConfig = QUERY_CONFIG,
    rec_cfg: RecognizerConfig | None = None,
    policy: DecisionPolicy | None = None,
    channel_id: str = "offline",
    track_durations: Optional[dict[int, float]] = None,
    stream_epoch: Optional[dt.datetime] = None,
) -> list[Play]:
    """Mismo pipeline que en vivo, sobre un archivo grabado.

    Es la función que sostiene la **reproducibilidad**: cualquier detección
    informada debe poder regenerarse a partir de la evidencia archivada, con la
    misma configuración y la misma versión del índice.
    """
    from .capture import decode_file
    from .fingerprint import fingerprint_arrays

    rec_cfg = rec_cfg or RecognizerConfig()
    policy = policy or DecisionPolicy()
    pcm = decode_file(path, fp_cfg.sample_rate)

    agg = PlayAggregator(
        channel_id,
        window_length_s=rec_cfg.window_s,
        policy=policy,
        stream_epoch=stream_epoch,
        track_durations=track_durations or {},
    )
    plays: list[Play] = []
    sr = fp_cfg.sample_rate
    w = int(rec_cfg.window_s * sr)
    h = int(rec_cfg.hop_s * sr)

    for start in range(0, max(len(pcm) - w + 1, 1), h):
        chunk = pcm[start : start + w]
        if len(chunk) < sr:
            break
        hashes, times = fingerprint_arrays(chunk, fp_cfg)
        window_start_s = start / sr
        best = None
        if len(hashes) >= rec_cfg.min_landmarks:
            results = match(hashes, times, index, fp_cfg, query_start_s=window_start_s)
            if results and policy.decide(results[0]) != "reject":
                best = results[0]
        plays.extend(agg.push(window_start_s, best))
    plays.extend(agg.flush())
    return plays
