# fonoscan — contexto del proyecto

Sistema de reconocimiento de fonogramas en flujos de audio para una **entidad de
gestión colectiva**. El resultado alimenta la liquidación de derechos conexos.

## Regla número uno

**Un falso positivo transfiere dinero de un titular a otro. Un falso negativo
sólo deja un pase sin cobrar.** Los errores no son simétricos. Ante la duda,
preferí siempre no informar un pase antes que informar uno dudoso. Si una
optimización mejora el recall a costa de la precisión, avisame antes de
aplicarla en lugar de decidirlo vos.

## Stack

- Python 3.11+, numpy, scipy. Sin dependencias pesadas en el núcleo.
- ffmpeg para toda decodificación y captura (nunca usar pydub, librosa ni
  audioread para esto: son más lentos y agregan dependencias).
- FastAPI + uvicorn para la API.
- PostgreSQL 16 para catálogo, pases y auditoría.
- Docker Compose para el despliegue de referencia.

## Restricciones de licenciamiento — no negociables

**Prohibido incorporar código AGPL-3** (Olaf, Panako, Gaborator, jgaborator).
La entidad ofrece un portal en red a sus asociados y eso dispara la obligación
de liberar el código fuente. Si una biblioteca parece útil, verificá la licencia
antes de proponerla y decímelo explícitamente.

Licencias aceptables: MIT, BSD, Apache-2.0, ISC, LGPL (enlace dinámico).

## Arquitectura

```
capture.py      ffmpeg → PCM 8 kHz mono (+ evidencia Opus en paralelo)
fingerprint.py  espectrograma → picos → pares (f1,f2,Δt) → hash uint32
index.py        índice invertido hash → (ref_id, t). Memoria (numpy) o PostgreSQL
matcher.py      histograma de offsets → score, coverage, margin, z → accept/review/reject
aggregator.py   ventanas → pases consolidados (inicio, fin, cobertura)
recognizer.py   worker en vivo + reproceso offline (MISMO código)
catalog.py      alta de fonogramas, ISRC, variantes de velocidad, duplicados
reporting.py    pases → reportes de uso ponderados
```

Documentación de diseño en `docs/`. Leela antes de proponer cambios
estructurales: `01-arquitectura.md`, `02-comparativa-fingerprinting.md`,
`03-plan-implementacion.md`, `04-operacion-y-calidad.md`.

## Invariantes que no se rompen

1. **El pipeline en vivo y el offline comparten el mismo código de decisión.**
   Cualquier pase informado tiene que poder reproducirse desde la evidencia
   archivada. Si tocás uno, tocá el otro o refactorizá a código común.

2. **Las detecciones no se editan en el lugar.** Una corrección genera una fila
   nueva en `play` con `supersede_id`. El histórico es la base probatoria del
   reparto.

3. **`config_id` versiona la compatibilidad de los hashes.** Si cambiás
   `n_fft`, `hop_length`, `sample_rate`, `max_dt_frames` o la cuantización, el
   índice existente queda inválido y hay que re-indexar todo el catálogo. Esto
   es caro: avisá antes de proponerlo y explicá qué se gana.

4. **La densidad de picos NO afecta la compatibilidad.** `REFERENCE_CONFIG` y
   `QUERY_CONFIG` difieren a propósito y deben seguir siendo intercambiables.

5. **Cuatro métricas de confianza, no una.** score, coverage, margin, z. No
   simplifiques la política de decisión a un solo umbral.

## Estilo de código

- Nombres de dominio en español (pase, fonograma, emisión, titular), nombres
  técnicos en inglés (hash, landmark, index, offset). Es la convención ya usada.
- Docstrings que expliquen **por qué**, no qué. El qué se lee en el código.
- Vectorizar con numpy. Un bucle Python sobre landmarks individuales en el
  camino caliente es un bug de rendimiento.
- Type hints en las firmas públicas.
- Sin `print()` en módulos: usar `logging`.

## Pruebas

```bash
python tests/test_end_to_end.py
```

La prueba usa audio sintético con cadena de emisión simulada (pasabanda
200–3400 Hz, limitador, SNR 8 dB). Umbrales actuales que hay que mantener:

- ≥ 85 % de ventanas correctamente clasificadas
- secuencia de pases exacta (mismos fonogramas, mismo orden)
- **cero** falsos positivos sobre audio fuera de catálogo

Si un cambio baja alguno de estos, no lo mergees: mostrame los números.

## Rendimiento de referencia (un núcleo)

| Métrica | Valor actual |
|---|---|
| Indexación | ~800× tiempo real |
| Densidad de referencia | 104 landmarks/s |
| Memoria del índice | 4,5 MB por hora de catálogo |
| Extracción de consulta (10 s) | 22 ms |
| Consulta contra el índice | 5,5 ms |

Si tocás el camino caliente, medí antes y después y reportá ambos números.

## Comandos habituales

```bash
python -m fonoscan.cli index    --catalog catalogo.csv --out index.npz
python -m fonoscan.cli audit    --index index.npz
python -m fonoscan.cli monitor  --index index.npz --channel FM100 --url http://...
python -m fonoscan.cli offline  --index index.npz --file grabacion.mp3
python -m fonoscan.cli identify --index index.npz --file fragmento.wav
python -m fonoscan.cli report   --plays plays.jsonl --catalog catalogo.csv \
                                --from 2026-09-01 --to 2026-09-30
```

## Qué NO hacer

- No agregar dependencias sin justificar el costo operativo.
- No convertir la política de decisión en un modelo entrenado sin conservar la
  explicabilidad de la decisión (hay que poder defenderla ante un reclamo).
- No optimizar el reconocimiento prematuramente: el cuello de botella real en
  producción es el ancho de banda de los streams, no la CPU.
- No tocar el esquema de `play`, `evidence` ni `audit_log` sin migración y sin
  avisar: son las tablas que sostienen la auditoría del reparto.
