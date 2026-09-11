# examples — demo reproducible de punta a punta

Catálogo sintético y emisión simulada para ejercitar el ciclo completo
(índice → reconocimiento → reporte) **sin Docker y sin audio con derechos**.

No reemplaza a `tests/test_end_to_end.py`, que es la prueba con umbrales. Esto
es material de demostración y de exploración manual.

## Contenido

| Archivo | Qué es |
|---|---|
| `generar.py` | Genera todo lo demás (~45 s). Semillas fijas: reproduce los mismos bytes |
| `catalogo.csv` | 10 fonogramas con ISRC válido, titular y territorio |
| `canales.csv` | Un canal (`FM100`, radio, peso 1,0) |
| `audio/track01..10.wav` | Los fonogramas de referencia (44,1 kHz) |
| `emision.wav` | 11 min de emisión simulada, ya degradada |
| `grabacion.mp3` | Lo mismo en MP3 128 kbps, para ejercitar la decodificación |
| `fragmento.wav` | 15 s sueltos, para `cli identify` |
| `verdad.txt` | Verdad de referencia: qué suena y entre qué segundos |

La emisión pasa por la misma cadena que la prueba del repo: pasabanda
200–3400 Hz, limitador y ruido a 8 dB de SNR, más pasajes de locución entre
bloques.

Incluye a propósito dos casos que importan:

- **el mismo fonograma dos veces** (track 4, en 3,5–103,5 s y en 402,5–462,5 s),
  que deben salir como **dos pases** y no como uno;
- **105 s de audio fuera de catálogo**, para observar el comportamiento frente
  a lo que el índice no conoce.

## Correr el ciclo

Desde la raíz del repo (las rutas de `catalogo.csv` son relativas a ahí):

```bash
python examples/generar.py                      # el audio no se versiona: generarlo primero

python -m fonoscan.cli index    --catalog examples/catalogo.csv --out examples/index.npz
python -m fonoscan.cli audit    --index examples/index.npz
python -m fonoscan.cli identify --index examples/index.npz --file examples/fragmento.wav
python -m fonoscan.cli offline  --index examples/index.npz --file examples/grabacion.mp3 \
                                --channel FM100 --epoch 2026-09-10T08:00:00 \
                                --out examples/pases.jsonl
python -m fonoscan.cli report   --plays examples/pases.jsonl --catalog examples/catalogo.csv \
                                --channels examples/canales.csv \
                                --from 2026-09-10 --to 2026-09-10
```

Requisitos: `numpy`, `scipy` y `ffmpeg` en el PATH. Nada más — ni PostgreSQL,
ni Redis, ni object storage.

El audio, el índice y los pases están en `.gitignore`: pesan 193 MB y se
reconstruyen con el primer comando. Los WAV salen bit a bit iguales en
cualquier máquina; `grabacion.mp3` depende de la versión de LAME que traiga
ffmpeg.

## Qué se espera ver

Índice de 155.104 landmarks (0,6 MB en disco, 1,9 MB en RAM), y los **6 pases
reales detectados en el orden correcto**, con los dos pases del track 4
separados y la cortina de 12 s clasificada como tal. Los bordes caen dentro del
±5 s que predice `docs/01-arquitectura.md` para ventana de 10 s y salto de 5 s.

`identify` sobre `fragmento.wav` devuelve "Chacarera del olvido" en la posición
92,99 s; la verdad de referencia dice 93,0 s.

Correr `offline` dos veces sobre el mismo archivo da un JSONL **byte a byte
idéntico**: es el invariante de reproducibilidad del que depende la defensa de
un pase impugnado.

## Limitación conocida de este material

Las 10 pistas salen del mismo generador, con la misma escala de 8 notas y la
misma grilla de percusión a 0,5 s, así que se parecen entre sí mucho más que
dos grabaciones reales. Eso se nota:

- `cli audit` marca 5 pares de fonogramas no relacionados con ~520–560 hashes
  compartidos;
- sobre los 105 s fuera de catálogo el agregador emite 6 pases espurios.

Los 6 salen con `needs_review = true` y el reporte los deja en la columna
`plays_in_review` con 0 segundos y 0 unidades ponderadas — es decir, **no
llegan al reparto**, que es exactamente lo que la política de cuatro métricas
debe garantizar. Pero la proporción de fragmentos (6 de 12) queda por encima
del 25 % que `docs/04-operacion-y-calidad.md` fija como umbral de alerta.

Es un artefacto del audio sintético, no una medición del motor. Para cualquier
conclusión sobre calidad, la referencia es `tests/test_end_to_end.py` y, en
serio, un conjunto de validación propio con emisoras reales (procedimiento en
`docs/04-operacion-y-calidad.md`, sección 2).
