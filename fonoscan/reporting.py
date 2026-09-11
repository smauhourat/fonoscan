"""
fonoscan.reporting
==================

Del pase detectado al **reporte de uso** que alimenta la liquidación.

La entidad no reparte "detecciones": reparte unidades de uso ponderadas. Este
módulo implementa las ponderaciones más habituales en gestión colectiva de
derechos conexos y deja el reparto económico fuera (eso lo hace el sistema de
socios, que conoce coeficientes, categorías de usuario y reglas estatutarias).

Ponderaciones implementadas:

``por_pase``          cada emisión cuenta 1, sin importar la duración.
``por_duracion``      segundos efectivamente emitidos (lo más usual en radio).
``por_pase_completo`` sólo cuentan los pases con cobertura >= umbral; los
                      fragmentos se informan aparte (jingles, cortinas, bumpers).
``ponderada``         segundos x coeficiente de audiencia/categoría del medio.

Salidas:

* ``usage_report_csv``  — plano, para carga en el sistema de reparto.
* ``ddex_like_json``    — estructura alineada con el modelo de reporte de uso
                          por grabación (ISRC + período + conteos), pensada para
                          intercambio con otras sociedades y con productores.
"""

from __future__ import annotations

import collections
import csv
import dataclasses
import datetime as dt
import io
import json
from typing import Iterable, Mapping, Optional, Sequence

from .aggregator import Play
from .catalog import Track


@dataclasses.dataclass
class Channel:
    channel_id: str
    name: str
    medium: str = "radio"          # radio | tv | streaming | ambiental
    territory: str = ""
    # Coeficiente de ponderación del medio (audiencia, tarifa, categoría).
    weight: float = 1.0
    license_id: str = ""


@dataclasses.dataclass
class UsageRow:
    period_start: dt.date
    period_end: dt.date
    channel_id: str
    channel_name: str
    medium: str
    track_id: int
    isrc: Optional[str]
    title: str
    artist: str
    label: str
    plays_full: int
    plays_partial: int
    plays_fragment: int
    total_seconds: float
    weighted_units: float
    plays_in_review: int

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["period_start"] = self.period_start.isoformat()
        d["period_end"] = self.period_end.isoformat()
        d["total_seconds"] = round(self.total_seconds, 1)
        d["weighted_units"] = round(self.weighted_units, 4)
        return d


WEIGHTINGS = ("por_pase", "por_duracion", "por_pase_completo", "ponderada")


def build_usage_report(
    plays: Iterable[Play],
    tracks: Mapping[int, Track],
    channels: Mapping[str, Channel],
    *,
    period_start: dt.date,
    period_end: dt.date,
    weighting: str = "por_duracion",
    full_coverage_threshold: float = 0.80,
    include_review: bool = False,
) -> list[UsageRow]:
    """Agrega pases por (canal, fonograma) para un período."""
    if weighting not in WEIGHTINGS:
        raise ValueError(f"ponderación desconocida: {weighting}")

    buckets: dict[tuple[str, int], dict] = collections.defaultdict(
        lambda: {"full": 0, "partial": 0, "fragment": 0, "seconds": 0.0,
                 "units": 0.0, "review": 0}
    )

    for p in plays:
        d = p.as_dict()
        if d["needs_review"] and not include_review:
            buckets[(p.channel_id, p.track_id)]["review"] += 1
            continue

        b = buckets[(p.channel_id, p.track_id)]
        kind = d["kind"]
        b[kind] += 1
        b["seconds"] += p.detected_seconds
        if d["needs_review"]:
            b["review"] += 1

        ch = channels.get(p.channel_id)
        w = ch.weight if ch else 1.0
        if weighting == "por_pase":
            b["units"] += 1.0
        elif weighting == "por_duracion":
            b["units"] += p.detected_seconds
        elif weighting == "por_pase_completo":
            b["units"] += 1.0 if p.work_coverage >= full_coverage_threshold else 0.0
        else:  # ponderada
            b["units"] += p.detected_seconds * w

    rows: list[UsageRow] = []
    for (channel_id, track_id), b in sorted(buckets.items(), key=lambda kv: -kv[1]["units"]):
        t = tracks.get(track_id)
        ch = channels.get(channel_id)
        rows.append(
            UsageRow(
                period_start=period_start,
                period_end=period_end,
                channel_id=channel_id,
                channel_name=ch.name if ch else channel_id,
                medium=ch.medium if ch else "",
                track_id=track_id,
                isrc=t.isrc if t else None,
                title=t.title if t else "",
                artist=t.artist if t else "",
                label=t.label if t else "",
                plays_full=b["full"],
                plays_partial=b["partial"],
                plays_fragment=b["fragment"],
                total_seconds=b["seconds"],
                weighted_units=b["units"],
                plays_in_review=b["review"],
            )
        )
    return rows


def usage_report_csv(rows: Sequence[UsageRow]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].as_dict().keys()))
    writer.writeheader()
    for r in rows:
        writer.writerow(r.as_dict())
    return buf.getvalue()


def ddex_like_json(rows: Sequence[UsageRow], *, sender: str, recipient: str) -> str:
    """Reporte de uso por grabación, estructurado al estilo de los mensajes de
    uso de DDEX (bloque de cabecera + lista de usos por ISRC).

    No pretende ser un XML DDEX válido: es el mismo modelo de datos en JSON,
    listo para mapear al perfil que acuerden las partes. Antes de intercambiar
    con otra sociedad hay que fijar el perfil y la versión del estándar.
    """
    payload = {
        "MessageHeader": {
            "MessageId": f"FONOSCAN-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}",
            "MessageSender": sender,
            "MessageRecipient": recipient,
            "MessageCreatedDateTime": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        "UsageReport": [
            {
                "ResourceReference": r.isrc or f"INTERNAL:{r.track_id}",
                "ISRC": r.isrc,
                "Title": r.title,
                "DisplayArtist": r.artist,
                "LabelName": r.label,
                "UseType": "Broadcast",
                "ServiceDescription": r.channel_name,
                "ServiceType": r.medium,
                "PeriodStartDate": r.period_start.isoformat(),
                "PeriodEndDate": r.period_end.isoformat(),
                "NumberOfUsages": r.plays_full + r.plays_partial,
                "NumberOfFragmentUsages": r.plays_fragment,
                "DurationOfUsageSeconds": round(r.total_seconds, 1),
                "WeightedUnits": round(r.weighted_units, 4),
                "UsagesPendingReview": r.plays_in_review,
            }
            for r in rows
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Control de cobertura del monitoreo
# ---------------------------------------------------------------------------


def coverage_report(
    plays: Sequence[Play],
    monitored_seconds_by_channel: Mapping[str, float],
) -> list[dict]:
    """Qué proporción del tiempo monitoreado quedó identificada.

    Es el indicador que más preguntan los socios y los usuarios de música: si
    una emisora aparece con 35 % de identificación, o emite mucho hablado, o
    hay un agujero de catálogo, o el capturador estuvo caído. Sin este reporte
    no se puede defender un reparto.
    """
    by_channel: dict[str, float] = collections.defaultdict(float)
    counts: dict[str, int] = collections.defaultdict(int)
    for p in plays:
        by_channel[p.channel_id] += p.detected_seconds
        counts[p.channel_id] += 1

    out = []
    for channel_id, monitored in monitored_seconds_by_channel.items():
        identified = by_channel.get(channel_id, 0.0)
        out.append(
            {
                "channel_id": channel_id,
                "monitored_hours": round(monitored / 3600, 2),
                "identified_hours": round(identified / 3600, 2),
                "identification_rate": round(identified / monitored, 4) if monitored else 0.0,
                "plays": counts.get(channel_id, 0),
            }
        )
    return sorted(out, key=lambda r: r["identification_rate"])
