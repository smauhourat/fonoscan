# 04 — Operación y calidad

## 1. Por qué los umbrales no son un detalle técnico

En este sistema, mover un umbral mueve dinero. Un falso positivo transfiere una
liquidación de un titular a otro; un falso negativo sólo deja un pase sin
cobrar. Los errores no son simétricos y la política de decisión debe reflejar
esa asimetría de forma explícita y documentada, porque eventualmente hay que
explicarla ante el consejo directivo o ante un reclamo.

La `DecisionPolicy` incluida exige **cuatro** condiciones simultáneas para
aceptar. Los valores por defecto (score ≥ 25, cobertura ≥ 0,35, margen ≥ 1,6,
z ≥ 8) son un punto de partida para ventanas de 10 s de FM, no una verdad.
Hay que recalibrarlos con datos propios.

## 2. Procedimiento de calibración

### 2.1 Construir el conjunto de validación

1. Elegir 20 horas de aire distribuidas entre canales, días de semana y fines
   de semana, franja diurna y nocturna. No usar sólo horario central: la
   programación nocturna suele tener repertorio distinto y más automatización.
2. Anotar manualmente: fonograma, inicio, fin. Dos anotadores por hora, con un
   tercero resolviendo desacuerdos. El acuerdo inter-anotador es un dato que
   hay que reportar: si dos personas no coinciden, no se le puede exigir más al
   sistema.
3. Incluir deliberadamente los casos difíciles: locución encima de la música,
   temas encadenados por el operador, cortinas, publicidad con música de
   catálogo, y al menos 2 horas de repertorio **no indexado** para medir falsos
   positivos.

Para la elección inicial del motor y una primera aproximación de umbrales sirve
el dataset BAF (ver documento 02), que fue construido específicamente para
monitoreo de radiodifusión. Pero es de uso no comercial: el conjunto de
producción tiene que ser propio.

### 2.2 Barrido de umbrales

```
python -m fonoscan.cli offline --index index.npz --file validacion.wav \
    --channel CAL --out pases.jsonl
```

Correr con varias políticas y construir la curva precisión-recall **a nivel de
pase**, no de ventana. Un pase se considera correcto si coincide el fonograma y
los intervalos se solapan al menos 50 %.

### 2.3 Punto de operación

Fijar el umbral en la precisión objetivo y tomar el recall que salga, nunca al
revés.

| Uso del dato | Precisión objetivo | Comentario |
|---|---|---|
| Base de reparto | ≥ 99,5 % | Todo lo que no llegue va a revisión |
| Estadística y tendencias | ≥ 97 % | Tolera más ruido |
| Detección de uso no licenciado | ≥ 95 % con recall alto | Es un disparador de investigación, no una liquidación |

Documentar la calibración con fecha, conjunto usado, curva obtenida y umbral
elegido. Ese documento es parte de la defensa del reparto.

## 3. Indicadores de operación diaria

| Indicador | Qué revela | Umbral de alerta |
|---|---|---|
| **Tasa de identificación** por canal y día | Salud general. Vista `v_identification_rate` | Caída > 15 puntos respecto de la media de 30 días |
| **Disponibilidad de captura** | Segundos capturados / segundos del día | < 98 % |
| **Pases en revisión** / pases totales | Calidad del catálogo y de los umbrales | > 8 % |
| **Pases con margen < 2,0** | Colisiones de catálogo | Tendencia creciente |
| **Fragmentos** / pases totales | Cortinas, o problema de segmentación | > 25 % |
| **Latencia de consulta** p95 | Salud del índice | > 200 ms |
| **Antigüedad del índice** | Novedades sin indexar | > 48 h |

Una tasa de identificación baja casi nunca es culpa del algoritmo. En orden de
frecuencia: el capturador estuvo caído, la emisora pasa mucho hablado, o falta
catálogo. El tablero debe permitir distinguir las tres en menos de un minuto.

## 4. Casos difíciles y cómo se tratan

### 4.1 Colisiones de catálogo (el mismo máster con dos ISRC)

Pasa constantemente: reediciones, compilados, licencias por territorio, un
sello que reporta el mismo audio dos veces. El motor no puede distinguir dos
archivos idénticos, y si decide "por ruido" el reparto se vuelve arbitrario.

```
python -m fonoscan.cli audit --index index.npz --min-shared 500
```

Devuelve pares con miles de hashes compartidos. Resolución: marcar uno como
`duplicate_of` el otro en la tabla `track` y definir una regla de negocio para
la titularidad (la más común es atribuir al ISRC de la edición vigente en el
territorio). **Hay que resolverlo antes de liquidar, no después.**

### 4.2 Versiones distintas de la misma obra

Radio edit, versión de álbum, remaster, en vivo, remix. Son fonogramas
distintos, con ISRC distintos y a veces titularidad distinta. El sistema los
distingue correctamente **si están todos indexados**. Si sólo está la versión
de álbum y suena el radio edit, el resultado típico es un pase parcial con
cobertura baja: revisable, no erróneo. La respuesta es de catálogo, no de
algoritmo.

### 4.3 Covers y versiones regrabadas

El fingerprinting acústico **no** los detecta y no debe pretender hacerlo: son
fonogramas diferentes. Si el cover es de un socio y está indexado, se detecta
como lo que es. Si la entidad además gestiona derechos de autor sobre la obra
subyacente, ese cruce se hace por metadatos (ISWC), no por audio.

### 4.4 Locución encima, compresión fuerte, procesamiento de aire

Es el caso normal, no la excepción. La banda 120–3400 Hz y el umbral adaptativo
por banda están elegidos para eso. En las pruebas con cadena de emisión simulada
(filtro pasabanda 200–3400 Hz, limitador, SNR de 8 dB) el motor mantiene 97 % de
ventanas correctas. Si un canal específico rinde mal, revisar primero su cadena
de procesamiento: algunas emisoras usan procesadores multibanda muy agresivos.

### 4.5 Alteración de velocidad

Algunas emisoras aceleran o comprimen la programación. Síntoma: caída de recall
concentrada en un canal, con el resto normal. Verificación: tomar evidencia de
ese canal y correr `cli identify` sobre variantes generadas con `asetrate`.
Solución: indexar el catálogo con `--robust` (variantes ±2 % y ±4 %) y usar ese
índice sólo para los canales afectados, para no multiplicar por cinco el índice
de todos.

### 4.6 Medleys, mashups y mezclas de DJ

El offset salta dentro de lo que perceptivamente es un solo bloque. El agregador
los corta en varios pases cortos. Es el comportamiento correcto: cada fonograma
usado recibe su detección. Revisar la regla de reparto para fragmentos, porque
en un mashup ningún fonograma alcanza cobertura alta.

### 4.7 Simulcast y repetidoras

Varias frecuencias emitiendo lo mismo generan pases duplicados. Se resuelve en
la capa de reporte, agrupando canales por `license_id`, no en el motor.
Conviene modelar explícitamente la relación cabecera/repetidora en la tabla
`channel`.

### 4.8 Publicidad con música de catálogo

Un fonograma del catálogo usado como cortina de un aviso genera pases cortos y
repetidos a lo largo del día. Técnicamente es correcto. Si la regla de reparto
lo trata distinto de una difusión, la marca `kind = fragment` más la
repetitividad del horario permiten separarlos.

## 5. Cola de revisión humana

Motivos de encolado, por prioridad:

| Prioridad | Motivo | Acción del revisor |
|---|---|---|
| 1 | Reclamo de socio o de usuario de música | Escuchar evidencia, resolver, dejar nota |
| 2 | `duplicate_catalog`: par conflictivo conocido | Aplicar la regla de titularidad |
| 3 | `low_margin`: margen entre 1,25 y 1,6 | Confirmar o reasignar |
| 4 | `fragment` con alta repetición | Clasificar como cortina o difusión |
| 5 | Muestreo aleatorio de pases aceptados | **Control de calidad permanente** |

El punto 5 es el que se suele omitir y el más importante: revisar al azar un
0,5 % de los pases aceptados es lo único que detecta una degradación silenciosa
del sistema. Sin muestreo, un cambio en la cadena de una emisora o una
regresión de código puede pasar meses inadvertida.

Toda resolución queda en `review_task` con actor, resolución y notas, y si
cambia la atribución genera una fila nueva en `play` con `supersede_id`. El
original nunca se edita.

## 6. Trazabilidad y defensa del reparto

Ante una impugnación hay que poder responder, para un pase concreto:

1. **Evidencia de audio** del intervalo (tabla `evidence`, con SHA-256).
2. **Versión del motor y del índice** con que se decidió (`engine_version`,
   `config_id`, `index_version` en la fila de `play`).
3. **Métricas de la decisión** (score, cobertura, margen, z), persistidas.
4. **Reproducción**: `recognize_file()` sobre la evidencia archivada, con la
   misma configuración, debe dar el mismo resultado. Esta reproducibilidad es
   la razón por la que el pipeline en vivo y el offline comparten exactamente
   el mismo código.
5. **Bitácora** de quién tocó qué (`audit_log`).

El reporte de uso se sella con hash y `frozen = true`. Las correcciones
posteriores se emiten como reporte de ajuste; no se reescribe lo ya liquidado.

## 7. Transparencia hacia los asociados

Publicar periódicamente, por canal y período:

- horas monitoreadas y horas efectivamente identificadas;
- tasa de identificación y su evolución;
- cantidad de pases en revisión y cuántos se resolvieron;
- cobertura del catálogo indexado sobre el total de fonogramas registrados.

Un socio que ve que su emisora principal tuvo 62 % de identificación entiende
mucho mejor su liquidación que uno que sólo recibe un número. Y un sistema que
publica sus propias limitaciones resiste mucho mejor el cuestionamiento que uno
que se presenta como infalible.

## 8. Qué no hace este sistema

Conviene decirlo por escrito para que nadie asuma lo contrario:

- No identifica obras, sólo grabaciones. La obra se resuelve por metadatos.
- No detecta covers ni interpretaciones en vivo distintas de la grabación.
- No identifica lo que no está en el índice. La cobertura del catálogo es el
  techo duro del sistema.
- No reemplaza el criterio humano en los casos límite; los encola.
- No mide audiencia ni valor económico del uso: mide difusión.
