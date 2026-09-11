"""Prueba end-to-end con audio sintético.

Genera un catálogo de 'fonogramas' sintéticos, arma una emisión simulada que
concatena fragmentos con ruido, compresión de rango dinámico y ecualización,
y verifica que el pipeline los reconozca y los consolide en pases.

Ejecutar:  python -m pytest tests/ -v   ó   python tests/test_end_to_end.py
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fonoscan.aggregator import PlayAggregator
from fonoscan.fingerprint import (DEFAULT_CONFIG, QUERY_CONFIG,
                                  REFERENCE_CONFIG, fingerprint_arrays)
from fonoscan.index import MemoryIndex, Reference
from fonoscan.matcher import DecisionPolicy, match

SR = DEFAULT_CONFIG.sample_rate
RNG = np.random.default_rng(20260911)


# ---------------------------------------------------------------------------
# Generación de "música" sintética con estructura espectral rica
# ---------------------------------------------------------------------------


def synth_track(seed: int, duration_s: float = 90.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(duration_s * SR)
    t = np.arange(n) / SR
    out = np.zeros(n, dtype=np.float32)

    scale = np.array([220, 246.94, 261.63, 293.66, 329.63, 392.0, 440.0, 493.88])
    note_len = 0.35 + rng.random() * 0.25
    n_notes = int(duration_s / note_len)
    for k in range(n_notes):
        f0 = rng.choice(scale) * rng.choice([1, 2, 4])
        start = int(k * note_len * SR)
        end = min(start + int(note_len * 1.4 * SR), n)
        seg = np.arange(end - start) / SR
        env = np.exp(-3.0 * seg)
        voice = np.zeros(end - start, dtype=np.float32)
        for h in range(1, 7):
            voice += (0.9 / h) * np.sin(2 * np.pi * f0 * h * seg + rng.random() * 6.28)
        out[start:end] += (voice * env).astype(np.float32)

    # Percusión: ruido filtrado en golpes regulares
    beat = 0.5
    for k in range(int(duration_s / beat)):
        start = int(k * beat * SR)
        end = min(start + int(0.12 * SR), n)
        hit = rng.standard_normal(end - start).astype(np.float32)
        hit *= np.exp(-30 * np.arange(end - start) / SR)
        out[start:end] += 0.6 * hit

    out += 0.01 * rng.standard_normal(n).astype(np.float32)
    return (out / (np.abs(out).max() + 1e-9)).astype(np.float32)


def degrade(x: np.ndarray, snr_db: float = 8.0) -> np.ndarray:
    """Simula la cadena de emisión: recorte de banda, compresión y locución."""
    from scipy import signal as sps

    sos = sps.butter(4, [200, 3400], btype="band", fs=SR, output="sos")
    y = sps.sosfilt(sos, x).astype(np.float32)
    y = np.tanh(2.5 * y).astype(np.float32)            # compresor/limitador
    noise = RNG.standard_normal(len(y)).astype(np.float32)
    p_sig, p_noise = np.mean(y ** 2), np.mean(noise ** 2)
    scale = np.sqrt(p_sig / (p_noise * 10 ** (snr_db / 10)))
    return (y + scale * noise).astype(np.float32)


# ---------------------------------------------------------------------------


def build_index(tracks: dict[int, np.ndarray]) -> MemoryIndex:
    entries = []
    for track_id, pcm in tracks.items():
        h, t = fingerprint_arrays(pcm, REFERENCE_CONFIG)
        entries.append(
            (h, t, Reference(ref_id=track_id, track_id=track_id, duration_s=len(pcm) / SR))
        )
    return MemoryIndex.build(entries, config_id=REFERENCE_CONFIG.fingerprint_id())


def test_recognition_end_to_end() -> None:
    n_tracks = 25
    tracks = {i: synth_track(1000 + i) for i in range(1, n_tracks + 1)}
    index = build_index(tracks)
    print(f"índice: {index.n_landmarks:,} landmarks, {index.memory_bytes()/1e6:.1f} MB")

    # Emisión simulada: 4 temas, fragmentos de 40 s, con 3 s de separación.
    programa = [(3, 10.0, 45.0), (17, 0.0, 40.0), (8, 25.0, 42.0), (21, 5.0, 38.0)]
    pieces, verdad = [], []
    cursor = 0.0
    for track_id, start_s, dur_s in programa:
        gap = np.zeros(int(3.0 * SR), dtype=np.float32)
        pieces.append(gap)
        cursor += 3.0
        seg = tracks[track_id][int(start_s * SR) : int((start_s + dur_s) * SR)]
        pieces.append(seg)
        verdad.append((track_id, cursor, cursor + dur_s))
        cursor += dur_s
    emision = degrade(np.concatenate(pieces), snr_db=8.0)

    window_s, hop_s = 10.0, 5.0
    policy = DecisionPolicy()
    agg = PlayAggregator("TEST-FM", window_length_s=window_s, policy=policy,
                         track_durations={i: len(p) / SR for i, p in tracks.items()})

    plays, w, h = [], int(window_s * SR), int(hop_s * SR)
    ventanas = aciertos = 0
    for start in range(0, len(emision) - w + 1, h):
        chunk = emision[start : start + w]
        qh, qt = fingerprint_arrays(chunk, QUERY_CONFIG)
        best = None
        if len(qh) >= 40:
            res = match(qh, qt, index, query_start_s=start / SR)
            if res and policy.decide(res[0]) != "reject":
                best = res[0]
        ventanas += 1
        t_mid = (start + w / 2) / SR
        esperado = next((tid for tid, a, b in verdad if a <= t_mid <= b), None)
        if best and best.track_id == esperado:
            aciertos += 1
        elif esperado is None and best is None:
            aciertos += 1
        plays.extend(agg.push(start / SR, best))
    plays.extend(agg.flush())

    print(f"ventanas correctas: {aciertos}/{ventanas} ({100*aciertos/ventanas:.0f} %)")
    print("\npases detectados:")
    for p in plays:
        d = p.as_dict()
        print(f"  track {d['track_id']:>3}  {d['detected_seconds']:>6.1f} s  "
              f"score~{d['mean_score']:>6.0f}  margen {d['min_margin']:>5.2f}  {d['kind']}")

    detectados = [p.track_id for p in plays]
    esperados = [tid for tid, _, _ in programa]
    assert detectados == esperados, f"esperado {esperados}, obtenido {detectados}"
    assert aciertos / ventanas > 0.85
    for p, (_, a, b) in zip(plays, verdad):
        assert abs(p.first_window_s - a) < 12.0, (p.first_window_s, a)
    print("\nOK: secuencia de pases correcta")


def test_rejects_unknown_audio() -> None:
    tracks = {i: synth_track(2000 + i) for i in range(1, 11)}
    index = build_index(tracks)
    desconocido = degrade(synth_track(9999, 30.0), snr_db=8.0)
    policy = DecisionPolicy()
    falsos = 0
    w, h = int(10 * SR), int(5 * SR)
    n = 0
    for start in range(0, len(desconocido) - w + 1, h):
        qh, qt = fingerprint_arrays(desconocido[start : start + w], QUERY_CONFIG)
        res = match(qh, qt, index, query_start_s=start / SR)
        n += 1
        if res and policy.decide(res[0]) == "accept":
            falsos += 1
            print("  FALSO POSITIVO:", res[0])
    print(f"falsos positivos sobre audio desconocido: {falsos}/{n}")
    assert falsos == 0


# ---------------------------------------------------------------------------
# Variantes de velocidad (`cli index --robust`)
# ---------------------------------------------------------------------------


def _escribir_wav(path: str, pcm: np.ndarray, sr: int) -> None:
    from scipy.io import wavfile

    wavfile.write(path, sr, (np.clip(pcm, -1, 1) * 32767).astype(np.int16))


def test_speed_variants() -> None:
    """Las variantes de ``--robust`` deben ser de +-4 %, no de un multiplo de la tasa.

    Hubo un momento en que ``asetrate`` recibia el audio a la tasa nativa del
    archivo y lo estiraba ~5,5x, dejando las variantes inservibles sin ningun
    error visible. Esta prueba fija el contrato en segundos, que es lo
    observable, y ademas verifica que sirvan para lo unico que existen:
    reconocer una emisora que altera la velocidad.
    """
    import shutil
    import tempfile

    from scipy import signal as sps

    from fonoscan.capture import decode_file
    from fonoscan.catalog import ROBUST_SPEEDS

    if shutil.which("ffmpeg") is None:
        print("ffmpeg no disponible: prueba de variantes omitida")
        return

    SR_FUENTE, DUR = 44100, 40.0
    pcm44 = sps.resample_poly(synth_track(4242, DUR), SR_FUENTE, SR).astype(np.float32)

    tmp = tempfile.mkdtemp()
    ruta = os.path.join(tmp, "ref.wav")
    try:
        _escribir_wav(ruta, pcm44, SR_FUENTE)

        # 1. La duracion de cada variante es la que promete la documentacion.
        for speed in ROBUST_SPEEDS:
            obtenida = len(decode_file(ruta, SR, speed=speed)) / SR
            esperada = DUR / speed
            assert abs(obtenida - esperada) < 0.05, (
                f"speed={speed}: {obtenida:.2f}s, se esperaba {esperada:.2f}s "
                f"(factor {obtenida / esperada:.2f}x)"
            )
        print(f"duraciones de las {len(ROBUST_SPEEDS)} variantes: correctas")

        # 2. Una emisora 4 % rapida no se reconoce sin variantes, y si con ellas.
        #    La consulta se genera por otro camino (resample_poly) a proposito:
        #    si se generara con decode_file la prueba seria circular.
        base = decode_file(ruta, SR)
        emitida = degrade(sps.resample_poly(base, 100, 104).astype(np.float32))
        consulta = emitida[int(5 * SR) : int(15 * SR)]
        qh, qt = fingerprint_arrays(consulta, QUERY_CONFIG)
        policy = DecisionPolicy()

        solo_1x = MemoryIndex.build(
            [(*fingerprint_arrays(base, REFERENCE_CONFIG),
              Reference(ref_id=1, track_id=1, speed=1.0, duration_s=len(base) / SR))],
            config_id=REFERENCE_CONFIG.fingerprint_id(),
        )
        res = match(qh, qt, solo_1x, query_start_s=0.0)
        sin_variantes = bool(res) and policy.decide(res[0]) == "accept"

        entradas, rid = [], 0
        for speed in ROBUST_SPEEDS:
            rid += 1
            v = decode_file(ruta, SR, speed=speed)
            entradas.append((*fingerprint_arrays(v, REFERENCE_CONFIG),
                             Reference(ref_id=rid, track_id=1, speed=speed,
                                       duration_s=len(v) / SR)))
        robusto = MemoryIndex.build(entradas, config_id=REFERENCE_CONFIG.fingerprint_id())
        res_r = match(qh, qt, robusto, query_start_s=0.0)
        con_variantes = bool(res_r) and policy.decide(res_r[0]) == "accept"

        est_sin = "reconoce" if sin_variantes else "NO reconoce"
        est_con = "reconoce" if con_variantes else "NO reconoce"
        print(f"emisora 4 % rapida | solo 1.0x: {est_sin} | --robust: {est_con}")
        if res_r:
            ref = robusto.references[res_r[0].ref_id]
            print(f"  variante que resolvio el match: speed={ref.speed}, "
                  f"score={res_r[0].score}, margen={res_r[0].margin:.2f}")

        assert con_variantes, "con --robust deberia reconocerse la emision acelerada"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_recognition_end_to_end()
    print()
    test_rejects_unknown_audio()
    print()
    test_speed_variants()
    print("\nTodas las pruebas pasaron.")
