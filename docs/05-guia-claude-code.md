# 05 — Guía de implementación con Claude Code

Cómo pasar de este repositorio a un sistema en producción, trabajando con
Claude Code. Verificado contra la documentación oficial
(https://code.claude.com/docs/en/quickstart) en septiembre de 2026.

---

## 1. Instalación

Necesitás una cuenta Claude (Pro, Max, Team o Enterprise), una cuenta de Claude
Console con créditos, o acceso vía un proveedor de nube soportado.

**macOS, Linux, WSL:**

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

**Windows PowerShell:**

```powershell
irm https://claude.ai/install.ps1 | iex
```

**Alternativas:** `brew install --cask claude-code` en macOS,
`winget install Anthropic.ClaudeCode` en Windows, o los paquetes apt/dnf/apk en
Debian, Fedora, RHEL y Alpine. La instalación nativa se actualiza sola en
segundo plano; Homebrew y WinGet no.

Verificar e iniciar sesión:

```bash
claude --version      # imprime la versión seguida de "(Claude Code)"
claude                # abre el navegador para autenticar en el primer uso
```

Claude Code también está disponible en la web, como app de escritorio, en VS
Code y JetBrains, en Slack, y en CI/CD con GitHub Actions y GitLab. Para este
proyecto, la terminal o la app de escritorio son lo más cómodo.

---

## 2. Arranque del repositorio

```bash
cd fonoscan
git init && git add -A && git commit -m "Base del sistema de monitoreo"

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tests/test_end_to_end.py     # confirmar que el motor funciona

claude
```

Primer prompt, antes de pedir nada:

```
Leé CLAUDE.md y los cuatro documentos de docs/. Después explicame con tus
palabras qué hace el sistema, cuál es el invariante que usa el agregador para
consolidar pases, y por qué no se puede usar Panako.
```

Si la respuesta es correcta, el contexto quedó cargado. Si no, hay que mejorar
el `CLAUDE.md` antes de seguir — y eso vale más que cualquier prompt elaborado.

---

## 3. Permisos

Creá `.claude/settings.json` para preaprobar lo seguro y que no te pregunte en
cada paso:

```json
{
  "permissions": {
    "allow": [
      "Read", "Glob", "Grep",
      "Bash(python -m pytest*)",
      "Bash(python tests/*)",
      "Bash(python -m fonoscan.cli*)",
      "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)",
      "Bash(ruff*)", "Bash(mypy*)"
    ],
    "ask": [
      "Bash(git push*)",
      "Bash(docker compose up*)",
      "Bash(psql*)"
    ]
  }
}
```

Dentro de la sesión, `Shift+Tab` cicla entre modos de permisos.

---

## 4. Ritmo de trabajo que funciona

**Una tarea por sesión, con `/clear` entre tareas.** El contexto arrastrado de
una tarea anterior degrada la siguiente.

**Pedí exploración antes de cambios.** El patrón que mejor rinde:

```
1. Leé fonoscan/matcher.py y explicame cómo se calcula el margen.
2. No cambies nada todavía. Proponeme tres formas de manejar el caso donde
   el segundo candidato es el mismo fonograma indexado a otra velocidad.
3. [elegís una] Implementá la opción 2 y corré las pruebas.
```

**Exigí números.** Cualquier cambio en el camino caliente:

```
Medí el rendimiento antes y después del cambio con el benchmark de
tests/, y mostrame ambos números. Si empeoró más de 10 %, revertilo.
```

**Commits chicos y frecuentes.** `commiteá los cambios con un mensaje
descriptivo` después de cada tarea verificada. Es lo que te permite revertir sin
drama.

---

## 5. Prompts por fase del plan

Siguen el orden de `docs/03-plan-implementacion.md`.

### Fase 0 — Diagnóstico del catálogo

```
Escribí un script scripts/diagnostico_catalogo.py que reciba el CSV de catálogo
y un directorio de audio, y reporte: cuántos fonogramas tienen ISRC válido,
cuántos tienen archivo de audio existente y legible, distribución de duraciones,
formatos y tasas de bits encontrados, y los ISRC duplicados. Salida en tabla por
consola y CSV con los casos a corregir. Sin dependencias nuevas.
```

### Fase 1 — Piloto

```
Necesito poder anotar manualmente audio de validación. Construí una herramienta
web de una sola página (HTML+JS, servida por FastAPI) que cargue un archivo de
audio, permita reproducirlo, marcar inicio y fin de cada tema con teclas, buscar
el fonograma en el catálogo por artista/título, y exportar las anotaciones en
JSONL con el mismo esquema que produce el CLI offline. Leé
docs/04-operacion-y-calidad.md sección 2.1 antes de diseñarlo.
```

```
Escribí scripts/calibrar.py: toma anotaciones de referencia en JSONL y un
archivo de audio, corre el reconocimiento con una grilla de valores de
DecisionPolicy, y produce la curva precisión-recall A NIVEL DE PASE (un pase es
correcto si coincide el fonograma y los intervalos se solapan al menos 50 %).
Que imprima la política recomendada para precisión ≥ 99,5 % y grafique la curva
a PNG con matplotlib.
```

### Fase 2 — Escalado de catálogo

```
Implementá la persistencia real en PostgreSQL. Hoy PostgresIndex en
fonoscan/index.py está escrito pero no probado contra una base viva. Levantá el
compose, aplicá deploy/schema.sql, escribí pruebas de integración que verifiquen
que MemoryIndex y PostgresIndex devuelven resultados idénticos sobre el mismo
catálogo, y medí la latencia de consulta con 5 millones de landmarks.
```

```
El índice en memoria no entra en un nodo con más de 150 mil fonogramas.
Implementá sharding por los bits altos del hash: un ShardedIndex que reciba N
instancias de MemoryIndex y combine los resultados de lookup preservando la
semántica exacta del matcher. Que el test end-to-end pase idéntico con 1 y con
8 shards.
```

### Fase 3 — Escalado de canales

```
Necesito un supervisor de workers: un proceso que lea la tabla channel de
PostgreSQL, levante un ChannelRecognizer por canal activo, los reinicie si
mueren, escriba las sesiones en capture_session, y exponga métricas Prometheus
(segundos capturados, desconexiones, ventanas procesadas, latencia de consulta,
pases por minuto). Que recargue la lista de canales sin reiniciar.
```

```
Los pases hoy van a un archivo JSONL. Escribí el sink que los persiste en
PostgreSQL respetando los invariantes de CLAUDE.md: nada se edita en el lugar,
las correcciones usan supersede_id, y cada fila guarda engine_version,
config_id e index_version. Incluí las migraciones.
```

### Fase 4 — Integración con liquidación

```
Construí el backoffice de revisión: lista priorizada desde v_review_queue,
reproductor del fragmento de evidencia correspondiente al pase, comparación
lado a lado con el fonograma de referencia en la misma posición, y acciones
confirmar / reasignar / rechazar que escriban en review_task y generen la fila
de supersede cuando corresponda. FastAPI + HTML server-side, sin framework de
frontend.
```

```
Implementá el sellado de reportes: al congelar un usage_report, calcular
SHA-256 determinista del contenido, marcarlo frozen, e impedir cualquier
modificación posterior. Agregá el flujo de reporte de ajuste para correcciones
posteriores al sellado. Con pruebas que verifiquen que el hash es estable entre
corridas.
```

### Fase 5 — Segunda etapa neural

```
Leé docs/02-comparativa-fingerprinting.md sección 1.3. Diseñá (todavía sin
implementar) la integración de una segunda etapa neural que procese sólo las
ventanas que la primera etapa deja en review o rechaza. Necesito el diseño de
la interfaz, cómo se mantiene la trazabilidad de la decisión, y qué pasa cuando
las dos etapas discrepan. Escribilo en docs/06-etapa-neural.md.
```

---

## 6. Comandos personalizados

Guardá prompts recurrentes en `.claude/commands/`. Por ejemplo
`.claude/commands/verificar.md`:

```markdown
Corré python tests/test_end_to_end.py y el benchmark de rendimiento.
Compará los números contra la tabla de CLAUDE.md.
Si algún indicador empeoró, decime exactamente cuál y en cuánto, y no
commitees nada.
```

Se invoca con `/verificar` dentro de la sesión. Escribí `/` para ver los
comandos y skills disponibles.

---

## 7. Errores que conviene evitar

**Pedir features grandes de una.** "Implementá el backoffice completo" produce
mucho código que nadie revisó. Partilo en tres o cuatro tareas verificables.

**No revisar los diffs.** Claude Code es rápido; la revisión sigue siendo tuya.
En un sistema que decide repartos de dinero, el código no revisado es deuda con
intereses.

**Dejar que reescriba el motor.** Si una sesión propone cambiar
`fingerprint.py` en profundidad, parala y preguntá qué problema concreto
resuelve. El motor está medido y probado; los cambios ahí tienen que
justificarse con números.

**Olvidar actualizar `CLAUDE.md`.** Cada decisión de arquitectura que tomen
juntos debería quedar ahí. Es lo que hace que la sesión número cuarenta sea tan
buena como la primera.

**Confiar en el test sintético como única validación.** Pasa siempre porque el
audio es sintético. La validación real es la anotación manual de la fase 1.

---

## 8. Trabajo en equipo

- `CLAUDE.md` versionado en el repo: es contexto compartido, no configuración
  personal.
- `.claude/settings.json` también versionado;
  `.claude/settings.local.json` para lo personal, en `.gitignore`.
- Comandos de `.claude/commands/` versionados: el equipo comparte los
  procedimientos.
- Para revisión automática de PRs, la integración con GitHub Actions está
  documentada en https://code.claude.com/docs/en/github-actions

---

## 9. Documentación de referencia

- Quickstart: https://code.claude.com/docs/en/quickstart
- Buenas prácticas: https://code.claude.com/docs/en/best-practices
- Flujos comunes: https://code.claude.com/docs/en/common-workflows
- Extensión (CLAUDE.md, skills, hooks, MCP): https://code.claude.com/docs/en/features-overview
