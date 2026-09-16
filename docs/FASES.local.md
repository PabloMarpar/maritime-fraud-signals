# Fases del proyecto — nota personal

> Fichero **local**: está en `.gitignore`, no se sube a GitHub. Es la chuleta del autor, en
> castellano y en lenguaje llano. La versión pública y granular vive en `tasks.json` y `docs/STATE.md`.
>
> _Última actualización: 2026-09-16_

## Dónde estamos ahora

**Fase 0, recién empezada.** Hecho el andamiaje del repositorio (1 tarea de 34). Lo siguiente es
bajar un solo día de datos daneses y ver barcos en pantalla.

Progreso global: **1/34 tareas**.

| Fase | Qué es | Tareas | Estado |
|---|---|---|---|
| 0 | Cimientos | 1/5 | 🟡 en curso |
| 1 | Procesamiento | 0/4 | ⬜ pendiente |
| 2 | Detectores | 0/7 | ⬜ pendiente |
| 3 | Etiquetas | 0/3 | ⬜ pendiente |
| 4 | Modelos | 0/7 | ⬜ pendiente |
| 5 | Producto | 0/4 | ⬜ pendiente |
| 6 | Operación | 0/4 | ⬜ pendiente |

---

## Fase 0 — Cimientos

**Objetivo: ver barcos en una pantalla el primer día.** Un pipeline elegante que tarda dos semanas en
enseñar el primer barco se abandona; uno que enseña barcos el primer día se termina.

- [x] `P0-1` Andamiaje del repo: carpetas, ficheros de continuidad, subagentes, comandos
- [ ] `P0-2` `ingest/dma.py`: descargar AIS danés de un rango de fechas y dejarlo en Parquet por día
- [ ] `P0-3` Abrirlo con DuckDB: confirmar columnas, número de filas y de barcos distintos
- [ ] `P0-4` Pintar un día de posiciones en un mapa estático
- [ ] `P0-5` Dockerfile y entorno reproducible

**Preguntas abiertas:** qué día usar como primera muestra (tráfico normal, no festivo); si el
certificado HTTPS caducado de los daneses obliga a HTTP plano o a un `verify=False` documentado.

## Fase 1 — Procesamiento

Convertir mensajes sueltos en rutas de barcos fiables.

- [ ] `P1-1` Limpieza: MMSI inválidos, coordenadas imposibles, mensajes duplicados
- [ ] `P1-2` Identidad: enlazar MMSI con IMO, detectar MMSI reutilizados y huérfanos
- [ ] `P1-3` Reconstruir la ruta de cada barco y partirla en viajes
- [ ] `P1-4` Tests con casos inventados para cada patología de limpieza

## Fase 2 — Detectores

Los cinco detectores. **No son machine learning**: son reglas de física y sentido común, funcionan
desde el primer día y cualquiera entiende por qué salta una alerta.

- [ ] `P2-1` **Mapa de cobertura empírico**: probabilidad de que se oiga a un barco en cada cuadrícula
      del mar. Es *la* pieza técnica; sin ella confundes "apagó el aparato" con "se alejó de la antena"
- [ ] `P2-2` Detector 1 — apagones deliberados. Devuelve una probabilidad, no un sí/no
- [ ] `P2-3` Detector 2 — posición falsificada (velocidades imposibles, barcos en tierra, círculos
      sintéticos, el mismo identificador en dos sitios a la vez)
- [ ] `P2-4` Detector 3 — transbordos en alta mar, con la definición pública de Global Fishing Watch
- [ ] `P2-5` Detector 4 — cambios de identidad (bandera, nombre, MMSI)
- [ ] `P2-6` Detector 5 — contradicciones en lo declarado (calado contra puertos, destino contra rumbo)
- [ ] `P2-7` Medir cuánto coincide el detector 3 con la API de eventos de GFW

## Fase 3 — Etiquetas

Las listas de sanciones, que son la "respuesta correcta" contra la que se valida todo.

- [ ] `P3-1` Descargar OFAC, UE y Reino Unido **con la fecha de designación de cada barco**
- [ ] `P3-2` Cruzar con nuestros barcos por IMO y MMSI; cuantificar tasa de acierto y ambigüedad
- [ ] `P3-3` Construir la tabla barco-mes etiquetada

## Fase 4 — Modelos

**Aquí sale el resultado que se enseña en una entrevista.**

- [ ] `P4-1` Baseline tonto: petrolero de más de 15 años con bandera de conveniencia
- [ ] `P4-2` Isolation Forest sobre las features barco-mes
- [ ] `P4-3` LightGBM con cortes temporales móviles; precisión en el top-20 y mejora sobre el baseline
- [ ] `P4-4` Calibración: curva de fiabilidad y Brier score. Si dice 80%, que acierte 8 de cada 10
- [ ] `P4-5` SHAP: ninguna alerta sale sin sus razones
- [ ] `P4-6` Pasar `analyst-review` buscando fugas temporales y validación tramposa
- [ ] `P4-7` Sección del README sobre el sesgo de las etiquetas

**Regla de oro:** si LightGBM no bate al baseline tonto, el problema está en las features, no en el
modelo. Nada exótico hasta que lo básico funcione. Los autoencoders de trayectorias quedan fuera de
la ruta principal.

**El experimento que vertebra todo:** entrenar sólo con datos anteriores a una fecha `T`, evaluar
**sólo** contra sanciones publicadas después de `T`. Si el sistema marca un barco en marzo y Bruselas
lo sanciona en septiembre, eso no admite discusión.

## Fase 5 — Producto

Lo que se ve y se enseña.

- [ ] `P5-1` Exportar rutas, apagones y encuentros como GeoJSON/Arrow
- [ ] `P5-2` Mapa deck.gl + MapLibre: rutas animadas, apagones en rojo, encuentros marcados
- [ ] `P5-3` Publicar en GitHub Pages; probar en móvil y en escritorio
- [ ] `P5-4` Generador de fichas de barco con nivel de confianza explícito

## Fase 6 — Operación

De prototipo a algo que se mantiene solo.

- [ ] `P6-1` Captura en vivo de Gibraltar/Ceuta con AISStream, **con compresión activada**
- [ ] `P6-2` Descargas automáticas con GitHub Actions
- [ ] `P6-3` Reproducir un caso conocido: puntuar un barco ya sancionado usando sólo su historial
      anterior a la sanción
- [ ] `P6-4` README definitivo y segunda pasada de `analyst-review`

---

## Cosas que conviene no olvidar

- **Las dos zonas**: estrechos daneses para desarrollar (histórico de años, gratis) y Gibraltar/Ceuta
  para aplicar (captura en vivo). El mismo método en dos sitios distintos demuestra que no está
  ajustado a medida de uno.
- **Los datos crudos no entran nunca en el contexto del chat.** Siempre DuckDB devolviendo un
  agregado o 10 filas.
- **Una sesión, un objetivo.** `/retomar` al empezar, `/cerrar` antes de quedarse sin contexto.
- **Honestidad sobre las etiquetas**: los barcos sancionados no son "todos los infractores", son "los
  que alguien pilló". Explicar eso bien vale más que cualquier décima de precisión.

---

_Este fichero se actualiza cuando cambien las fases o el estado. Fuentes de verdad: `tasks.json`
(tareas), `docs/STATE.md` (estado), `docs/DECISIONS.md` (decisiones) y el plan completo en_
`C:\Users\pablo.mparera\.claude\plans\hagamos-un-proyecto-nuevo-vast-volcano.md`.
