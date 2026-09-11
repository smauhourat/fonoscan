"""Genera un catálogo sintético y una emisión simulada para la demo local.

Correr desde la raíz del repo:  python examples/generar.py

Regenera exactamente los mismos bytes (las semillas están fijas), así que el
audio de ``examples/audio/`` es reconstruible y no hace falta versionarlo.
(El MP3 lo codifica ffmpeg: ahí los bytes dependen de la versión de LAME.)
"""
import os
import shutil
import subprocess
import sys
import numpy as np
from scipy import signal as sps
from scipy.io import wavfile

SR = 44100
BASE = os.path.dirname(os.path.abspath(__file__))
AUD = os.path.join(BASE, "audio")
RNG = np.random.default_rng(20260911)

os.makedirs(AUD, exist_ok=True)


def synth_track(seed, duration_s):
    """Música sintética con estructura espectral rica (armónicos + percusión)."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * SR)
    t_out = np.zeros(n, dtype=np.float32)
    escala = np.array([220, 246.94, 261.63, 293.66, 329.63, 392.0, 440.0, 493.88])
    largo = 0.35 + rng.random() * 0.25
    for k in range(int(duration_s / largo)):
        f0 = rng.choice(escala) * rng.choice([1, 2, 4])
        ini = int(k * largo * SR)
        fin = min(ini + int(largo * 1.4 * SR), n)
        seg = np.arange(fin - ini) / SR
        env = np.exp(-3.0 * seg)
        voz = np.zeros(fin - ini, dtype=np.float32)
        for h in range(1, 7):
            voz += (0.9 / h) * np.sin(2 * np.pi * f0 * h * seg + rng.random() * 6.28)
        t_out[ini:fin] += (voz * env).astype(np.float32)
    for k in range(int(duration_s / 0.5)):          # percusión
        ini = int(k * 0.5 * SR)
        fin = min(ini + int(0.12 * SR), n)
        golpe = rng.standard_normal(fin - ini).astype(np.float32)
        golpe *= np.exp(-30 * np.arange(fin - ini) / SR)
        t_out[ini:fin] += 0.6 * golpe
    t_out += 0.01 * rng.standard_normal(n).astype(np.float32)
    return (t_out / (np.abs(t_out).max() + 1e-9)).astype(np.float32)


def locucion(seg):
    """Ruido con forma espectral de voz, para los pasajes hablados."""
    n = int(seg * SR)
    x = RNG.standard_normal(n).astype(np.float32)
    sos = sps.butter(4, [300, 3000], btype="band", fs=SR, output="sos")
    y = sps.sosfilt(sos, x).astype(np.float32)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * np.arange(n) / SR)   # sílabas
    return (0.5 * y * env).astype(np.float32)


def degradar(x, snr_db=8.0):
    """Cadena de emisión: pasabanda 200-3400, limitador, ruido a 8 dB de SNR."""
    sos = sps.butter(4, [200, 3400], btype="band", fs=SR, output="sos")
    y = sps.sosfilt(sos, x).astype(np.float32)
    y = np.tanh(2.5 * y).astype(np.float32)
    ruido = RNG.standard_normal(len(y)).astype(np.float32)
    esc = np.sqrt(np.mean(y ** 2) / (np.mean(ruido ** 2) * 10 ** (snr_db / 10)))
    return (y + esc * ruido).astype(np.float32)


def escribir(path, x):
    wavfile.write(path, SR, (np.clip(x, -1, 1) * 32767).astype(np.int16))


# --- Catálogo: 10 fonogramas indexados + 2 que NO se indexan --------------
CATALOGO = [
    (1, "Milonga del puerto",   "Dúo Sur",            "Sello Austral", 168.0),
    (2, "Viento norte",         "Los Algarrobos",     "Sello Austral", 142.0),
    (3, "Zamba de la espera",   "Ana Quiroga",        "Discos Litoral", 195.0),
    (4, "Tren de las seis",     "Cuarteto Bermejo",   "Discos Litoral", 156.0),
    (5, "Luz de enero",         "Marta Vidal",        "Sello Austral", 128.0),
    (6, "Calle sin nombre",     "Los Algarrobos",     "Independiente",  174.0),
    (7, "Cortina institucional","Estudio Central",    "Independiente",   22.0),
    (8, "Río marrón",           "Ana Quiroga",        "Discos Litoral", 187.0),
    (9, "Chacarera del olvido", "Dúo Sur",            "Sello Austral", 163.0),
    (10, "Nocturno 4 AM",       "Marta Vidal",        "Independiente",  149.0),
]
NO_CATALOGO = [(101, 95.0), (102, 70.0)]

pistas = {}
for tid, titulo, artista, sello, dur in CATALOGO:
    pcm = synth_track(2000 + tid, dur)
    pistas[tid] = pcm
    escribir(os.path.join(AUD, f"track{tid:02d}.wav"), pcm)
for tid, dur in NO_CATALOGO:
    pistas[tid] = synth_track(9000 + tid, dur)

with open(os.path.join(BASE, "catalogo.csv"), "w", encoding="utf-8", newline="") as fh:
    fh.write("isrc,title,artist,label,album,year,audio_path,rights_owner_id,territory\n")
    for tid, titulo, artista, sello, dur in CATALOGO:
        isrc = f"ARZZZ26{tid:05d}"
        # Ruta relativa a la raíz del repo: los comandos del CLI se corren desde ahí.
        ruta = f"examples/audio/track{tid:02d}.wav"
        fh.write(f"{isrc},{titulo},{artista},{sello},Antología {tid},2026,{ruta},TIT-{tid:03d},AR\n")

with open(os.path.join(BASE, "canales.csv"), "w", encoding="utf-8", newline="") as fh:
    fh.write("channel_id,name,medium,weight\n")
    fh.write("FM100,FM Cien,radio,1.0\n")

# --- Emisión simulada -----------------------------------------------------
# (track_id, desde_s, duración_s). track 4 aparece dos veces: son DOS pases.
PROGRAMA = [
    (4,   20.0, 100.0),
    (9,    0.0, 150.0),
    (101,  5.0,  60.0),   # fuera de catálogo
    (2,   30.0,  75.0),
    (4,   10.0,  60.0),   # segundo pase del mismo fonograma
    (7,    2.0,  12.0),   # cortina -> fragmento
    (102,  0.0,  45.0),   # fuera de catálogo
    (10,   0.0, 130.0),
]

bloques, guia, reloj = [], [], 0.0
for tid, desde, dur in PROGRAMA:
    hab = locucion(3.5)
    bloques.append(hab); reloj += len(hab) / SR
    ini = int(desde * SR)
    seg = pistas[tid][ini:ini + int(dur * SR)]
    guia.append((tid, round(reloj, 1), round(reloj + len(seg) / SR, 1)))
    bloques.append(seg); reloj += len(seg) / SR

emision = degradar(np.concatenate(bloques))
escribir(os.path.join(BASE, "emision.wav"), emision)
escribir(os.path.join(BASE, "fragmento.wav"), emision[int(200 * SR):int(215 * SR)])

with open(os.path.join(BASE, "verdad.txt"), "w", encoding="utf-8") as fh:
    for tid, a, b in guia:
        marca = "FUERA DE CATÁLOGO" if tid > 100 else f"track {tid}"
        fh.write(f"{a:8.1f} - {b:8.1f} s  {marca}\n")

# El MP3 existe para que la demo ejercite la decodificación de ffmpeg, que es
# el camino real: en producción nunca llega un WAV.
mp3 = os.path.join(BASE, "grabacion.mp3")
subprocess.run(
    [shutil.which("ffmpeg") or "ffmpeg", "-y", "-v", "error",
     "-i", os.path.join(BASE, "emision.wav"),
     "-c:a", "libmp3lame", "-b:a", "128k", mp3],
    check=True,
)

print(f"emisión: {len(emision)/SR/60:.1f} min, {len(PROGRAMA)} bloques")
print(f"catálogo: {len(CATALOGO)} fonogramas indexables, {len(NO_CATALOGO)} fuera de catálogo")
