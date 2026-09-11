"""
fonoscan.cli
============

Línea de comandos.

    python -m fonoscan.cli index    --catalog catalogo.csv --out index.npz [--robust]
    python -m fonoscan.cli monitor  --index index.npz --channel FM100 --url http://... 
    python -m fonoscan.cli offline  --index index.npz --file grabacion.mp3
    python -m fonoscan.cli identify --index index.npz --file fragmento.wav
    python -m fonoscan.cli report   --plays plays.jsonl --catalog catalogo.csv \
                                    --channels canales.csv --from 2026-09-01 --to 2026-09-30
    python -m fonoscan.cli audit    --index index.npz     # duplicados de catálogo
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

from .aggregator import Play
from .capture import CaptureConfig
from .catalog import (DEFAULT_SPEEDS, ROBUST_SPEEDS, build_memory_index,
                      detect_duplicates, read_catalog_csv, track_durations)
from .fingerprint import QUERY_CONFIG, REFERENCE_CONFIG, fingerprint_arrays
from .index import MemoryIndex
from .matcher import DecisionPolicy, match
from .recognizer import ChannelRecognizer, RecognizerConfig, recognize_file
from .reporting import Channel, build_usage_report, usage_report_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("fonoscan")


# ---------------------------------------------------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    tracks = read_catalog_csv(args.catalog)
    log.info("catálogo: %d fonogramas", len(tracks))
    speeds = ROBUST_SPEEDS if args.robust else DEFAULT_SPEEDS
    index = build_memory_index(tracks, cfg=REFERENCE_CONFIG, speeds=speeds)
    index.save(args.out)
    size_mb = os.path.getsize(args.out) / 1e6
    log.info(
        "índice guardado en %s | %d landmarks | %.1f MB en disco | %.1f MB en RAM | config %s",
        args.out, index.n_landmarks, size_mb, index.memory_bytes() / 1e6, index.config_id,
    )
    with open(args.out + ".catalog.json", "w", encoding="utf-8") as fh:
        json.dump([t.as_dict() for t in tracks], fh, ensure_ascii=False)
    return 0


def _load(args: argparse.Namespace) -> tuple[MemoryIndex, dict]:
    index = MemoryIndex.load(args.index)
    meta_path = args.index + ".catalog.json"
    tracks = {}
    if os.path.exists(meta_path):
        from .catalog import Track

        with open(meta_path, encoding="utf-8") as fh:
            tracks = {t["track_id"]: Track(**t) for t in json.load(fh)}
    return index, tracks


def cmd_monitor(args: argparse.Namespace) -> int:
    index, tracks = _load(args)
    out = open(args.out, "a", encoding="utf-8") if args.out else sys.stdout

    def on_play(p: Play) -> None:
        d = p.as_dict()
        t = tracks.get(p.track_id)
        if t:
            d.update(isrc=t.isrc, title=t.title, artist=t.artist, label=t.label)
        out.write(json.dumps(d, ensure_ascii=False) + "\n")
        out.flush()
        log.info("PASE %s | %s — %s | %.0f s | %s",
                 p.channel_id, d.get("artist", "?"), d.get("title", p.track_id),
                 p.detected_seconds, d["kind"])

    rec = ChannelRecognizer(
        CaptureConfig(
            url=args.url,
            channel_id=args.channel,
            sample_rate=QUERY_CONFIG.sample_rate,
            evidence_dir=args.evidence_dir,
        ),
        index,
        rec_cfg=RecognizerConfig(window_s=args.window, hop_s=args.hop),
        track_durations={tid: t.duration_s for tid, t in tracks.items()},
        on_play=on_play,
    )
    log.info("monitoreando %s (%s)", args.channel, args.url)
    rec.run()
    log.info("estadísticas: %s", dict(rec.stats))
    return 0


def cmd_offline(args: argparse.Namespace) -> int:
    index, tracks = _load(args)
    plays = recognize_file(
        args.file, index,
        rec_cfg=RecognizerConfig(window_s=args.window, hop_s=args.hop),
        channel_id=args.channel,
        track_durations={tid: t.duration_s for tid, t in tracks.items()},
        stream_epoch=dt.datetime.fromisoformat(args.epoch) if args.epoch else None,
    )
    sink = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    for p in plays:
        d = p.as_dict()
        t = tracks.get(p.track_id)
        if t:
            d.update(isrc=t.isrc, title=t.title, artist=t.artist, label=t.label)
        sink.write(json.dumps(d, ensure_ascii=False) + "\n")
    log.info("%d pases detectados", len(plays))
    return 0


def cmd_identify(args: argparse.Namespace) -> int:
    from .capture import decode_file

    index, tracks = _load(args)
    pcm = decode_file(args.file, QUERY_CONFIG.sample_rate, max_seconds=args.seconds)
    h, t = fingerprint_arrays(pcm, QUERY_CONFIG)
    policy = DecisionPolicy()
    for r in match(h, t, index, QUERY_CONFIG, top_k=args.top_k):
        meta = tracks.get(r.track_id)
        label = f"{meta.artist} — {meta.title} [{meta.isrc}]" if meta else f"track {r.track_id}"
        print(f"{policy.decide(r):>7}  score={r.score:<5} cov={r.coverage:.2f} "
              f"margen={r.margin:.2f} z={r.z:.0f} pos={r.position_s:7.2f}s  {label}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    import csv

    from .catalog import Track

    plays = []
    with open(args.plays, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            p = Play(
                channel_id=d["channel_id"], track_id=d["track_id"], ref_id=d["ref_id"],
                speed=d["speed"], offset_s=d["offset_s"], first_window_s=0.0,
                last_window_s=d["detected_seconds"], window_length_s=0.0,
                hits=d["hits"], score_sum=int(d["mean_score"] * d["hits"]),
                best_score=d["best_score"], min_margin=d["min_margin"],
                review_hits=d["review_hits"], track_duration_s=d["track_duration_s"],
            )
            plays.append(p)

    tracks = {t.track_id: t for t in read_catalog_csv(args.catalog)}
    channels = {}
    if args.channels:
        with open(args.channels, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                channels[row["channel_id"]] = Channel(
                    channel_id=row["channel_id"], name=row.get("name", ""),
                    medium=row.get("medium", "radio"),
                    weight=float(row.get("weight", 1.0)),
                )

    rows = build_usage_report(
        plays, tracks, channels,
        period_start=dt.date.fromisoformat(args.date_from),
        period_end=dt.date.fromisoformat(args.date_to),
        weighting=args.weighting,
    )
    out = usage_report_csv(rows)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(out)
        log.info("reporte escrito en %s (%d filas)", args.out, len(rows))
    else:
        print(out)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    index, tracks = _load(args)
    dups = detect_duplicates(index, min_shared=args.min_shared)
    if not dups:
        print("sin duplicados por encima del umbral")
        return 0
    print(f"{len(dups)} pares de fonogramas con audio muy similar:")
    for a, b, n in dups[:100]:
        ta, tb = tracks.get(a), tracks.get(b)
        print(f"  {n:>6} hashes  |  {ta.isrc if ta else a} {ta.title if ta else ''}"
              f"  <->  {tb.isrc if tb else b} {tb.title if tb else ''}")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fonoscan")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="construir el índice desde un CSV de catálogo")
    p.add_argument("--catalog", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--robust", action="store_true",
                   help="indexar variantes de velocidad ±2%% y ±4%% (5x tamaño)")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("monitor", help="monitorear un stream en vivo")
    p.add_argument("--index", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--window", type=float, default=10.0)
    p.add_argument("--hop", type=float, default=5.0)
    p.add_argument("--evidence-dir", default=None)
    p.add_argument("--out", default=None, help="archivo JSONL de pases")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("offline", help="reconocer una grabación")
    p.add_argument("--index", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--channel", default="offline")
    p.add_argument("--window", type=float, default=10.0)
    p.add_argument("--hop", type=float, default=5.0)
    p.add_argument("--epoch", default=None, help="ISO 8601 del inicio de la grabación")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_offline)

    p = sub.add_parser("identify", help="identificar un fragmento")
    p.add_argument("--index", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--top-k", type=int, default=3)
    p.set_defaults(func=cmd_identify)

    p = sub.add_parser("report", help="generar reporte de uso")
    p.add_argument("--plays", required=True)
    p.add_argument("--catalog", required=True)
    p.add_argument("--channels", default=None)
    p.add_argument("--from", dest="date_from", required=True)
    p.add_argument("--to", dest="date_to", required=True)
    p.add_argument("--weighting", default="por_duracion")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("audit", help="detectar fonogramas duplicados en el catálogo")
    p.add_argument("--index", required=True)
    p.add_argument("--min-shared", type=int, default=500)
    p.set_defaults(func=cmd_audit)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
