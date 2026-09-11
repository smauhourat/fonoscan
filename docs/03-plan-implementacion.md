# 03 — Plan de implementación

Plan por fases, pensado para una entidad de gestión colectiva que hoy liquida
con planillas declaradas por los usuarios de música y quiere pasar a medición
automática. El orden está elegido para que cada fase produzca valor propio y
para que la más riesgosa (cambiar la base del reparto) llegue cuando ya hay
evidencia acumulada.

## Fase 0 — Preparación (4–6 semanas)

Nada de esto es técnico y todo condiciona el resultado.

| Tarea | Entregable | Responsable |
|---|---|---|
| Inventario de medios a monitorear | Planilla: emisora, tipo, URL/frecuencia, licencia, clase tarifaria, prioridad | Operaciones / Licencias |
| Diagnóstico del catálogo | Cuántos fonogramas, cuántos con ISRC válido, cuántos con audio máster disponible | Sistemas + Documentación |
| Decisión sobre repertorio internacional | Construir, comprar o híbrido | Dirección |
| Dictamen legal | Retención de evidencia de audio, base de tratamiento, libertad de operación en patentes, política de licencias de software | Legales |
| Definición de reglas de reparto | Qué cuenta como uso: ¿por pase? ¿por duración? ¿los fragmentos cuentan? ¿desde qué cobertura mínima? | Consejo directivo |

**El punto crítico es el audio máster.** Un catálogo de 80 000 ISRC del que sólo
hay audio de 30 000 rinde, como techo, 37 % de identificación. Ningún algoritmo
arregla eso. Si el diagnóstico muestra una brecha grande, la primera inversión
es un plan de ingesta de audio con los sellos asociados, no el motor.

## Fase 1 — Piloto técnico (6–8 semanas)

**Alcance:** 10 emisoras representativas (FM comercial, FM de nicho, AM, una
TV abierta, una radio online), 5 000–10 000 fonogramas del catálogo de mayor
rotación.

Actividades:

1. Desplegar `docker-compose.yml` en un servidor único.
2. Ingestar el subconjunto de catálogo; correr `cli audit` y resolver
   duplicados antes de seguir.
3. Monitorear 30 días continuos, sin informar nada a nadie.
4. **Anotar manualmente 20 horas** distribuidas entre canales y franjas
   horarias. Es tedioso y es imprescindible: sin verdad de referencia propia no
   hay calibración ni defensa posible de los umbrales.
5. Calibrar `DecisionPolicy` con esa anotación (procedimiento en documento 04).
6. Medir contra las planillas declaradas por las emisoras: la diferencia entre
   lo declarado y lo detectado suele ser el hallazgo que justifica todo el
   proyecto.

**Criterios de salida:**

| Indicador | Meta |
|---|---|
| Precisión a nivel de pase | ≥ 99,0 % |
| Recall a nivel de pase (repertorio indexado) | ≥ 95 % |
| Tasa de identificación sobre tiempo monitoreado | ≥ 60 % en FM musical |
| Disponibilidad de captura | ≥ 98 % del tiempo por canal |
| Cola de revisión | ≤ 5 % de los pases |

## Fase 2 — Escalado de catálogo (8–12 semanas, solapa con la 1)

- Ingesta masiva del catálogo de socios. Proceso industrial: recepción de
  audio, validación de ISRC, deduplicación, cálculo de huellas, control de
  calidad por muestreo.
- Automatizar el alta de novedades: los socios deben poder subir un fonograma
  nuevo y verlo indexado en menos de 24 horas. Si el alta es manual, el sistema
  se degrada solo.
- Decidir sharding según el tamaño alcanzado (tabla de dimensionamiento en el
  documento 01).
- Activar `--robust` en los canales donde se detecte alteración de velocidad.

## Fase 3 — Escalado de canales (8–12 semanas)

- Pasar a nodos de captura distribuidos.
- Incorporar emisoras sin stream online mediante nodos SDR en el interior.
- Instrumentación completa: Prometheus + tableros de tasa de identificación por
  canal y día, con alertas sobre caídas.
- Backoffice de revisión humana con reproducción de la evidencia.

## Fase 4 — Integración con liquidación (6–8 semanas)

- Conectar `usage_report` con el sistema de socios.
- **Correr en paralelo** al menos dos períodos completos: se liquida como
  siempre y se calcula en paralelo con la medición automática, comparando.
- Presentar al consejo directivo las diferencias, por emisora y por titular,
  antes de cambiar la base del reparto.
- Definir el procedimiento de reclamos y el circuito de ajustes.

## Fase 5 — Operación y mejora continua

- Recalibración trimestral de umbrales con anotación fresca.
- Evaluación de la segunda etapa neural sobre el residuo no identificado.
- Cruce sistemático con el proveedor comercial, si se contrató.
- Reporte anual de cobertura y calidad a los asociados: es un requisito de
  transparencia razonable y además disciplina al equipo.

## Cronograma agregado

```
Mes:        1    2    3    4    5    6    7    8    9   10   11   12
Fase 0    ████
Fase 1         ████████
Fase 2              ██████████████
Fase 3                       ██████████████
Fase 4                                      ██████████
Fase 5                                                  ████████████►
```

## Equipo

| Rol | Dedicación | Cuándo |
|---|---|---|
| Ingeniero de datos / backend | 1 FTE | Todo el proyecto |
| Ingeniero de infraestructura | 0,5 FTE | Fases 1 y 3 |
| Especialista en audio / MIR | 0,5 FTE | Fases 1, 2 y 5 |
| Analista de documentación musical | 1 FTE | Fase 2 en adelante (ingesta y revisión) |
| Product owner del área de reparto | 0,3 FTE | Todo el proyecto |
| Legales | Consultas puntuales | Fase 0 y 4 |

El rol de documentación musical suele subestimarse. En régimen, la cola de
revisión y la higiene de catálogo son trabajo humano permanente, y la calidad
del reparto depende más de ese trabajo que del algoritmo.

## Riesgos

| Riesgo | Prob. | Impacto | Mitigación |
|---|---|---|---|
| Catálogo sin audio máster suficiente | Alta | Alto | Diagnóstico en fase 0; plan de ingesta con sellos; complementar con proveedor comercial |
| ISRC mal cargados o duplicados | Alta | Alto | Validación automática; `cli audit`; resolución previa a liquidar |
| Resistencia de emisoras a ser medidas | Media | Medio | Correr en paralelo; mostrar evidencia de audio; período de transición |
| Impugnación del reparto por un socio | Media | Alto | Trazabilidad total: evidencia, versiones, umbrales, bitácora inmutable |
| Streams inestables o que cambian de URL | Alta | Medio | Monitoreo de disponibilidad; inventario revalidado; SDR como respaldo |
| Cambio de velocidad en emisoras | Media | Medio | Variantes indexadas; detección por caída de recall |
| Dependencia de una sola persona técnica | Media | Alto | Documentación; código propio y legible; sin cajas negras |
| Sobrecosto de almacenamiento de evidencia | Media | Medio | Política de retención escalonada definida con legales |

## Presupuesto orientativo (infraestructura, 200 canales)

| Concepto | Mensual aprox. |
|---|---|
| 8 nodos de captura (4 vCPU / 8 GB) | según proveedor |
| PostgreSQL con réplica | según proveedor |
| Object storage, evidencia caliente 90 días (≈9 TB) | según proveedor |
| Archivo frío | según proveedor |
| Proveedor comercial complementario | tarifa por canal |

No pongo cifras porque varían demasiado por región y por proveedor; la tabla
está para que el área de sistemas la complete con cotizaciones reales. El orden
de magnitud relevante: el almacenamiento de evidencia termina costando más que
el cómputo del reconocimiento.
