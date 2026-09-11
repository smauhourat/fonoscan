# 01 — Arquitectura

## 1. Qué problema resuelve exactamente

Identificar **fonogramas** (grabaciones concretas, no obras ni composiciones) en
flujos de audio difundidos, con precisión y trazabilidad suficientes para que
el resultado sostenga un reparto de derechos conexos.

Esto impone tres restricciones que no aparecen en un "Shazam para el celular":

1. **Continuidad, no consultas.** El sistema escucha 24×7 cientos de canales.
   La unidad de salida no es "qué tema es éste" sino "qué se emitió, desde qué
   hora, hasta qué hora, en qué canal".
2. **Trazabilidad.** Cada pase informado tiene que poder reconstruirse: audio
   de evidencia, versión del motor, versión del índice, umbrales aplicados.
   Un socio o un usuario de música puede impugnar el reparto.
3. **Sesgo hacia la precisión.** Un falso positivo transfiere dinero de un
   titular a otro. Un falso negativo sólo lo deja sin cobrar ese pase. No son
   errores simétricos y los umbrales deben reflejarlo.

## 2. Vista general

```
  FUENTES                CAPTURA              RECONOCIMIENTO           CONSOLIDACIÓN
┌──────────┐         ┌─────────────┐        ┌────────────────┐       ┌──────────────┐
│ Icecast  │         │             │ PCM 8k │ StreamFinger-  │ land- │              │
│ HLS/RTMP ├────────►│   ffmpeg    ├───────►│   printer      ├──────►│   matcher    │
│ UDP/TS   │         │  (1 proc.   │        │ (landmarks     │ marks │ (histograma  │
│ RTL-SDR  │         │   por canal)│        │  incrementales)│       │  de offsets) │
│ línea    │         │             │        └────────────────┘       └──────┬───────┘
└──────────┘         └──────┬──────┘                                        │
                            │                        ┌───────────────┐      │
                            │ Opus 48k               │ índice        │◄─────┘
                            ▼                        │ invertido     │
                     ┌─────────────┐                 │ hash→(ref,t)  │
                     │ EVIDENCIA   │                 └───────▲───────┘
                     │ S3 / MinIO  │                         │
                     │ segmentos   │                 ┌───────┴───────┐
                     │ de 5 min    │                 │  CATÁLOGO     │
                     └─────────────┘                 │  fonogramas   │
                                                     │  + ISRC       │
                                                     └───────────────┘
                            │                                │
                            ▼                                ▼
                     ┌──────────────────────────────────────────────┐
                     │             PlayAggregator                    │
                     │  ventanas → pases (inicio, fin, cobertura)    │
                     └───────────────────────┬───────────────────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    ▼                        ▼                        ▼
            ┌──────────────┐        ┌─────────────────┐      ┌────────────────┐
            │ PostgreSQL   │        │ cola de revisión│      │ reportes de uso│
            │ play, track  │        │   humana        │      │ CSV / DDEX-like│
            │ evidence     │        └─────────────────┘      └────────┬───────┘
            └──────────────┘                                          │
                                                                      ▼
                                                        ┌──────────────────────┐
                                                        │ sistema de socios    │
                                                        │ y liquidación        │
                                                        └──────────────────────┘
```

## 3. Componentes

### 3.1 Capa de captura (`fonoscan/capture.py`)

Un proceso ffmpeg por canal, con dos salidas independientes del mismo origen:

| Salida | Formato | Destino | Por qué |
|---|---|---|---|
| Reconocimiento | PCM f32 mono 8 kHz | tubería al worker | Mínimo costo de FFT; 8 kHz cubre la banda donde los landmarks son estables en AM, FM y TV |
| Evidencia | Opus 48 kbps mono 16 kHz | segmentos de 5 min en object storage | ~21 MB por canal por día; barato y suficiente para escucha humana y para reproceso |

Reconexión con backoff exponencial (5 s → 120 s). Cada sesión de captura se
registra en `capture_session` con segundos capturados y desconexiones: sin ese
registro no se puede distinguir "la radio no pasó música" de "el capturador
estuvo caído", y esa diferencia es exactamente lo que se discute cuando un
socio reclama.

**Fuentes soportadas.** Todo lo que abra ffmpeg: streaming online (Icecast,
SHOUTcast, HLS, DASH), contribución (RTMP, RTSP, SRT), MPEG-TS multicast desde
un receptor de TDT o satélite con salida IP, entrada de línea desde receptores
de FM/AM, y `rtl_fm` para dongles RTL-SDR cuando hay que monitorear emisoras
que no tienen stream online. Para radios sin presencia en internet — habituales
en el interior — la opción práctica es un nodo de captura local de bajo costo
(una SBC con un dongle SDR) que sube evidencia y corre el reconocimiento en el
borde contra un índice reducido, o sube audio comprimido al centro.

### 3.2 Motor de huellas (`fonoscan/fingerprint.py`)

Landmarks tipo Wang (2003): espectrograma → picos locales → pares
(ancla, objetivo) → hash de 32 bits.

Parámetros elegidos y por qué:

| Parámetro | Valor | Razón |
|---|---|---|
| `sample_rate` | 8000 Hz | La energía discriminante en cadenas de emisión comprimidas está bajo 4 kHz; bajar de 44.1 a 8 kHz reduce ~6× el costo |
| `n_fft` / `hop` | 1024 / 256 | Ventana de 128 ms, 31.25 tramas/s. Compromiso estándar entre resolución frecuencial (7.8 Hz) y temporal |
| Banda útil | 120–3400 Hz | Corta rumble y el techo de los códecs de baja tasa |
| Umbral de picos | percentil 80 **por banda** | Impide que una locución fuerte en medios acapare todos los picos |
| `max_dt` | 63 tramas (~2 s) | Zona objetivo larga = más robustez, pero más hashes; 2 s es el punto habitual |
| Cuantización | 9 bits f1, 9 bits f2, 14 bits Δt | Cabe en 32 bits; tolera ±1 bin de corrimiento |

**Densidad asimétrica.** La referencia se indexa a ~104 landmarks/s y la
consulta se extrae a ~300/s. Ahorra 3× de almacenamiento sin perder recall,
porque el match sólo necesita que un subconjunto de los hashes de la consulta
caiga en el índice. Presets: `REFERENCE_CONFIG` y `QUERY_CONFIG`.

**Extracción incremental.** `StreamFingerprinter` procesa bloques y mantiene un
solape igual a `max_dt`, de modo que no se pierden los pares que cruzan el
límite entre bloques y no se recomputa la FFT del audio ya visto.

### 3.3 Índice invertido (`fonoscan/index.py`)

Dos implementaciones intercambiables tras la misma interfaz:

- **`MemoryIndex`** — arreglos numpy ordenados + `searchsorted` vectorizado.
  12 bytes por landmark (hash `uint32` + ref `int32` + tiempo `int32`).
  Se serializa a `.npz`, se distribuye a los workers y se carga desde tmpfs.
- **`PostgresIndex`** — tabla `fp_landmark` particionada por hash en 32
  particiones, carga masiva con `COPY`, consulta con `hash = ANY(...)`.

**Dimensionamiento medido** (en el contenedor de prueba, audio sintético):

| Magnitud | Valor medido |
|---|---|
| Indexación | ~800× tiempo real, un núcleo |
| Densidad de referencia | 104 landmarks/s de audio |
| Memoria del índice | **4,5 MB por hora** de catálogo (~0,26 MB por fonograma de 3:30) |
| Extracción de consulta (10 s) | 22 ms |
| Consulta contra el índice | 5,5 ms con 0,5 M landmarks |

Proyección:

| Catálogo | Landmarks | RAM del índice | Estrategia |
|---|---|---|---|
| 50 000 fonogramas | 1,8 G | ~13 GB | `MemoryIndex` en un nodo |
| 250 000 | 9,1 G | ~65 GB | `MemoryIndex` en nodo grande, o 4 shards |
| 1 000 000 | 36 G | ~260 GB | Sharding por prefijo de hash (8–16 shards) o PostgreSQL |
| 5 000 000+ | 180 G | ~1,3 TB | PostgreSQL particionado + caché de hashes calientes, o motor comercial |

El sharding es trivial porque el hash es uniforme: se reparte por los bits
altos y cada shard responde su porción; el agregador suma los histogramas. La
alternativa a explorar cuando el catálogo se pasa del millón es la segunda
etapa neural (ver documento 02), que reduce el índice a vectores en FAISS.

### 3.4 Decisión (`fonoscan/matcher.py`)

Histograma de offsets: si la consulta corresponde a la referencia, todos los
landmarks coincidentes comparten el mismo `Δ = t_referencia − t_consulta`.
Las coincidencias casuales se dispersan.

Se calculan cuatro métricas, no una:

- `score` — landmarks alineados (±1 trama).
- `coverage` — fracción de segundos de la consulta que aportan al menos un
  landmark alineado. Detecta el match concentrado en un solo golpe compartido.
- `margin` — cociente entre el mejor candidato y el mejor de **otro**
  fonograma. Es el predictor más confiable de falso positivo.
- `z` — desvíos sobre el conteo esperado bajo hipótesis nula.

La política `DecisionPolicy` devuelve `accept` / `review` / `reject`. La zona
`review` es deliberada: alimenta la cola humana en lugar de forzar un binario.

### 3.5 Consolidación (`fonoscan/aggregator.py`)

El invariante que hace el trabajo: mientras el mismo fonograma siga sonando sin
cortes, el offset absoluto `t_stream − t_fonograma` se mantiene constante.
Cambia cuando empieza otro tema, cuando el mismo tema se repite más tarde (dos
pases, se liquidan como dos) o cuando hay un salto de edición.

Salida por pase: inicio, fin, segundos detectados, cobertura de la obra y
clasificación `full` / `partial` / `fragment`. La distinción importa: un
fragmento de 6 segundos es una cortina o un bumper, no una difusión completa, y
muchas reglas de reparto los tratan distinto.

**Granularidad temporal.** Con ventana de 10 s y salto de 5 s, los bordes del
pase tienen una incertidumbre de ±5 s y la duración tiende a sobreestimarse en
hasta una ventana. Si el reparto es por duración y esa incertidumbre importa,
se refina el borde con una segunda pasada de salto 1 s sobre la evidencia
archivada, sólo en los ~20 s alrededor de cada transición.

### 3.6 Servicio y reporte

`fonoscan/api.py` expone identificación puntual, consulta de pases con filtro
por ISRC y período, y exportación de reportes. `fonoscan/reporting.py`
implementa cuatro ponderaciones (`por_pase`, `por_duracion`,
`por_pase_completo`, `ponderada`) y la salida estilo DDEX en JSON.

## 4. Topología de despliegue

### Piloto (hasta ~30 canales)

Un servidor: 16 vCPU, 64 GB RAM, 2 TB SSD. `docker-compose.yml` tal cual.
Todos los workers, la API, PostgreSQL y MinIO en la misma máquina.

### Producción (100–1000 canales)

```
  ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
  │ nodo captura 1│   │ nodo captura 2│   │ nodo captura N│   4–8 vCPU, 8 GB
  │ 20–40 canales │   │ 20–40 canales │   │               │   c/u, índice en
  └───────┬───────┘   └───────┬───────┘   └───────┬───────┘   tmpfs
          └───────────────────┼───────────────────┘
                              ▼
                   ┌──────────────────────┐
                   │  bus (NATS/Kafka)    │  pases + métricas
                   └──────────┬───────────┘
                              ▼
              ┌───────────────────────────────┐
              │ PostgreSQL (primario+réplica) │
              │ play particionado por mes     │
              └───────────────┬───────────────┘
                              ▼
              ┌───────────────────────────────┐      ┌──────────────┐
              │ API + backoffice de revisión  │◄────►│ S3 evidencia │
              └───────────────────────────────┘      └──────────────┘
```

**Costo por canal.** Un canal consume ~0,15 vCPU en reconocimiento continuo
(22 ms de extracción + 6 ms de consulta cada 5 s de audio, más la decodificación
de ffmpeg). Un nodo de 4 vCPU sostiene cómodamente 20–25 canales con margen
para picos y reconexiones. El cuello de botella real no es CPU sino ancho de
banda y estabilidad de los streams de origen.

**Almacenamiento.**

| Concepto | Por canal por día | 200 canales por año |
|---|---|---|
| Evidencia Opus 48 kbps | 518 MB | ~38 TB |
| Filas `play` (≈350/día) | ~0,2 MB | ~15 GB |

La evidencia domina. Política sugerida: 90 días en almacenamiento caliente,
luego reducción a 24 kbps y archivo frío por el plazo de prescripción de
reclamos aplicable, que hay que definir con el área legal de la entidad.

## 5. Modos de falla y cómo se manejan

| Falla | Detección | Respuesta |
|---|---|---|
| Stream caído | `capture_session.disconnections`, sin bytes por N segundos | Backoff exponencial; alerta si supera 15 min; el período queda marcado como no monitoreado y **no** se informa como "sin música" |
| Emisora cambia URL | Caída permanente de un canal | Alerta al operador; hay que mantener un inventario de URLs y revalidarlo |
| Índice desactualizado | `config_id` del worker distinto al del índice | El worker se niega a arrancar antes que producir pases inválidos |
| Deriva de reloj del encoder | El offset se corre lentamente dentro de un pase | Promedio móvil del offset en el agregador (factor 0,8/0,2) |
| Cambio de velocidad de la emisora | Caída brusca de recall en un canal | Indexar variantes ±2 % y ±4 % (`--robust`), o derivar ese canal al motor neural |
| Catálogo con duplicados | `cli audit` detecta pares con miles de hashes compartidos | Resolver antes de liquidar; ver documento 04 |
| Falso positivo sistemático | Margen bajo recurrente sobre un mismo par | Cola de revisión + lista de pares conflictivos conocidos |

## 6. Seguridad y cumplimiento

- **Aislamiento por rol.** `operador`, `auditor`, `socio`, `admin`. El rol
  `socio` sólo ve pases de fonogramas de los que es titular.
- **Inmutabilidad de la detección.** Una corrección genera una fila nueva con
  `supersede_id`; nunca se edita el original. `audit_log` registra actor,
  acción y payload.
- **Congelamiento de reportes.** `usage_report.frozen` + hash SHA-256 del
  contenido. Si después se corrige una detección, se emite un reporte de
  ajuste; no se reescribe el sellado.
- **Datos personales.** La evidencia de audio de radio y TV contiene voz de
  locutores y, en programas con llamadas, de terceros. Definir con el área
  legal el plazo de retención y el título habilitante del tratamiento antes de
  encender la grabación permanente.
- **Licenciamiento de terceros.** El motor incluido no usa código AGPL. Si se
  decide integrar Panako u Olaf, ambos son AGPL-3: al ofrecer un portal en red
  a los socios se dispara la obligación de poner el código fuente a
  disposición. Ver documento 02.
