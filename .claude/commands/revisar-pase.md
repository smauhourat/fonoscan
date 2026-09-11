Tomá el pase que te indique y reconstruí toda su cadena de evidencia:

1. Buscá la evidencia de audio del intervalo.
2. Corré `recognize_file()` sobre esa evidencia con la misma configuración.
3. Compará el resultado con lo que quedó registrado en la tabla `play`.
4. Mostrame score, coverage, margin y z, y los tres mejores candidatos.

Si el resultado no reproduce el registrado, eso es un bug grave de
trazabilidad: parate ahí y explicame la discrepancia antes de seguir.
