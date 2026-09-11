# EXPLAIN — qué hace fonoscan

Resumen del sistema a partir de `CLAUDE.md` y los documentos de `docs/`.

## Qué es

**fonoscan** es un sistema de monitoreo de radiodifusión para una entidad de
gestión colectiva. Escucha canales 24×7 (FM, AM, TV, streams online, SDR) e
identifica qué **fonogramas** —grabaciones concretas, no obras ni
composiciones— se emitieron, en qué canal y entre qué horas. Esa salida
alimenta la liquidación de derechos conexos: reemplaza (o audita) las planillas
que hoy declaran los usuarios de música.

La diferencia con "un Shazam" está en tres puntos que atraviesan todo el
diseño:

1. **La unidad de salida es el pase, no la consulta.** No "qué tema es éste"
   sino "qué sonó, desde cuándo, hasta cuándo, con qué cobertura".
2. **Todo tiene que ser reproducible.** Cada pase informado debe poder
   reconstruirse desde la evidencia archivada, con la versión del motor, del
   índice y los umbrales de ese momento — porque un socio o una emisora puede
   impugnarlo.
3. **Los errores no son simétricos.** Un falso positivo transfiere plata de un
   titular a otro; un falso negativo sólo deja un pase sin cobrar. Los umbrales
   están corridos hacia la precisión a propósito.

## Cómo funciona el pipeline

```
ffmpeg           un proceso por canal, dos salidas del mismo origen:
                 → PCM 8 kHz mono  (reconocimiento, FFT barata)
                 → Opus 48 kbps    (evidencia, segmentos de 5 min a S3/MinIO)
       ↓
fingerprint.py   espectrograma → picos locales (percentil 80 por banda)
                 → pares (f1, f2, Δt) → hash uint32. Banda útil 120–3400 Hz,
                 n_fft 1024 / hop 256. Landmarks tipo Wang (2003).
       ↓
index.py         índice invertido hash → (ref_id, t).
                 MemoryIndex (numpy + searchsorted, 12 B/landmark, .npz)
                 o PostgresIndex (particionado por hash, COPY masivo).
       ↓
matcher.py       histograma de offsets: si la consulta es esa referencia,
                 todos los landmarks comparten Δ = t_ref − t_consulta.
                 → score, coverage, margin, z → accept / review / reject
       ↓
aggregator.py    ventanas de 10 s (salto 5 s) → pases consolidados.
                 Invariante: mientras el mismo tema suena sin cortes, el
                 offset absoluto es constante. Clasifica full/partial/fragment.
       ↓
reporting.py     pases → reportes de uso (por_pase, por_duracion,
                 por_pase_completo, ponderada) + salida estilo DDEX.
```

`recognizer.py` corre tanto el worker en vivo como el reproceso offline **con
el mismo código de decisión** — no es elegancia, es el requisito de
reproducibilidad del punto 2.

## Las decisiones de diseño que más importan

**Cuatro métricas de confianza, no una.** `score` (landmarks alineados),
`coverage` (fracción de segundos de la consulta que aportan), `margin` (mejor
candidato contra el mejor de *otro* fonograma — el predictor más confiable de
falso positivo) y `z`. Las cuatro tienen que cumplirse simultáneamente para
aceptar. La zona `review` es deliberada: alimenta una cola humana en lugar de
forzar un binario.

**Densidad asimétrica.** La referencia se indexa a ~104 landmarks/s, la
consulta se extrae a ~300/s. Ahorra 3× de almacenamiento sin perder recall,
porque el match sólo necesita que un subconjunto de los hashes de la consulta
caiga en el índice. Los dos presets son intercambiables a propósito.

**Motor propio por licenciamiento.** Olaf y Panako son AGPL-3, y el portal
donde los socios consultan sus pases dispara la cláusula de uso en red. De ahí
la implementación propia, sin dependencias pesadas. La contramedida al punto
débil de los landmarks (no son invariantes a cambios de velocidad) es indexar
variantes ±2 % y ±4 % con `--robust`, activado sólo en los canales donde se
detecta el problema.

**Inmutabilidad.** Una corrección nunca edita una fila de `play`: genera una
nueva con `supersede_id`. Los reportes se sellan con SHA-256 y `frozen`; lo que
se corrige después sale como reporte de ajuste.

**`config_id` versiona la compatibilidad de hashes.** Si cambian `n_fft`,
`hop_length`, `sample_rate`, `max_dt_frames` o la cuantización, el índice queda
inválido y hay que re-indexar todo el catálogo. El worker se niega a arrancar
si su `config_id` no coincide con el del índice — prefiere caerse antes que
producir pases inválidos.

## Escala y estado

Medido en un núcleo: indexación ~800× tiempo real, 4,5 MB de índice por hora de
catálogo, extracción de consulta de 10 s en 22 ms, consulta al índice en
5,5 ms. Un canal consume ~0,15 vCPU. El cuello de botella real en producción no
es CPU sino ancho de banda y estabilidad de los streams; el costo dominante no
es el cómputo sino el almacenamiento de evidencia (~38 TB/año para 200
canales).

El escalado por catálogo está proyectado: hasta 250 k fonogramas entra en
`MemoryIndex` en un nodo; del millón para arriba, sharding por prefijo de hash
o PostgreSQL particionado — trivial porque el hash es uniforme y los
histogramas se suman.

El plan (`docs/03-plan-implementacion.md`) va de un piloto de 10 emisoras a la
integración con liquidación, con dos períodos completos corriendo en paralelo
antes de cambiar la base del reparto. El punto crítico está señalado con
honestidad: **el techo duro es la cobertura del catálogo con audio máster
disponible**, no el algoritmo. 80 000 ISRC con audio de sólo 30 000 rinde 37 %
de identificación como máximo.

## Dos cosas que conviene tener presentes

`docs/04-operacion-y-calidad.md` incluye una sección explícita de **qué no hace
el sistema**: no identifica obras (sólo grabaciones), no detecta covers ni
versiones en vivo distintas de la grabación, no identifica lo que no está
indexado, y no mide audiencia ni valor económico — mide difusión.

Y la prueba de `tests/test_end_to_end.py` usa audio sintético con cadena de
emisión simulada (pasabanda 200–3400 Hz, limitador, SNR 8 dB), con tres
umbrales que hay que mantener: ≥ 85 % de ventanas correctas, secuencia de pases
exacta, y **cero** falsos positivos sobre audio fuera de catálogo.
