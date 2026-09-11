"""
fonoscan.fingerprint
====================

Generador de huellas acústicas (audio fingerprints) tipo *landmark* /
"constellation map", según Wang (2003) "An Industrial-Strength Audio Search
Algorithm", con ajustes pensados para monitoreo de radiodifusión:

  * trabaja sobre PCM mono a 8 kHz (suficiente para radio AM/FM y TV con voz
    encima; reduce 6x el costo de FFT frente a 44.1 kHz);
  * densidad de picos controlada por percentil adaptativo -> robusto ante
    compresión, ecualización y locución superpuesta;
  * hash de 32 bits + offset de 32 bits, empaquetables en un entero de 64 bits
    para índices invertidos compactos;
  * emisión incremental: se puede alimentar bloque a bloque desde un stream
    infinito (radio) sin volver a procesar el pasado.

No depende de ninguna librería AGPL: el código es propio y puede integrarse en
un sistema cerrado de una entidad de gestión colectiva.

IMPORTANTE (propiedad industrial): el esquema de "landmark pairs" fue objeto de
las patentes US6990453 y US7627477. Ambas corresponden a solicitudes de
2000-2003 y su plazo nominal de 20 años ya transcurrió, pero antes de desplegar
en producción conviene una verificación de libertad de operación (FTO) con el
asesor de propiedad industrial de la entidad, para la/s jurisdicción/es de uso.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Iterable, Iterator, NamedTuple

import numpy as np
from scipy import signal as sps
from scipy.ndimage import maximum_filter

# ---------------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FingerprintConfig:
    """Parámetros del extractor. Cambiarlos invalida el índice existente:
    versionar con `version` y re-indexar el catálogo ante cualquier cambio."""

    version: str = "fs-lm-1"

    sample_rate: int = 8000
    n_fft: int = 1024          # 128 ms de ventana
    hop_length: int = 256      # 32 ms -> 31.25 tramas/segundo
    window: str = "hann"

    # Selección de picos
    peak_neighborhood_freq: int = 15   # bins (~117 Hz)
    peak_neighborhood_time: int = 9    # tramas (~288 ms)
    peak_percentile: float = 80.0      # umbral adaptativo por banda
    max_peaks_per_second: int = 40     # techo de densidad

    # Zona objetivo (target zone) para el emparejamiento combinatorio
    fan_out: int = 8
    min_dt_frames: int = 1
    max_dt_frames: int = 63            # ~2 s
    max_df_bins: int = 128             # limita pares absurdos entre extremos

    # Banda útil (Hz). Recorta DC/rumble y el techo de los codecs de baja tasa.
    f_min: float = 120.0
    f_max: float = 3600.0

    @property
    def frames_per_second(self) -> float:
        return self.sample_rate / self.hop_length

    @property
    def bin_hz(self) -> float:
        return self.sample_rate / self.n_fft

    # Campos que definen la *compatibilidad* de los hashes. Dos configuraciones
    # que coinciden en estos campos producen hashes intercambiables aunque
    # difieran en densidad de picos.
    COMPAT_FIELDS = ("version", "sample_rate", "n_fft", "hop_length", "window",
                     "max_dt_frames", "max_df_bins")

    def fingerprint_id(self) -> str:
        """Identificador de compatibilidad. Se guarda junto a cada índice para
        impedir consultas cruzadas entre configuraciones incompatibles."""
        payload = repr({f: getattr(self, f) for f in self.COMPAT_FIELDS}).encode()
        return hashlib.sha256(payload).hexdigest()[:16]

    def with_density(self, *, max_peaks_per_second: int, fan_out: int) -> "FingerprintConfig":
        return dataclasses.replace(
            self, max_peaks_per_second=max_peaks_per_second, fan_out=fan_out
        )


DEFAULT_CONFIG = FingerprintConfig()

# Densidad asimétrica: la referencia se indexa con menos landmarks por segundo
# (ahorra 3-4x de almacenamiento e I/O) y la consulta se extrae densa, de modo
# que igual encuentra suficientes coincidencias exactas. Es la práctica estándar
# en sistemas de landmarks a gran escala.
REFERENCE_CONFIG = DEFAULT_CONFIG.with_density(max_peaks_per_second=22, fan_out=5)
QUERY_CONFIG = DEFAULT_CONFIG.with_density(max_peaks_per_second=45, fan_out=10)


class Peak(NamedTuple):
    frame: int
    bin: int
    magnitude: float


class Landmark(NamedTuple):
    """Par de picos ya empaquetado."""

    hash: int    # uint32
    time: int    # trama del pico ancla (offset dentro del audio)


# ---------------------------------------------------------------------------
# Espectrograma
# ---------------------------------------------------------------------------


def spectrogram(samples: np.ndarray, cfg: FingerprintConfig = DEFAULT_CONFIG) -> np.ndarray:
    """Devuelve magnitud logarítmica en dB con forma (n_bins, n_frames)."""
    if samples.ndim != 1:
        samples = samples.mean(axis=1)
    samples = samples.astype(np.float32, copy=False)

    _, _, stft = sps.stft(
        samples,
        fs=cfg.sample_rate,
        window=cfg.window,
        nperseg=cfg.n_fft,
        noverlap=cfg.n_fft - cfg.hop_length,
        boundary=None,
        padded=False,
        return_onesided=True,
    )
    mag = np.abs(stft).astype(np.float32)
    return 20.0 * np.log10(mag + 1e-10)


# ---------------------------------------------------------------------------
# Selección de picos espectrales
# ---------------------------------------------------------------------------


def find_peaks(spec_db: np.ndarray, cfg: FingerprintConfig = DEFAULT_CONFIG) -> list[Peak]:
    """Máximos locales en la representación tiempo-frecuencia.

    Dos filtros encadenados:
      1. máximo local en una vecindad rectangular (constellation map clásico);
      2. umbral adaptativo por banda de frecuencia (percentil), que evita que
         una locución fuerte en medios acapare todos los picos del segmento.
    """
    n_bins, n_frames = spec_db.shape
    if n_frames == 0:
        return []

    lo = max(1, int(cfg.f_min / cfg.bin_hz))
    hi = min(n_bins - 1, int(cfg.f_max / cfg.bin_hz))
    band = spec_db[lo:hi]

    footprint_shape = (cfg.peak_neighborhood_freq, cfg.peak_neighborhood_time)
    local_max = maximum_filter(band, size=footprint_shape, mode="nearest")
    is_peak = band == local_max

    # Umbral adaptativo: percentil calculado por banda (cuartos del eje f).
    n_band_bins = band.shape[0]
    threshold = np.empty_like(band)
    n_sub = 4
    edges = np.linspace(0, n_band_bins, n_sub + 1, dtype=int)
    for i in range(n_sub):
        a, b = edges[i], edges[i + 1]
        if b <= a:
            continue
        threshold[a:b] = np.percentile(band[a:b], cfg.peak_percentile)
    is_peak &= band > threshold

    f_idx, t_idx = np.nonzero(is_peak)
    if f_idx.size == 0:
        return []
    mags = band[f_idx, t_idx]

    peaks = [Peak(int(t), int(f) + lo, float(m)) for f, t, m in zip(f_idx, t_idx, mags)]

    # Techo de densidad: conservar los más fuertes, repartidos en el tiempo.
    duration_s = max(n_frames / cfg.frames_per_second, 1e-6)
    budget = int(cfg.max_peaks_per_second * duration_s)
    if len(peaks) > budget:
        peaks = _limit_density(peaks, n_frames, cfg, budget)

    peaks.sort(key=lambda p: (p.frame, p.bin))
    return peaks


def _limit_density(
    peaks: list[Peak], n_frames: int, cfg: FingerprintConfig, budget: int
) -> list[Peak]:
    """Reparte el presupuesto de picos en ventanas de 1 s para no vaciar
    los pasajes suaves (intros, finales) que también deben poder matchear."""
    fps = cfg.frames_per_second
    buckets: dict[int, list[Peak]] = {}
    for p in peaks:
        buckets.setdefault(int(p.frame / fps), []).append(p)
    if not buckets:
        return peaks
    per_bucket = max(1, budget // len(buckets))
    out: list[Peak] = []
    for bucket in buckets.values():
        bucket.sort(key=lambda p: p.magnitude, reverse=True)
        out.extend(bucket[:per_bucket])
    return out


# ---------------------------------------------------------------------------
# Empaquetado de pares -> hash de 32 bits
# ---------------------------------------------------------------------------

_F_BITS = 9    # 512 valores -> resolución de 2 bins (~15.6 Hz)
_DT_BITS = 14

_F_MASK = (1 << _F_BITS) - 1
_DT_MASK = (1 << _DT_BITS) - 1


def _pack(f1: int, f2: int, dt: int) -> int:
    return ((f1 & _F_MASK) << (_F_BITS + _DT_BITS)) | ((f2 & _F_MASK) << _DT_BITS) | (dt & _DT_MASK)


def landmarks_from_peaks(
    peaks: list[Peak], cfg: FingerprintConfig = DEFAULT_CONFIG
) -> list[Landmark]:
    """Emparejamiento combinatorio ancla -> zona objetivo."""
    out: list[Landmark] = []
    n = len(peaks)
    for i in range(n):
        anchor = peaks[i]
        paired = 0
        for j in range(i + 1, n):
            target = peaks[j]
            dt = target.frame - anchor.frame
            if dt < cfg.min_dt_frames:
                continue
            if dt > cfg.max_dt_frames:
                break
            if abs(target.bin - anchor.bin) > cfg.max_df_bins:
                continue
            out.append(Landmark(_pack(anchor.bin, target.bin, dt), anchor.frame))
            paired += 1
            if paired >= cfg.fan_out:
                break
    return out


def fingerprint(
    samples: np.ndarray, cfg: FingerprintConfig = DEFAULT_CONFIG
) -> list[Landmark]:
    """PCM mono normalizado -> lista de landmarks."""
    spec = spectrogram(samples, cfg)
    peaks = find_peaks(spec, cfg)
    return landmarks_from_peaks(peaks, cfg)


def fingerprint_arrays(
    samples: np.ndarray, cfg: FingerprintConfig = DEFAULT_CONFIG
) -> tuple[np.ndarray, np.ndarray]:
    """Versión vectorizada de salida: (hashes uint32, tiempos int32)."""
    lms = fingerprint(samples, cfg)
    if not lms:
        return np.empty(0, dtype=np.uint32), np.empty(0, dtype=np.int32)
    arr = np.asarray(lms, dtype=np.int64)
    return arr[:, 0].astype(np.uint32), arr[:, 1].astype(np.int32)


# ---------------------------------------------------------------------------
# Extractor incremental para streams infinitos
# ---------------------------------------------------------------------------


class StreamFingerprinter:
    """Consume bloques de PCM de un stream continuo y emite landmarks con
    marca temporal absoluta (en tramas desde el inicio de la captura).

    Mantiene un solape igual a `max_dt_frames` para no perder los pares que
    cruzan el límite entre bloques.
    """

    def __init__(self, cfg: FingerprintConfig = DEFAULT_CONFIG, block_seconds: float = 5.0):
        self.cfg = cfg
        self.block_samples = int(block_seconds * cfg.sample_rate)
        self.overlap_samples = int(
            (cfg.max_dt_frames + cfg.peak_neighborhood_time) * cfg.hop_length
        )
        self._buffer = np.zeros(0, dtype=np.float32)
        self._frames_emitted = 0   # tramas ya cubiertas (sin contar solape)
        self._samples_consumed = 0

    def push(self, pcm: np.ndarray) -> list[Landmark]:
        """Agrega audio y devuelve los landmarks nuevos, con tiempo absoluto."""
        self._buffer = np.concatenate([self._buffer, pcm.astype(np.float32, copy=False)])
        out: list[Landmark] = []
        while len(self._buffer) >= self.block_samples + self.overlap_samples:
            chunk = self._buffer[: self.block_samples + self.overlap_samples]
            base_frame = self._samples_consumed // self.cfg.hop_length
            emitted_frames = self.block_samples // self.cfg.hop_length
            for lm in fingerprint(chunk, self.cfg):
                if lm.time < emitted_frames:      # descarta la zona de solape
                    out.append(Landmark(lm.hash, lm.time + base_frame))
            self._buffer = self._buffer[self.block_samples :]
            self._samples_consumed += self.block_samples
        return out

    def flush(self) -> list[Landmark]:
        """Procesa la cola del buffer al cerrar la captura."""
        if len(self._buffer) < self.cfg.n_fft:
            self._buffer = np.zeros(0, dtype=np.float32)
            return []
        base_frame = self._samples_consumed // self.cfg.hop_length
        out = [Landmark(lm.hash, lm.time + base_frame) for lm in fingerprint(self._buffer, self.cfg)]
        self._samples_consumed += len(self._buffer)
        self._buffer = np.zeros(0, dtype=np.float32)
        return out


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def frames_to_seconds(frames: float, cfg: FingerprintConfig = DEFAULT_CONFIG) -> float:
    return frames / cfg.frames_per_second


def seconds_to_frames(seconds: float, cfg: FingerprintConfig = DEFAULT_CONFIG) -> int:
    return int(round(seconds * cfg.frames_per_second))


def iter_windows(
    samples: np.ndarray, window_s: float, hop_s: float, sample_rate: int
) -> Iterator[tuple[float, np.ndarray]]:
    """Ventanas deslizantes (t_inicio_segundos, pcm)."""
    w = int(window_s * sample_rate)
    h = int(hop_s * sample_rate)
    for start in range(0, max(len(samples) - w + 1, 1), h):
        yield start / sample_rate, samples[start : start + w]
