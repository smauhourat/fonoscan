"""
fonoscan.capture
================

Captura de audio desde cualquier fuente que ffmpeg sepa abrir:

  * Icecast / SHOUTcast  ``http://host:8000/stream``
  * HLS                  ``https://host/playlist.m3u8``
  * RTMP / RTSP / SRT     (TV, contribución)
  * MPEG-TS multicast    ``udp://@239.0.0.1:1234`` (TDT vía receptor IP)
  * Dispositivo local    ``alsa:hw:1,0`` / ``pulse`` (receptores FM/AM con
                         placa de sonido, o dongles RTL-SDR vía `rtl_fm`)
  * Archivo              (reproceso histórico, peritajes)

Devuelve PCM float32 mono a la frecuencia del extractor.

Dos salidas simultáneas por canal:
  1. el stream PCM para reconocimiento (baja tasa, descartable);
  2. opcionalmente, un archivo comprimido rotativo que constituye la
     **evidencia** del pase. Sin evidencia recuperable, una detección es
     difícil de sostener frente al reclamo de un usuario de música.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import logging
import os
import shutil
import subprocess
import time
from typing import Iterator, Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclasses.dataclass
class CaptureConfig:
    url: str
    channel_id: str
    sample_rate: int = 8000
    chunk_seconds: float = 1.0
    reconnect_delay_s: float = 5.0
    max_reconnect_delay_s: float = 120.0
    timeout_s: float = 30.0
    extra_input_args: tuple[str, ...] = ()
    # Evidencia
    evidence_dir: Optional[str] = None
    evidence_segment_seconds: int = 300
    evidence_bitrate: str = "48k"


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg no está instalado o no está en el PATH")
    return path


def _input_args(cfg: CaptureConfig) -> list[str]:
    args: list[str] = []
    if cfg.url.startswith(("http://", "https://")):
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-rw_timeout", str(int(cfg.timeout_s * 1_000_000)),
            "-user_agent", "fonoscan/1.0 (monitoreo de difusión)",
        ]
    args += list(cfg.extra_input_args)
    return args


class StreamCapture:
    """Iterador de bloques PCM. Se reconecta solo ante caídas del stream."""

    def __init__(self, cfg: CaptureConfig):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.evidence_proc: Optional[subprocess.Popen] = None
        self.started_at: Optional[dt.datetime] = None
        self._delay = cfg.reconnect_delay_s

    # ------------------------------------------------------------------

    def _spawn(self) -> subprocess.Popen:
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin",
            *_input_args(self.cfg),
            "-i", self.cfg.url,
            "-vn",
            "-ac", "1",
            "-ar", str(self.cfg.sample_rate),
            "-f", "f32le",
            "-",
        ]
        log.info("[%s] abriendo %s", self.cfg.channel_id, self.cfg.url)
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

    def _spawn_evidence(self) -> Optional[subprocess.Popen]:
        d = self.cfg.evidence_dir
        if not d:
            return None
        os.makedirs(os.path.join(d, self.cfg.channel_id), exist_ok=True)
        pattern = os.path.join(d, self.cfg.channel_id, "%Y-%m-%dT%H-%M-%S.opus")
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin",
            *_input_args(self.cfg),
            "-i", self.cfg.url,
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libopus", "-b:a", self.cfg.evidence_bitrate,
            "-f", "segment",
            "-segment_time", str(self.cfg.evidence_segment_seconds),
            "-strftime", "1",
            "-reset_timestamps", "1",
            pattern,
        ]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[np.ndarray]:
        chunk_bytes = int(self.cfg.chunk_seconds * self.cfg.sample_rate) * 4  # float32
        self.evidence_proc = self._spawn_evidence()
        while True:
            self.proc = self._spawn()
            self.started_at = dt.datetime.now(dt.timezone.utc)
            assert self.proc.stdout is not None
            try:
                while True:
                    raw = self.proc.stdout.read(chunk_bytes)
                    if not raw or len(raw) < chunk_bytes:
                        break
                    self._delay = self.cfg.reconnect_delay_s
                    yield np.frombuffer(raw, dtype=np.float32)
            finally:
                self._terminate(self.proc)
            log.warning(
                "[%s] stream interrumpido; reintento en %.0fs",
                self.cfg.channel_id, self._delay,
            )
            time.sleep(self._delay)
            self._delay = min(self._delay * 2, self.cfg.max_reconnect_delay_s)

    def close(self) -> None:
        for p in (self.proc, self.evidence_proc):
            self._terminate(p)

    @staticmethod
    def _terminate(p: Optional[subprocess.Popen]) -> None:
        if p is None or p.poll() is not None:
            return
        with contextlib.suppress(Exception):
            p.terminate()
            p.wait(timeout=5)
        with contextlib.suppress(Exception):
            p.kill()


# ---------------------------------------------------------------------------
# Decodificación de archivos (alta de catálogo y reproceso)
# ---------------------------------------------------------------------------


def decode_file(
    path: str, sample_rate: int = 8000, *, speed: float = 1.0, max_seconds: float | None = None
) -> np.ndarray:
    """Decodifica un archivo a PCM mono float32.

    ``speed`` != 1.0 aplica cambio de tempo+tono (``asetrate``), que es el tipo
    de alteración que introducen las emisoras al ajustar la grilla. Se usa para
    generar variantes de referencia en el índice.

    El ``aresample`` inicial no es redundante: ``-ar`` es opción de salida y se
    aplica *después* del grafo de filtros, así que sin él ``asetrate`` recibiría
    el audio a la tasa nativa del archivo (44,1 kHz típicamente) y lo estiraría
    por ``tasa_nativa / (sample_rate * speed)`` — un factor de ~5,5, no el ±4 %
    buscado. Las variantes quedaban inservibles en silencio.
    """
    filters = []
    if abs(speed - 1.0) > 1e-6:
        filters.append(
            f"aresample={sample_rate},"
            f"asetrate={int(sample_rate * speed)},"
            f"aresample={sample_rate}"
        )
    cmd = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", path, "-vn", "-ac", "1", "-ar", str(sample_rate),
    ]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-f", "f32le", "-"]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falló con {path}: {proc.stderr.decode()[:500]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def probe_duration(path: str) -> float:
    cmd = [
        shutil.which("ffprobe") or "ffprobe", "-v", "error",
        "-show_entries", "format=duration", "-of", "csv=p=0", path,
    ]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return float(out.stdout.decode().strip())
    except ValueError:
        return 0.0
