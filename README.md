# fonoscan

Sistema de reconocimiento de fonogramas en tiempo real a partir de flujos de
audio, para el monitoreo de difusión y la liquidación de derechos conexos por
parte de una entidad de gestión colectiva.

Identifica **grabaciones concretas** (no obras) en radio, TV, streaming y música
ambiental, las consolida en **pases** con hora de inicio y fin, y produce
**reportes de uso** listos para el sistema de reparto, con evidencia de audio
archivada y trazabilidad completa de cada decisión.

---

## Instalación rápida

```bash
# Requisitos: Python 3.11+, ffmpeg
sudo apt install ffmpeg
pip install -r requirements.txt

# Verificar que el motor funciona (prueba end-to-end con audio sintético)
python tests/test_end_to_end.py
```

## Uso en cinco pasos

### 1. Preparar el catálogo

CSV con una fila por fonograma:

```csv
isrc,title,artist,label,album,year,audio_path,rights_owner_id,territory
AR-ZZZ-26-00001,Tema uno,Artista A,Sello Demo,Album,2026,/audio/001.flac,OWN1,AR
AR-ZZZ-26-00002,Tema dos,Artista B,Sello Demo,Album,2026,/audio/002.flac,OWN2,AR
```

El ISRC se normaliza y valida automáticamente (`AR-ZZZ-26-00001` → `ARZZZ2600001`).
Las filas con ISRC inválido se indexan igual y quedan registradas para corrección.

### 2. Construir el índice

```bash
python -m fonoscan.cli index --catalog catalogo.csv --out index.npz

# Variantes de velocidad ±2 % y ±4 % (para emisoras que alteran el tempo).
# Cuesta 5× el tamaño del índice; activarlo sólo donde haga falta.
python -m fonoscan.cli index --catalog catalogo.csv --out index-robust.npz --robust
```

### 3. Revisar la higiene del catálogo antes de monitorear

```bash
python -m fonoscan.cli audit --index index.npz
```

Detecta fonogramas con el mismo máster bajo dos ISRC. Si no se resuelven, el
reparto entre esos titulares queda decidido por el ruido.

### 4. Monitorear en vivo

```bash
python -m fonoscan.cli monitor \
    --index index.npz \
    --channel FM100 \
    --url http://stream.emisora.com/fm100 \
    --evidence-dir /var/lib/fonoscan/evidencia \
    --out plays/FM100.jsonl
```

Salida por pase, en JSONL:

```json
{"channel_id":"FM100","track_id":9,"isrc":"ARZZZ2600009","title":"Tema 9",
 "artist":"Artista 3","started_at":"2026-09-11T09:00:50+00:00",
 "ended_at":"2026-09-11T09:01:50+00:00","detected_seconds":60.0,
 "work_coverage":0.8,"kind":"full","hits":11,"best_score":153,
 "mean_score":115.4,"min_margin":3.0,"needs_review":false}
```

### 5. Generar el reporte de uso

```bash
python -m fonoscan.cli report \
    --plays plays/FM100.jsonl \
    --catalog catalogo.csv \
    --channels canales.csv \
    --from 2026-09-01 --to 2026-09-30 \
    --weighting ponderada \
    --out uso-septiembre.csv
```

Ponderaciones disponibles: `por_pase`, `por_duracion`, `por_pase_completo`,
`ponderada` (segundos × coeficiente del medio).

### Además

```bash
# Reproceso de una grabación (auditoría, peritaje, recalibración)
python -m fonoscan.cli offline --index index.npz --file grabacion.mp3 \
    --channel FM100 --epoch 2026-09-11T09:00:00-03:00

# Identificación puntual de un fragmento
python -m fonoscan.cli identify --index index.npz --file fragmento.wav
```

```
 accept  score=198   cov=1.00 margen=19.80 z=113 pos=  16.00s  Artista 3 — Tema 9 [ARZZZ2600009]
 reject  score=10    cov=0.33 margen=0.05  z=4   pos=   1.54s  Artista 4 — Tema 11 [ARZZZ2600011]
```

## API HTTP

```bash
uvicorn fonoscan.api:app --host 0.0.0.0 --port 8080
```

| Endpoint | Uso |
|---|---|
| `GET /health` | Estado y versión del índice cargado |
| `POST /identify` | Identificación de un fragmento subido |
| `GET /plays` | Pases, con filtro por canal, ISRC, período y estado de revisión |
| `GET /reports/usage.csv` | Reporte de uso para el sistema de reparto |
| `GET /reports/usage.json` | Mismo reporte con estructura estilo DDEX |
| `POST /admin/reload-index` | Recarga en caliente tras actualizar el catálogo |

## Despliegue

```bash
cd deploy
cp .env.example .env      # definir contraseñas
docker compose up -d db storage api
docker compose run --rm indexer
docker compose up -d worker-fm100
```

El esquema completo de base de datos está en `deploy/schema.sql`: catálogo,
canales, sesiones de captura, pases particionados por mes, evidencia, cola de
revisión, reclamos, reportes sellados y bitácora inmutable.

## Rendimiento medido

Medido en un contenedor de un solo núcleo, sobre audio sintético con cadena de
emisión simulada (pasabanda 200–3400 Hz, limitador, SNR 8 dB):

| Métrica | Valor |
|---|---|
| Indexación | ~800× tiempo real |
| Densidad del índice | 104 landmarks por segundo de catálogo |
| Memoria del índice | 4,5 MB por hora de catálogo (~0,26 MB por fonograma) |
| Extracción de huella (ventana de 10 s) | 22 ms |
| Consulta contra el índice | 5,5 ms |
| Costo por canal en vivo | ~0,15 vCPU |
| Ventanas correctamente clasificadas | 97 % |
| Falsos positivos sobre audio fuera de catálogo | 0 |

Proyección de catálogo: 50 000 fonogramas ≈ 13 GB de RAM en un nodo; 1 000 000
requiere sharding o PostgreSQL (tablas en `docs/01-arquitectura.md`).

## Mapa del proyecto

```
fonoscan/
├── fonoscan/
│   ├── fingerprint.py   Extracción de huellas (landmarks) + versión incremental
│   ├── index.py         Índice invertido: memoria (numpy) y PostgreSQL
│   ├── matcher.py       Histograma de offsets, métricas de confianza, política
│   ├── aggregator.py    Ventanas → pases consolidados
│   ├── capture.py       Captura con ffmpeg, reconexión, evidencia
│   ├── recognizer.py    Worker en vivo + reproceso offline (mismo código)
│   ├── catalog.py       Alta de catálogo, ISRC, variantes de velocidad, duplicados
│   ├── reporting.py     Reportes de uso, ponderaciones, salida DDEX-like
│   ├── api.py           API HTTP (FastAPI)
│   └── cli.py           Línea de comandos
├── deploy/
│   ├── schema.sql       Esquema PostgreSQL completo
│   ├── docker-compose.yml
│   └── Dockerfile
├── docs/
│   ├── 01-arquitectura.md              Componentes, dimensionamiento, fallas
│   ├── 02-comparativa-fingerprinting.md  Relevamiento de bibliotecas y servicios
│   ├── 03-plan-implementacion.md      Fases, equipo, riesgos, cronograma
│   └── 04-operacion-y-calidad.md      Calibración, umbrales, casos difíciles
└── tests/
    └── test_end_to_end.py
```

## Decisiones de diseño

**Motor propio en lugar de Olaf o Panako.** Ambos son excelentes y están
activamente mantenidos, pero son AGPL-3: al ofrecer un portal en red a los
asociados se dispara la obligación de liberar el código fuente. El motor
incluido es implementación propia y no arrastra esa obligación. Detalle en
`docs/02-comparativa-fingerprinting.md`.

**Landmarks en lugar de huellas neuronales.** Las huellas neuronales rinden
mejor ante degradación fuerte, pero dan resultados aproximados y son difíciles
de explicar ante una impugnación. La hoja de ruta las contempla como segunda
etapa, sólo sobre lo que la primera no resuelve.

**Cuatro métricas de confianza, no una.** Score, cobertura, margen y z. El
margen respecto del mejor candidato de otro fonograma es el mejor predictor de
falso positivo, y es el que sostiene la decisión ante un reclamo.

**El mismo código en vivo y offline.** Cualquier pase informado tiene que poder
reproducirse desde la evidencia archivada. Si el pipeline en vivo y el de
reproceso divergieran, esa garantía se pierde.

**Zona de revisión explícita.** El sistema devuelve `accept` / `review` /
`reject`, no un binario. Los casos límite van a una cola humana en lugar de
convertirse en un reparto silenciosamente equivocado.

## Advertencias

- **El techo del sistema es el catálogo indexado**, no el algoritmo. Si falta
  el audio máster de la mitad de los ISRC registrados, ninguna mejora técnica
  compensa eso.
- **Verificar libertad de operación** en patentes con el asesor de propiedad
  industrial antes del despliegue (US6990453 y US7627477: plazo nominal ya
  transcurrido, pero conviene el dictamen).
- **Definir con legales** la retención de la evidencia de audio, que contiene
  voz de locutores y de terceros.
- **Correr en paralelo al menos dos períodos** antes de cambiar la base del
  reparto.
