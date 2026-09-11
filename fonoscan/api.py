"""
fonoscan.api
============

API HTTP del sistema. Tres audiencias:

1. **Operación interna** — estado de capturadores, cola de revisión, reproceso.
2. **Socios (productores e intérpretes)** — consulta de pases de su catálogo,
   con evidencia de audio descargable.
3. **Integraciones** — alta de fonogramas, exportación de reportes de uso.

Arranque:  ``uvicorn fonoscan.api:app --host 0.0.0.0 --port 8080``

Autenticación: aquí va un esquema mínimo por API key para que el ejemplo corra.
En producción se reemplaza por el IdP de la entidad (OIDC) con roles
``operador``, ``auditor``, ``socio`` y ``admin``; el rol ``socio`` debe estar
restringido al catálogo del que el socio es titular.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Optional

import numpy as np
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .aggregator import Play
from .catalog import Track, normalize_isrc
from .fingerprint import QUERY_CONFIG, fingerprint_arrays
from .index import MemoryIndex
from .matcher import DecisionPolicy, match
from .reporting import Channel, build_usage_report, ddex_like_json, usage_report_csv

app = FastAPI(
    title="fonoscan — monitoreo de fonogramas",
    version="1.0",
    description="Reconocimiento de fonogramas en flujos de audio para gestión colectiva.",
)

API_KEY = os.environ.get("FONOSCAN_API_KEY", "cambiar-esta-clave")
INDEX_PATH = os.environ.get("FONOSCAN_INDEX", "/var/lib/fonoscan/index.npz")

_state: dict = {"index": None, "tracks": {}, "channels": {}, "plays": []}


def require_key(x_api_key: str = Header(default="")) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


def get_index() -> MemoryIndex:
    if _state["index"] is None:
        if not os.path.exists(INDEX_PATH):
            raise HTTPException(status_code=503, detail="índice no cargado")
        _state["index"] = MemoryIndex.load(INDEX_PATH)
    return _state["index"]


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


class MatchOut(BaseModel):
    track_id: int
    isrc: Optional[str] = None
    title: str = ""
    artist: str = ""
    score: int
    coverage: float
    margin: float
    z: float
    position_s: float
    decision: str


class HealthOut(BaseModel):
    status: str
    index_config_id: str
    n_landmarks: int
    n_references: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    idx = get_index()
    return HealthOut(
        status="ok",
        index_config_id=idx.config_id,
        n_landmarks=idx.n_landmarks,
        n_references=len(idx.references),
    )


@app.post("/identify", response_model=list[MatchOut], dependencies=[Depends(require_key)])
async def identify(
    file: UploadFile = File(..., description="Fragmento de audio (cualquier formato que lea ffmpeg)"),
    top_k: int = Query(3, ge=1, le=10),
) -> list[MatchOut]:
    """Identificación puntual de un fragmento. Se usa para verificación manual,
    para peritajes y para que un socio confirme una detección dudosa."""
    import tempfile

    from .capture import decode_file

    idx = get_index()
    policy = DecisionPolicy()

    suffix = os.path.splitext(file.filename or "")[1] or ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    try:
        pcm = decode_file(tmp_path, QUERY_CONFIG.sample_rate)
    finally:
        os.unlink(tmp_path)

    if len(pcm) < QUERY_CONFIG.sample_rate * 3:
        raise HTTPException(status_code=400, detail="se requieren al menos 3 segundos de audio")

    hashes, times = fingerprint_arrays(pcm, QUERY_CONFIG)
    results = match(hashes, times, idx, QUERY_CONFIG, top_k=top_k)

    out = []
    for r in results:
        t: Track | None = _state["tracks"].get(r.track_id)
        out.append(
            MatchOut(
                track_id=r.track_id,
                isrc=t.isrc if t else None,
                title=t.title if t else "",
                artist=t.artist if t else "",
                score=r.score,
                coverage=round(r.coverage, 3),
                margin=round(r.margin, 2),
                z=round(r.z, 1),
                position_s=round(r.position_s, 2),
                decision=policy.decide(r),
            )
        )
    return out


@app.get("/plays", dependencies=[Depends(require_key)])
def list_plays(
    channel_id: Optional[str] = None,
    isrc: Optional[str] = None,
    since: Optional[dt.datetime] = None,
    until: Optional[dt.datetime] = None,
    needs_review: Optional[bool] = None,
    limit: int = Query(500, le=5000),
) -> list[dict]:
    """Pases detectados. En producción esto consulta la tabla `play`
    (ver deploy/schema.sql) con paginación por cursor."""
    isrc_n = normalize_isrc(isrc) if isrc else None
    rows = []
    for p in _state["plays"]:
        d = p.as_dict()
        t = _state["tracks"].get(p.track_id)
        if channel_id and p.channel_id != channel_id:
            continue
        if isrc_n and (not t or t.isrc != isrc_n):
            continue
        if needs_review is not None and d["needs_review"] != needs_review:
            continue
        start = p.started_at()
        if since and start and start < since:
            continue
        if until and start and start > until:
            continue
        d["isrc"] = t.isrc if t else None
        d["title"] = t.title if t else ""
        d["artist"] = t.artist if t else ""
        rows.append(d)
        if len(rows) >= limit:
            break
    return rows


@app.get("/reports/usage.csv", response_class=PlainTextResponse,
         dependencies=[Depends(require_key)])
def usage_csv(
    period_start: dt.date,
    period_end: dt.date,
    weighting: str = "por_duracion",
) -> str:
    rows = build_usage_report(
        _state["plays"], _state["tracks"], _state["channels"],
        period_start=period_start, period_end=period_end, weighting=weighting,
    )
    return usage_report_csv(rows)


@app.get("/reports/usage.json", dependencies=[Depends(require_key)])
def usage_json(
    period_start: dt.date,
    period_end: dt.date,
    recipient: str = "SOCIEDAD",
    weighting: str = "por_duracion",
) -> dict:
    rows = build_usage_report(
        _state["plays"], _state["tracks"], _state["channels"],
        period_start=period_start, period_end=period_end, weighting=weighting,
    )
    import json as _json

    return _json.loads(ddex_like_json(rows, sender="FONOSCAN", recipient=recipient))


@app.post("/admin/reload-index", dependencies=[Depends(require_key)])
def reload_index() -> dict:
    _state["index"] = MemoryIndex.load(INDEX_PATH)
    return {"status": "ok", "n_landmarks": _state["index"].n_landmarks}
