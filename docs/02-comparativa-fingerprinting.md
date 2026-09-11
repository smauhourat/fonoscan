# 02 — Comparativa de bibliotecas y motores de *fingerprinting*

Relevamiento realizado en septiembre de 2026. La conclusión operativa está al
final (sección 6).

## 1. Familias de algoritmos

### 1.1 Landmarks / constellation map (Wang, 2003)

Picos espectrales locales emparejados en hashes `(f1, f2, Δt)`. Es la base de
Shazam y de la mayoría de los sistemas abiertos.

**A favor:** búsqueda exacta por hash (índice invertido trivial, sublineal),
robusto a ruido aditivo y a compresión con pérdida, costo de indexación bajo,
implementación comprensible y auditable — lo que importa cuando un tercero
impugna un reparto.

**En contra:** no es invariante a cambios de velocidad ni de tono. Un
estiramiento del 2 % desalinea los Δt y el recall se desploma. Es una
limitación relevante porque las emisoras aceleran o comprimen la programación
para cuadrar tandas.

### 1.2 Landmarks invariantes a escala temporal

Panako introduce un frente constante-Q (transformada de Gabor no estacionaria)
y hashing casi-exacto sobre un B-tree persistente, con lo que tolera
modificaciones de tiempo y tono de hasta ~10 % manteniendo escalabilidad. Es
directamente la respuesta a la debilidad anterior.

### 1.3 Huellas neuronales (contrastivas)

El trabajo de referencia es NAFP (Chang et al., ICASSP 2021): aprendizaje
contrastivo que produce embeddings por segmento, invariantes a degradaciones,
buscados con FAISS. La literatura reciente sigue esa línea: GraFPrint (ICASSP
2025) con redes de grafos; Araz et al. (ISMIR 2025) muestra que una adaptación
autosupervisada de la pérdida triplete rinde mejor que las alternativas y logra
estado del arte tanto en un conjunto degradado sintéticamente como en uno real
grabado con micrófonos en salas; y un trabajo de noviembre de 2025 explora
usar modelos fundacionales de música preentrenados como backbone.

**A favor:** mejor robustez ante degradación fuerte, índice mucho más compacto
(vectores en lugar de decenas de hashes por segundo), tolerancia natural a
pequeños cambios de velocidad.

**En contra:** hay que entrenar o adaptar un modelo, el índice vectorial da
resultados aproximados (no exactos), la explicación de una decisión es más
difícil de sostener ante un reclamo, y la evidencia reciente es que el
rendimiento cae bastante fuera de las condiciones de entrenamiento — Nikou y
Giannakopoulos (2025) proponen un protocolo de evaluación con grabaciones
reales de celular y reportan caídas sustanciales de dos modelos CNN respecto de
los números publicados. Conviene tomarlo como **segunda etapa** sobre los casos
que el motor de landmarks no resuelve, no como reemplazo.

### 1.4 Huellas "de archivo" (Chromaprint / AcoustID)

Chromaprint es el componente cliente del proyecto AcoustID y genera una huella
de la pista completa, incluida su duración. Está pensado para identificar
**archivos** y reconciliar metadatos, no fragmentos cortos y ruidosos de aire.
Para una entidad de gestión es una herramienta de higiene de catálogo
(deduplicación, cruce con MusicBrainz), no de monitoreo.

## 2. Implementaciones abiertas

| Proyecto | Lenguaje | Licencia | Estado | Fuerte en | Débil en |
|---|---|---|---|---|---|
| **audfprint** (D. Ellis, LabROSA) | Python | MIT | Estable, poco movimiento | Baseline académico bien documentado; usado en el dataset BAF | Rendimiento y operación en producción |
| **Dejavu** (worldveil) | Python + MySQL/Postgres | MIT | Sin mantenimiento activo | Didáctico, muy citado (≈6,7 k estrellas) | Porting a Python 3 con parches de terceros; esquema de BD no escala |
| **Olaf** (J. Six) | C11 | **AGPL-3** | Activo | Portabilidad extrema: corre en ESP32/RP2040 con ≥250 kB; consulta de 20 s contra 1 h de índice en <512 kB; compila a WebAssembly | Algoritmo = Shazam clásico (sin invarianza a velocidad); AGPL |
| **Panako 2.x** (J. Six) | Java | **AGPL-3** | Activo | Invarianza a tiempo/tono hasta ±10 %; constante-Q; LMDB | AGPL; depende de Gaborator (AGPL, con licencia comercial disponible); JVM en el borde |
| **SoundFingerprinting** (AddictedCS) | C#/.NET | MIT (biblioteca) | Muy activo | Pensado para streams en tiempo real; granularidad ~1,46 s; posición exacta de la consulta; desde v8 también huella de video | El almacén persistente recomendado (Emy) es comercial: la versión comunitaria es gratuita sólo para uso no comercial |
| **stream-audio-fingerprint** (Adblock Radio) | Node.js | MIT | **Archivado (marzo 2025)** | Diseñado explícitamente para streams infinitos de radio | Sin mantenimiento |
| **neural-audio-fp** (Chang et al.) | Python/TF | MIT | Referencia académica | Punto de partida para la etapa neural | Requiere entrenamiento y GPU |
| **Chromaprint / pyacoustid** | C + Python | LGPL / MIT | Muy activo | Identificación de archivos; ecosistema MusicBrainz | No sirve para fragmentos de aire |

### Nota de licenciamiento — importante para una entidad de gestión

Olaf y Panako son **AGPL-3**. La AGPL agrega a la GPL la cláusula de uso en
red: si el software se usa para prestar un servicio accesible por red —por
ejemplo, el portal donde los socios consultan sus pases— hay que poner el
código fuente correspondiente a disposición de esos usuarios. Para uso
estrictamente interno sin acceso de terceros por red la obligación no se
dispara, pero la frontera es fina y en una entidad que por definición atiende a
sus asociados conviene resolverlo antes, no después. Gaborator (dependencia de
Panako) ofrece licencia comercial alternativa.

Por eso el motor incluido en este proyecto es implementación propia: permite
integrarlo al sistema de la entidad sin arrastrar obligaciones de apertura.

### Nota de propiedad industrial

El README de Panako advierte sobre las patentes **US7627477 B2** y
**US6990453**, que cubren técnicas usadas por varios de estos algoritmos.
Corresponden a solicitudes de 2000–2003, de modo que su plazo nominal de 20
años ya transcurrió, pero antes del despliegue conviene un dictamen de libertad
de operación del asesor de propiedad industrial de la entidad para la o las
jurisdicciones de uso. Es un trámite barato comparado con el riesgo.

## 3. El recurso de evaluación que hay que usar: BAF

**BAF: An Audio Fingerprinting Dataset for Broadcast Monitoring** (Cortès,
Ciurana, Molina, Miron, Meyers, Six y Serra, 2022) es el conjunto de datos
hecho específicamente para este caso de uso. Fue anotado por seis anotadores,
con cada consulta anotada de forma cruzada por tres, lo que da un acuerdo
inter-anotador alto y valida la metodología. Se distribuye vía Zenodo a
pedido, para investigación no comercial, y tiene cargador en `mirdata`.

El kit de reproducibilidad compara audfprint, Panako/Olaf, NeuralFP y PeakFP
con configuraciones publicadas. Es la base natural para calibrar umbrales y
para justificar técnicamente ante el consejo directivo por qué se eligió un
motor y no otro.

**Advertencia de licencia:** es para investigación no comercial. Sirve para
elegir y calibrar, no para operar. El conjunto de validación de producción
tiene que construirlo la entidad con su propio catálogo y sus propias
emisoras — y además eso es mejor, porque refleja las cadenas de emisión reales
del territorio.

## 4. Servicios comerciales

| Proveedor | Perfil | Datos relevantes |
|---|---|---|
| **BMAT** (Vericast / Reportal) | Especializado en entidades de gestión; es el proveedor de referencia en el sector | Declara seguimiento de 8 200 canales 24×7 en 134 países y 80 millones de identificaciones diarias; certificación ISO 27001; consolidación de repeticiones en una sola planilla de uso; evidencia de audio y video accesible desde el tablero. Tiene clientes declarados en la región, entre ellos CAPIF (Argentina) |
| **ACRCloud** | Plataforma de reconocimiento con fuerte orientación a API | Base de más de 150 millones de grabaciones; más de 50 000 emisoras de radio y TV ya indexadas, lo que evita tener que conseguir las URLs de los streams; devuelve ISRC y UPC en el resultado; permite subir contenido propio a una base privada; API de resultados en tiempo real e histórico; prueba de 14 días |
| **Audible Magic** | Identificación de contenido, fuerte en plataformas UGC | Menos orientado a planillas de uso de radiodifusión |
| **Tunesat, Soundmouse, Radiomonitor** | Monitoreo por nicho (publicidad, producción, airplay) | Útiles como fuente cruzada de verificación |

## 5. Construir contra comprar

No es una decisión binaria. El patrón que mejor funciona en entidades de
gestión es **híbrido**.

| | Construir (este proyecto) | Comprar |
|---|---|---|
| Costo inicial | Desarrollo + infraestructura | Bajo; suscripción |
| Costo marginal por canal | ~0,15 vCPU, infraestructura propia | Tarifa por canal por mes |
| Cobertura de catálogo | Sólo lo que la entidad indexa | Base global de decenas de millones de grabaciones |
| Repertorio internacional | Hay que conseguir los másteres | Resuelto |
| Emisoras locales sin stream | Resuelto con SDR propio | Depende del proveedor |
| Soberanía del dato | Total | El proveedor ve el uso del repertorio nacional |
| Auditabilidad ante impugnación | Total: código, umbrales y evidencia propios | Caja negra; se depende del reporte del proveedor |
| Dependencia | Equipo técnico propio | Proveedor único |

**Recomendación.** Construir el núcleo para el **repertorio nacional y el
catálogo de socios**, que es donde la entidad tiene los másteres, donde se
concentra el reparto y donde la auditabilidad importa más. Complementar con un
proveedor comercial para el **repertorio internacional** y para canales de
difícil captura. Con el tiempo, la proporción se ajusta según cuánto catálogo
propio logre indexarse.

Un beneficio lateral del sistema propio, frecuentemente subestimado: sirve de
**control cruzado** del reporte del proveedor. Tener dos fuentes independientes
sobre los mismos canales durante algunos meses revela discrepancias sistemáticas
y da poder de negociación.

## 6. Decisión técnica adoptada en este proyecto

1. **Motor principal:** landmarks, implementación propia, sin dependencias
   AGPL, con densidad asimétrica referencia/consulta.
2. **Contramedida de velocidad:** multi-indexación de variantes ±2 % y ±4 %
   (`cli index --robust`), que cuesta 5× el índice pero se activa sólo para los
   canales donde se detecta el problema.
3. **Segunda etapa (hoja de ruta):** motor neural tipo NAFP con índice FAISS,
   aplicado sólo a las ventanas que la primera etapa deja en `review` o
   rechaza. Reduce el falso negativo sin comprometer la explicabilidad de los
   pases aceptados en primera etapa.
4. **Calibración:** BAF para la elección y el ajuste inicial; conjunto propio
   de validación con emisoras del territorio para producción.
5. **Complemento comercial:** contrato con un proveedor para repertorio
   internacional, con cruce sistemático de resultados.

## Referencias

- Wang, A. (2003). *An Industrial-Strength Audio Search Algorithm*. ISMIR.
- Six, J. & Leman, M. (2014). *Panako — A Scalable Acoustic Fingerprinting System Handling Time-Scale and Pitch Modification*. ISMIR.
- Six, J. (2021). *Panako 2.0 — Updates for an Acoustic Fingerprinting System*. ISMIR LBD. https://archives.ismir.net/ismir2021/latebreaking/000039.pdf
- Six, J. (2023). *Olaf: a lightweight, portable audio search system*. JOSS. https://github.com/JorenSix/Olaf
- Cortès, G. et al. (2022). *BAF: An Audio Fingerprinting Dataset for Broadcast Monitoring*. ISMIR. https://github.com/guillemcortes/baf-dataset
- Chang, S. et al. (2021). *Neural Audio Fingerprint for High-specific Audio Retrieval based on Contrastive Learning*. ICASSP. https://arxiv.org/abs/2010.11910
- Bhattacharjee, A. et al. (2025). *GraFPrint: A GNN-based approach for audio identification*. ICASSP.
- Araz, R. O. et al. (2025). *Enhancing Neural Audio Fingerprint Robustness to Audio Degradation for Music Identification*. ISMIR. https://arxiv.org/abs/2506.22661
- Nikou, C. & Giannakopoulos, T. (2025). *Contrastive and Transfer Learning for Effective Audio Fingerprinting through a Real-World Evaluation Protocol*. https://arxiv.org/abs/2507.06070
- *Robust Neural Audio Fingerprinting using Music Foundation Models* (2025). https://arxiv.org/pdf/2511.05399
- Chromaprint / AcoustID: https://pypi.org/project/pyacoustid/ y https://musicbrainz.org/doc/Fingerprinting
- SoundFingerprinting: https://github.com/AddictedCS/soundfingerprinting
- BMAT para entidades de gestión: https://www.bmat.com/cmo/
- ACRCloud, monitoreo de radiodifusión: https://www.acrcloud.com/broadcast-monitoring/
