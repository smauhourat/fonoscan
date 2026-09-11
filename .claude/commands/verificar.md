Corré `python tests/test_end_to_end.py` y el benchmark de rendimiento.

Compará los resultados contra la tabla de rendimiento de CLAUDE.md:
- indexación ~800x tiempo real
- 104 landmarks/s de referencia
- extracción de consulta 22 ms / consulta 5,5 ms
- >= 85 % de ventanas correctas
- secuencia de pases exacta
- cero falsos positivos

Si algún indicador empeoró, decime exactamente cuál y en cuánto, y no commitees.
