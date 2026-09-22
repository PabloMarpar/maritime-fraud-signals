# Plan: chequeo discriminativo normalizado (B) + almacenamiento por ventana (A)

> **Orden de ejecución: primero la Parte B, después la Parte A.** B es barato y decide si la
> inversión en A merece la pena: si las features normalizadas siguen sin discriminar, el problema no
> se arregla con más datos.

## Contexto

El proyecto necesita muestrear muchas ventanas cortas de AIS repartidas por 2022-2024 (unos 100-120
días) porque las etiquetas de sanciones son fechadas: cuanto más atrás esté la ventana, más barcos
quedan "sancionados después" y por tanto utilizables como positivos. Hoy 30 días de datos limpios
ocupan 14,2 GB, así que mantener todas las ventanas a la vez es inviable en este portátil.

Dos cosas se establecieron durante la investigación de este plan y condicionan todo lo que sigue:

1. **El detector de apagones NO era un bloqueo.** `liveness_verdict()` lee únicamente
   `data/coverage/liveness.parquet` (2,1 MB/día), nunca las posiciones limpias; `detect/gaps.py`
   solo lee `voyages.parquet` + `liveness.parquet`. El problema real es operativo: ese archivo se
   escribe como bloque único por rango, y lo mismo ocurre con **todos** los detectores
   (`data/detect/*.parquet`, `voyages.parquet`, `mmsi_imo.parquet`) — la ventana N+1 sobrescribe la
   N. Nada es acumulable hoy.

2. **El chequeo discriminativo ya se ejecutó a mano y sale negativo.** Sobre la ventana actual, los
   petroleros sancionados después muestran *menos* señal que los no sancionados: 0,43 vs 0,73
   apagones por viaje, 0% vs 0,5% de encuentros barco-a-barco, 5,9% vs 32% de spoofing — con volumen
   de mensajes casi idéntico (mediana 23.245 vs 22.459), así que no es un artefacto de exposición
   bruta. La única señal en la dirección correcta es el cambio de calado inexplicado (40,7% vs
   24,6%), justamente la huella de una transferencia ocurrida *fuera* de la cobertura danesa.
   Diagnóstico: los contadores actuales miden **cuánto opera un barco localmente**, no evasión. Un
   ferry danés acumula más eventos que un petrolero en tránsito solo por estar más tiempo delante de
   la antena.

Resultado buscado: features que midan tasa de comportamiento anómalo en vez de volumen de presencia,
y poder procesar ventana a ventana con un pico de disco de ~1 ventana en vez de la suma de todas.

---

# PARTE B (primero) — Chequeo discriminativo con features normalizadas

## B1. Exposición desde `liveness`, no desde los datos limpios

Clave: `liveness.parquet` tiene grano `(cell_lat, cell_lon, cell_hour, mmsi)`, así que
`count(DISTINCT cell_hour)` por MMSI da **horas observadas** y `count(DISTINCT date)` da **días
observados**. Es una medida de exposición limpia que **sobrevive al borrado de las posiciones**.

Añadir a `features/panel.py` columnas `n_observed_hours` y `n_observed_days` derivadas de liveness
(nuevo `_build_exposure`, mismo patrón que los demás `_build_*_agg`).

## B2. Features de tasa

Añadir al panel, junto a los contadores actuales (no en sustitución): eventos por viaje, por día
observado y por cada 1.000 mensajes, para `n_gaps`, `n_gaps_high_probability`,
`n_spoofing_events_total`, `n_sts_episodes`, `n_identity_anomalies_total`,
`n_destination_course_mismatch`, `n_draught_change_unexplained`. Denominadores con `nullif(...,0)`
para que exposición cero dé NULL, no infinito. Extender el test guard existente de `label_` para que
las nuevas columnas no colisionen con el contrato de etiquetas.

## B3. El módulo de chequeo

**Fichero nuevo:** `model/discriminative_check.py` (`model/` es "anomaly scoring and supervised risk
scoring" según `CLAUDE.md`; esto es la evaluación pre-modelo). Convenciones del repo: `_git_sha()`
propio, `_parse_args(argv)` + `main(argv)`, una sola `duckdb.connect()` en try/finally, salida con
`window_start/window_end/built_at/git_sha`.

Debe producir, sobre la población `imo IS NOT NULL`:

1. **Comparación positivos vs negativos** de cada feature cruda y normalizada: n, media, mediana,
   % no-cero.
2. **Controles emparejados**: comparar solo contra barcos del mismo `ship_type` y exposición
   similar (bucket por `n_observed_days`), que es lo que evita volver a comparar petroleros en
   tránsito contra ferrys residentes.
3. **Tamaño de efecto e incertidumbre**: Mann-Whitney U o AUC univariante por feature, con
   intervalos de confianza por bootstrap. Con 147 positivos hay que reportar incertidumbre, no
   solo medias.
4. **Veredicto explícito** por feature: discrimina en la dirección esperada / en la contraria / no
   discrimina.

Salida a `data/processed/discriminative_check.parquet` + un resumen legible en `outputs/`.

## B4. Registrar el hallazgo

Tanto si las tasas rescatan señal como si no, es un resultado que orienta el proyecto entero: anotar
en `docs/DECISIONS.md` (append-only) y en las preguntas abiertas de `docs/STATE.md`. El hallazgo de
que el cambio de calado inexplicado sea la única señal discriminante — y que sea precisamente la
huella de actividad *fuera* de la cobertura — merece su propia entrada.

**Punto de decisión al terminar B:** si ninguna feature normalizada discrimina con intervalo de
confianza que excluya el no-efecto, parar y replantear detectores o cobertura geográfica antes de
ejecutar la Parte A.

---

# PARTE A (después) — Almacenamiento por ventana

## A0. Principios de seguridad (leer antes de escribir código)

El borrado de datos limpios es la única operación irreversible del proyecto. Todo lo demás en la
Parte A está subordinado a estas reglas:

1. **Reducir y borrar son dos comandos separados, nunca el mismo.** `process_window` crea artefactos
   y NO borra nunca. Un segundo comando explícito, `pipeline/prune.py`, borra únicamente días que el
   manifiesto ya marca como verificados. Un bug en la fase de creación no puede borrar nada porque
   no tiene permiso para hacerlo.
2. **Cuarentena antes que borrado.** `prune` mueve las particiones a `data/.trash/date=.../` (un
   rename dentro del mismo sistema de ficheros, instantáneo y reversible). El vaciado real de
   `.trash` ocurre solo en la invocación SIGUIENTE, y solo para días cuya ventana ya se completó con
   éxito. Eso da una ventana entera de margen para detectar un problema.
3. **Huella digital antes de tocar nada.** Antes de mover un día a cuarentena, registrar en el
   manifiesto: `clean_rows`, `n_distinct_mmsi`, `min_timestamp`, `max_timestamp`, bbox redondeada y
   `sum(message_count)`. Esto convierte "irreversible" en "verificable": si mañana se vuelve a
   descargar ese día, se puede comprobar que los datos recuperados son los mismos.
4. **Ensayo de re-descarga como puerta obligatoria.** Antes de cualquier borrado masivo: coger UN
   día, moverlo a cuarentena, volver a descargarlo y limpiarlo, y verificar que la huella coincide
   exactamente. Si no coincide, el archivo del DMA no es estable y la estrategia entera de borrado
   queda invalidada. **Esto se hace una vez, al principio, y condiciona todo lo demás.**
5. **Lo peligroso es opt-in.** `prune` requiere `--yes-delete` explícito; sin él hace dry-run y lista
   lo que haría. No existe ninguna ruta de código en la que un borrado ocurra por defecto.
6. **Interruptor de parada.** Si existe el fichero `data/.no-prune`, `prune` aborta inmediatamente
   sin tocar nada, diga lo que diga el resto de flags.
7. **Tope por invocación.** `prune` nunca procesa más de `--max-days` (por defecto 15) en una
   ejecución. Un bucle defectuoso no puede vaciar el disco entero.
8. **Escrituras atómicas en todos los artefactos nuevos.** Escribir a `.tmp` + `os.replace`, el mismo
   patrón que ya usa `pipeline/manifest.py:62-67`. Un parquet a medio escribir existe y no está
   vacío: pasaría una comprobación ingenua de "el fichero está ahí".
9. **Verificar leyendo, no mirando.** Cada puerta abre el artefacto con DuckDB y comprueba esquema y
   `count(*)`. Que el fichero exista y ocupe bytes no prueba que sea legible.
10. **El manifiesto se respalda antes de cada prune** (`data/manifest.json.bak`). El manifiesto es el
    registro de qué se ha reducido; perderlo es perder el mapa de lo que se tiene.

## A1. Hacer `liveness` acumulable por día

**Fichero:** `detect/liveness.py`

- `LIVENESS_PATH` → `LIVENESS_ROOT = COVERAGE_ROOT / "liveness"`, layout
  `data/coverage/liveness/date=YYYY-MM-DD/part-0.parquet`. Reutilizar
  `process.partitions.partition_path` / `existing_partitions`, ya escritos para este layout exacto.
- `build_liveness(start, end, in_root=CLEAN_ROOT, out_root=LIVENESS_ROOT, ..., force=False) -> list[Path]`:
  mantener `_build` pero llamarlo **por día** (precedente: el bucle día a día de
  `detect/anchorages.py:176`, hecho explícitamente para controlar memoria a escala real), un fichero
  por día con `window_start = window_end = día`. Saltar días ya existentes salvo `force`. Con
  provenance por día, el problema de "rangos disjuntos" desaparece.
- `liveness_verdict(..., liveness_path: Path = LIVENESS_ROOT)`: añadir
  `_liveness_sources(path, first_day, last_day) -> tuple[list[str], set[date]]`. Si `path` es
  fichero → comportamiento legacy intacto. Si es directorio → devolver solo las particiones
  existentes en `[baseline_start.date(), window_end.date()]` y pasar esa **lista** a
  `read_parquet(?)` (DuckDB acepta parámetro lista; un glob como *string* parametrizado no está
  verificado, comprobar antes de usarlo). Da además poda de particiones: con ~2,3 GB de liveness y
  miles de llamadas a `score_gap`, leer la tabla entera en cada llamada es la diferencia entre que
  la ejecución termine o no.

**A1.1 — La corrección del denominador (crítica, no opcional).** Eliminar el CTE `bounds` y la
aritmética de span en `liveness.py:407-414`, y calcular en Python desde la lista de ficheros:

```
covered = {d for d in daterange(baseline_start, baseline_end - 1día) si existe partición}
baseline_hours_available = 24.0 * len(covered)
```

Con ventanas disjuntas el span ingenuo sobrecuenta el denominador, lo que **infravalora**
`expected_corroborators` y sesga todos los veredictos hacia `no_evidence`. Descalibración silenciosa
y sistemática. Mantener la aritmética antigua solo en la rama de fichero único.

**A1.2 — Migración.** Re-ejecutar `build_liveness` por día sobre junio 2024 **mientras los datos
limpios siguen existiendo**, antes de borrar nada.

## A2. Artefactos por ventana

Todos los detectores y los dos procesos de identidad escriben hoy un único fichero de rango
completo. Cambiar a `out_path = data/<kind>/window=<start>_<end>/part-0.parquet`, con escritura
atómica (A0.8).

Los consumidores (`features/panel.py`, `detect/gaps.py`, `detect/sts_agreement.py`) ya reciben cada
entrada como parámetro `--*-path` e interpolan en `read_parquet('<str>')`, así que globs tipo
`data/detect/behaviour/*/part-0.parquet` deberían funcionar sin tocar código — **verificar que uno
de ellos parsea realmente antes de darlo por hecho.**

**Bloqueo duro:** `features/panel.py:365` (`_build_ship_type`) y
`detect/identity_anomalies.py:461` (`_build_ship_types`) leen particiones limpias directamente. El
paso de reducción debe emitir `data/reference/ship_type/window=.../part-0.parquet`
(`mmsi, ship_type, n_messages` + provenance) y ambos llamadores necesitan un parámetro de ruta que
lo prefiera.

## A3. Trazas adelgazadas

**Fichero nuevo:** `process/thin.py`

`build_thin_tracks(start, end, in_root=CLEAN_ROOT, out_root=THIN_ROOT, interval_minutes=5, force=False)`,
particionado por día en `data/tracks/thin/date=.../part-0.parquet`. SQL:
`time_bucket(INTERVAL 'N minutes', timestamp)` + `row_number() OVER (PARTITION BY mmsi, bucket ORDER
BY timestamp) = 1`, conservando `mmsi, timestamp, latitude, longitude, sog, cog, nav_status,
ship_type`.

Tamaño sin medir: estimación ~10-25 MB/día a 5 min (~2 GB para 120 días). **Medir sobre un día real
y bajar a 10-15 min si excede presupuesto.** Sirve para el mapa de la Fase 5 y la revisión manual —
**nunca para re-detectar**: a 5 min se pierden los saltos de posición del spoofing y la geometría de
aproximación de los encuentros.

## A4. Orquestación (crea, no borra)

**Fichero nuevo:** `pipeline/window.py`

```
process_window(start, end, data_root=DATA_ROOT, lead_in_days=30,
               min_free_gb=DEFAULT_MIN_FREE_GB, thin_minutes=5, force=False, dry_run=False) -> None
```

Orden: (1) `backfill_range(lead_in_start, end)` reutilizado tal cual — ya salta estados
`clean`/`reduced`; (2) `build_liveness` día a día sobre todo el rango incluido el lead-in; (3) sobre
`[start, end]`: trazas adelgazadas, referencia de ship_type, `tracks`, `identity`, y luego
anchorages/spoofing/sts/behaviour/identity_anomalies; (4) `_verify_window() -> list[str]` de fallos;
(5) si la lista está vacía, registrar en el manifiesto la huella digital de cada día (A0.3) y
marcarlo `verified_at`. **No borra nada, nunca.**

## A5. Borrado (comando separado)

**Fichero nuevo:** `pipeline/prune.py`

```
prune(data_root=DATA_ROOT, max_days=15, yes_delete=False) -> None
```

Comportamiento: aborta si existe `data/.no-prune`; respalda el manifiesto; vacía de `.trash` los días
cuya ventana posterior ya se completó; selecciona días con `verified_at` presente y
`clean_discarded_at` ausente, hasta `max_days`; re-verifica **todas** las puertas en el momento del
borrado (no confía en la verificación anterior); mueve a `.trash`; registra
`clean_discarded_at=_now_iso()` con `manifest.record`. Sin `--yes-delete` solo imprime el plan.

Reutilizar el patrón de `pipeline/backfill.py:173-178` (unlink, rmdir si vacío, record) y sus
precondiciones de `:152-171`. El estado `reduced` ya existe en `pipeline/manifest.py:48-51`
(`_REDUCED_MARKER = "clean_discarded_at"`), `day_state()` ya lo devuelve, `backfill.py:104` ya lo
salta y `tests/test_manifest.py:81-86,139-143` ya lo cubren — **nada lo escribe todavía; eso es lo
único que falta**.

**Puertas (todas obligatorias, re-evaluadas en el momento del borrado):** artefactos existen y son
legibles con DuckDB; no vacíos donde se exige (`tracks`, `liveness`, `thin` con `n_rows > 0`; los
detectores de eventos pueden ser 0 pero se registra ruidosamente); nº de días en `thin` == nº de días
limpios; MMSI distintos en `thin` ≥ 0,95× los de limpio; cobertura de liveness completa para todo
`[start - lead_in, end]`; huella digital registrada; provenance de los artefactos coincide con la
ventana que se está podando; disco libre ≥ `min_free_gb`; `data/.no-prune` ausente; `--yes-delete`
presente.

**Puerta de cordura estadística:** comparar las tasas de eventos por día de la ventana nueva contra
las de junio 2024. Una desviación de más de un orden de magnitud aborta el borrado y avisa — un
detector que se rompe silenciosamente con datos de otro año produciría un fichero pequeño pero
válido que todas las demás puertas aprobarían.

---

# Verificación

- **Ensayo de re-descarga (A0.4) antes que nada en la Parte A.** Es la puerta que convierte el
  borrado en reversible-en-la-práctica. Si falla, parar.
- **Tests nuevos** (`tests/`, funciones pytest planas, fixtures sintéticas en `tmp_path`, sin tocar
  `data/`): denominador con cobertura disjunta (dos islas de 3 días → `baseline_hours_available ==
  144.0`, no el span); idempotencia de particiones diarias de liveness; poda (un día fuera de rango
  presente en disco no altera el veredicto); adelgazado (N posiciones en un bucket → 1); **cada
  puerta de borrado fallando por separado impide el borrado**; `.no-prune` aborta; `max_days` se
  respeta; sin `--yes-delete` no se toca el disco; tasas con exposición cero → NULL.
- **Equivalencia antes de borrar nada**: re-ejecutar `build_liveness` particionado sobre junio 2024 y
  comprobar que `detect/gaps.py` produce **los mismos 74.546 candidatos y la misma distribución de
  veredictos** que el fichero único actual. Si no coincide, parar.
- **Primera ventana real completa sin ejecutar `prune`**, comparando artefactos contra los de junio
  2024 antes de confiar en el ciclo.
- `pytest` completo (346 tests hoy) y `python -m ruff check .` limpios en cada paso.

# Riesgos que quedan aun con las salvaguardas

- **Irreversible sin volver a descargar**: cambiar un umbral de detector, añadir un detector nuevo,
  re-derivar encuentros, o cualquier cosa por debajo de la resolución de `thin_minutes`. La huella
  digital permite verificar una re-descarga, pero no evita tener que hacerla.
- **Que el archivo del DMA deje de servir un día antiguo** — el ensayo de re-descarga solo prueba un
  día en un momento; no garantiza disponibilidad futura de todos.
- **Lead-in incompleto**: si falta algún día de liveness en los 30 previos, los veredictos salen
  descalibrados sin avisar. Es puerta de borrado obligatoria, no un aviso.
- `DEFAULT_MIN_FREE_GB = 20.0` se mide antes de cada día, no de cada ventana — revisar que sigue
  siendo suficiente con el pico real de una ventana.
