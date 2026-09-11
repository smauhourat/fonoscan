"""
fonoscan.catalog
================

Alta del catálogo de referencia: los fonogramas cuya difusión se quiere
detectar.

Cada fonograma se identifica por **ISRC** (ISO 3901), que es el identificador
que la industria usa para grabaciones y el que permite cruzar la detección con
la base de titulares (productor y artistas intérpretes) para liquidar.

Notas de calidad de datos, que en la práctica pesan más que el algoritmo:

* Un mismo tema puede tener varios ISRC (remaster, versión de radio, versión de
  álbum, licencias por territorio). Se indexan **todos** como fonogramas
  distintos; el motor los distinguirá si el audio difiere, y si el audio es
  idéntico bit a bit hay que resolverlo por regla de negocio (ver
  ``docs/04-operacion-y-calidad.md``, sección "colisiones de catálogo").
* Los ISRC llegan mal cargados con frecuencia (guiones, minúsculas, ISRC del
  álbum repetido en todos los tracks). Se normaliza y se valida el formato.
* El audio de referencia debe ser el máster comercial. Un rip de YouTube o una
  versión en vivo generan falsos negativos sistemáticos.
"""

from __future__ import annotations

import csv
import dataclasses
import logging
import os
import re
from typing import Iterable, Iterator, Optional, Sequence

import numpy as np

from .capture import decode_file, probe_duration
from .fingerprint import REFERENCE_CONFIG, FingerprintConfig, fingerprint_arrays
from .index import MemoryIndex, Reference

log = logging.getLogger(__name__)

ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}$")


def normalize_isrc(raw: str) -> Optional[str]:
    """`AR-ZZZ-24-00001` -> `ARZZZ2400001`. Devuelve None si no es válido."""
    if not raw:
        return None
    s = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    return s if ISRC_RE.match(s) else None


@dataclasses.dataclass
class Track:
    track_id: int
    isrc: Optional[str]
    title: str = ""
    artist: str = ""
    label: str = ""            # productor fonográfico
    album: str = ""
    year: Optional[int] = None
    duration_s: float = 0.0
    audio_path: str = ""
    # Datos de reparto: se completan desde el sistema de socios de la entidad.
    rights_owner_id: Optional[str] = None
    territory: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def read_catalog_csv(path: str) -> list[Track]:
    """CSV con columnas: isrc,title,artist,label,album,year,audio_path[,rights_owner_id,territory]"""
    tracks: list[Track] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for i, row in enumerate(csv.DictReader(fh), start=1):
            isrc = normalize_isrc(row.get("isrc", ""))
            if row.get("isrc") and not isrc:
                log.warning("fila %d: ISRC inválido %r (se indexa igual, sin ISRC)", i, row["isrc"])
            audio = row.get("audio_path", "").strip()
            tracks.append(
                Track(
                    track_id=i,
                    isrc=isrc,
                    title=row.get("title", "").strip(),
                    artist=row.get("artist", "").strip(),
                    label=row.get("label", "").strip(),
                    album=row.get("album", "").strip(),
                    year=int(row["year"]) if row.get("year", "").strip().isdigit() else None,
                    audio_path=audio,
                    duration_s=probe_duration(audio) if audio and os.path.exists(audio) else 0.0,
                    rights_owner_id=(row.get("rights_owner_id") or "").strip() or None,
                    territory=(row.get("territory") or "").strip(),
                )
            )
    return tracks


# ---------------------------------------------------------------------------
# Indexación
# ---------------------------------------------------------------------------

DEFAULT_SPEEDS: tuple[float, ...] = (1.0,)
ROBUST_SPEEDS: tuple[float, ...] = (0.96, 0.98, 1.0, 1.02, 1.04)


def index_entries(
    tracks: Sequence[Track],
    *,
    cfg: FingerprintConfig = REFERENCE_CONFIG,
    speeds: Sequence[float] = DEFAULT_SPEEDS,
    progress: bool = True,
) -> Iterator[tuple[np.ndarray, np.ndarray, Reference]]:
    """Genera las entradas (hashes, tiempos, referencia) para construir el índice."""
    ref_id = 0
    for n, track in enumerate(tracks, start=1):
        if not track.audio_path or not os.path.exists(track.audio_path):
            log.warning("sin audio: %s (%s)", track.title, track.audio_path)
            continue
        for speed in speeds:
            ref_id += 1
            try:
                pcm = decode_file(track.audio_path, cfg.sample_rate, speed=speed)
            except RuntimeError as exc:
                log.error("no se pudo decodificar %s: %s", track.audio_path, exc)
                continue
            hashes, times = fingerprint_arrays(pcm, cfg)
            ref = Reference(
                ref_id=ref_id,
                track_id=track.track_id,
                speed=speed,
                duration_s=len(pcm) / cfg.sample_rate,
            )
            yield hashes, times, ref
        if progress and n % 50 == 0:
            log.info("indexados %d/%d fonogramas", n, len(tracks))


def build_memory_index(
    tracks: Sequence[Track],
    *,
    cfg: FingerprintConfig = REFERENCE_CONFIG,
    speeds: Sequence[float] = DEFAULT_SPEEDS,
) -> MemoryIndex:
    return MemoryIndex.build(
        index_entries(tracks, cfg=cfg, speeds=speeds),
        config_id=cfg.fingerprint_id(),
    )


def track_durations(tracks: Iterable[Track]) -> dict[int, float]:
    return {t.track_id: t.duration_s for t in tracks}


# ---------------------------------------------------------------------------
# Higiene del catálogo
# ---------------------------------------------------------------------------


def detect_duplicates(index: MemoryIndex, min_shared: int = 500) -> list[tuple[int, int, int]]:
    """Fonogramas con audio prácticamente idéntico (mismo máster con dos ISRC).

    Devuelve tríos ``(track_a, track_b, hashes_compartidos)``. Hay que
    resolverlos **antes** de liquidar: si no, el mismo pase puede adjudicarse a
    un titular u otro según el ruido.
    """
    from collections import Counter, defaultdict

    by_hash: defaultdict[int, set[int]] = defaultdict(set)
    for h, r in zip(index.hashes.tolist(), index.ref_ids.tolist()):
        ref = index.reference(r)
        if ref:
            by_hash[h].add(ref.track_id)

    pair_counts: Counter[tuple[int, int]] = Counter()
    for tracks_with_hash in by_hash.values():
        if 1 < len(tracks_with_hash) <= 4:
            ts = sorted(tracks_with_hash)
            for i in range(len(ts)):
                for j in range(i + 1, len(ts)):
                    pair_counts[(ts[i], ts[j])] += 1

    return [(a, b, c) for (a, b), c in pair_counts.most_common() if c >= min_shared]
