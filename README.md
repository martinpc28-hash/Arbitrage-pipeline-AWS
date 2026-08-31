# Buscador de arbitraje deportivo (OddsPapi + AWS)

Detecta oportunidades de arbitraje (surebets) entre casas de apuestas para
todos los partidos de hoy y mañana, bajo demanda (botón), 100% serverless
en AWS (eu-west-3, París), sin Docker.

## Estado: DESPLEGADO Y FUNCIONANDO EN PRODUCCIÓN

- API: `https://d6xu9m467d.execute-api.eu-west-3.amazonaws.com/prod`
- Frontend: `http://oddsarb-frontend-909011175236.s3-website.eu-west-3.amazonaws.com`
- Cuenta AWS: `909011175236`, región `eu-west-3`

**IMPORTANTE:** el código de este repo se sincronizó manualmente después de
varias iteraciones de parches aplicados directamente en AWS durante una
sesión de deploy asistido (no hubo `sam deploy` real: el despliegue se hizo
recurso por recurso vía API de AWS, usando un proyecto de CodeBuild como
"máquina de build" para generar los zips, porque el entorno del asistente
no podía transferir binarios directamente). El código en este repo debería
coincidir con lo desplegado a fecha de la última sincronización, pero si
vas a seguir editando, trata `template.yaml` como una plantilla de
referencia para una migración futura a Infra-as-Code real, no como la
fuente de verdad del estado actual — la fuente de verdad son los recursos
ya creados en la cuenta de AWS.

## Recursos creados en AWS (manualmente, no vía CloudFormation)

- DynamoDB: `oddsarb-jobs`, `oddsarb-results`, `oddsarb-cache` (con TTL)
- Lambdas (Python 3.12, sin capas): `oddsarb-estimate-job`,
  `oddsarb-confirm-job`, `oddsarb-fetch-odds-detect-arb`,
  `oddsarb-aggregate-results`, `oddsarb-get-job-status`
- Step Functions: `oddsarb-arbitrage`
- API Gateway REST: id `d6xu9m467d`, stage `prod`
- IAM roles: `oddsarb-lambda-role`, `oddsarb-sfn-role`, `oddsarb-codebuild-role`
- S3: `oddsarb-frontend-909011175236` (frontend estático),
  `oddsarb-staging-909011175236` (staging de builds, se puede borrar)
- CodeBuild: `oddsarb-build` (se usó como "máquina de build" remota para
  generar los zips de Lambda sin poder subir binarios directamente desde
  el asistente; se puede borrar si no lo vas a seguir usando así)

Para reconstruir esto desde cero con Infra-as-Code real, `template.yaml`
(SAM) es el punto de partida, pero **no ha sido probado con `sam deploy`**
en esta sesión — solo se usó como documentación de la arquitectura.

## Arquitectura

```
Frontend (botón)
  │
  ▼
POST /jobs/estimate          → Lambda: lista fixtures hoy/mañana + cuota disponible
  │
  ▼  (usuario confirma el gasto de solicitudes, con aviso si excede cuota)
POST /jobs/{jobId}/confirm   → Lambda: arranca Step Functions
  │
  ▼
Step Functions
  ├─ Map (concurrencia 3, controlada)
  │    └─ por cada fixture: GET /v4/odds (con caché 10min) → filtrar
  │       mercados de exactamente 2 resultados → detectar arbitraje →
  │       enriquecer nombres con catálogo /v4/markets (caché 24h) →
  │       guardar SIEMPRE un resumen por fixture (haya o no arbitraje)
  └─ Agregar resultados → marcar job como DONE
  │
  ▼
GET /jobs/{jobId}            → Lambda: frontend hace polling hasta status=DONE
                                devuelve opportunities (con arbitraje real)
                                y scannedFixtures (TODOS los partidos, con
                                el mercado más cercano a arbitraje de cada uno)
```

## Descubrimientos importantes de esta sesión (ya corregidos en el código)

1. **Bloqueo de Cloudflare por IP de datacenter (error 1010).** OddsPapi
   bloquea peticiones sin un `User-Agent` de navegador real. Ver
   `common/oddspapi_client.py::_headers()`.
2. **Rate limit no documentado de ~5 solicitudes/segundo**, además del
   límite mensual del plan. Se maneja con reintentos automáticos en 429
   con backoff, incluyendo el campo `retryMs` que a veces devuelve la API.
3. **Free tier: 250 solicitudes/MES en total**, no por día. Cada
   `GET /odds` cuenta 1 solicitud sin importar cuántas casas/mercados
   traiga. Por eso el flujo tiene el paso `estimate` → `confirm` separado,
   con aviso explícito si la búsqueda excede la cuota restante.
4. **Esquema de líneas (over/under, hándicap):** no hay un campo `"line"`
   separado; cada línea distinta es un `market_id` numérico diferente.
   Agrupar por `market_id` (lo que hace el parser) ya es seguro.
5. **Nombres de mercados/resultados:** `GET /odds` casi nunca trae
   `marketName`/`outcomeLabel` poblados. Hay que pedirlos aparte con
   `GET /markets?sportId=X` (catálogo cacheado 24h) y hacer el join por
   `marketId`/`outcomeId`.
6. **Falsos positivos de arbitraje:**
   - Mercados con 3+ resultados reales (ej. 1X2) donde solo se detectaron
     cuotas de 2 casas para 2 de esos resultados generan arbitrajes
     falsos. Solución: se descartan mercados que no tengan EXACTAMENTE 2
     resultados detectados.
   - Incluso con exactamente 2 resultados, se observaron cuotas claramente
     de prueba/erróneas de casas específicas (`balkanbet.rs`, `gamdom`)
     en secuencias sospechosamente redondas (10.0, 9.8, 9.6...). Solución
     aplicada: se descarta cualquier "arbitraje" con más de 30% de
     ganancia (`PROFIT_PCT_MAX` en `fetch_odds_detect_arb/app.py`), umbral
     muy por encima de lo que ocurre en arbitrajes reales (normalmente
     0.5%-5%).
7. **DynamoDB + boto3 resource:** no acepta `float` nativo de Python (usar
   `Decimal`), ni siempre maneja bien números al leer (vienen como
   `Decimal`, no mezclar con `float` en operaciones aritméticas sin
   convertir antes). Ver `_to_decimal()` en `fetch_odds_detect_arb/app.py`.
8. **Límite de 400KB por ítem de DynamoDB:** algunos fixtures con muchas
   casas/mercados generan una respuesta demasiado grande para cachear
   completa. El guardado en caché está en un `try/except` que no debe
   tumbar el procesamiento del fixture si falla.
9. **Links a los eventos:** OddsPapi expone `fixturePath` (link general al
   evento en esa casa) a nivel de cada bookmaker, y opcionalmente
   `betslip` (deep-link a la apuesta exacta) a nivel de cada precio, casi
   siempre `null` en la práctica. El código usa `betslip` si existe, si no
   cae a `fixturePath`. **Ojo:** en pruebas reales el `fixturePath` a veces
   apunta a la categoría de deporte equivocada dentro de la casa (ej. un
   partido de béisbol enlazando a la sección de hockey) — es una
   limitación de los datos de la API, no del código. Además, algunas casas
   (`polymarket.us`, `duel`, `circasports`, entre otras) simplemente no
   traen ni `betslip` ni `fixturePath` en la respuesta de OddsPapi — el
   frontend lo maneja bien (muestra el nombre de la casa sin link), pero
   no hay forma de "arreglarlo" desde el código: es un hueco de los datos.
10. **`GET /markets` NO filtra por deporte (bug corregido el 2026-08-31).**
    El código original llamaba `GET /markets?sportId=X` asumiendo que
    devolvía solo los mercados de ese deporte. **Confirmado contra la
    documentación oficial** (oddspapi.io/us/docs/get-markets): el único
    parámetro que acepta ese endpoint es `language`; `sportId` se ignora en
    silencio y siempre devuelve el catálogo COMPLETO (todos los deportes).
    Ese catálogo completo pesa varios cientos de KB — muy por encima del
    límite de 400KB por ítem de DynamoDB — así que el caché de 24h
    (guardado como un solo ítem) fallaba en CADA invocación
    (`ValidationException: Item size has exceeded the maximum allowed
    size`, visible en CloudWatch), y por eso `marketLabel`/`outcomeLabel`
    salían `null` el 100% de las veces (el frontend caía de vuelta a
    mostrar `marketKey`/`outcomeId` crudos). Arreglado en
    `oddspapi_client.get_markets()` (ya no envía `sportId`) y en
    `fetch_odds_detect_arb/app.py::_catalogo_nombres()` (ahora el catálogo
    se cachea partido en varios ítems de DynamoDB, ver `_catalog_chunk_key`).
    Verificado en producción: `GET /markets` en sí **no consume cuota**
    mensual (solo `GET /odds` la consume), así que este bug no gastaba
    cuota, pero sí hacía cada invocación más lenta (recargaba el catálogo
    entero en cada fixture) y dejaba los mercados sin nombre legible.

## Antes de seguir trabajando en esto

- Correr `sam build && sam deploy --guided` NO se ha probado en esta
  sesión. Si quieres pasar a Infra-as-Code real, hazlo contra una cuenta/
  stack nuevo primero, o importa los recursos existentes a un stack de
  CloudFormation antes de tocar nada en producción.
- El bucket `oddsarb-staging-909011175236` y el proyecto CodeBuild
  `oddsarb-build` se usaron como mecanismo de despliegue alternativo (el
  asistente no podía subir binarios directamente por una limitación de su
  entorno de ejecución) y se pueden borrar sin afectar el funcionamiento
  normal de la app.
- El mapeo de `sportId` en el `<select>` del frontend es parcial y no fue
  verificado exhaustivamente contra el catálogo real de OddsPapi (solo se
  confirmó que `sportId=13` es Baseball, no Tenis como se asumió al
  principio). Antes de confiar en otros valores del desplegable, verificar
  contra `GET /sports` o los `sport_ids` que trae `GET /account`.

## Estructura del proyecto

```
template.yaml                          Plantilla SAM (referencia, no probada con sam deploy)
statemachine/definition.asl.json       Definición del Step Function (coincide con lo desplegado)
frontend/index.html                    Frontend estático (coincide con lo desplegado en S3)
src/common/
  arbitrage.py                         Matemática del arbitraje + evaluar_mercado_siempre
  oddspapi_parser.py                   Agrupa cuotas por mercado, extrae links
  oddspapi_client.py                   Cliente OddsPapi: User-Agent, reintentos 429, /markets
src/lambdas/
  estimate_job/                        POST /jobs/estimate
  confirm_job/                         POST /jobs/{jobId}/confirm
  fetch_odds_detect_arb/               1 vez por fixture: filtro 2 resultados, catálogo, cap 30%
  aggregate_results/                   Cierra el job al final del Map
  get_job_status/                      GET /jobs/{jobId}: opportunities + scannedFixtures
```

## Siguientes pasos sugeridos

1. Verificar el mapeo completo de `sportId` contra `GET /sports` de OddsPapi.
2. Decidir si migrar a IaC real (SAM/CDK) o seguir gestionando los recursos
   manualmente.
3. Evaluar filtrar también por casa de apuestas de baja confianza (ya se
   identificaron `balkanbet.rs` y `gamdom` con datos sospechosos en al
   menos un mercado).
4. Si se compra el plan de pago de OddsPapi, subir `MaxConcurrency` en el
   Step Function y relajar o quitar el paso de confirmación de gasto.
5. **Pendiente:** el caché de 10 min de `GET /odds` por fixture
   (`_cached_odds` en `fetch_odds_detect_arb/app.py`) también puede fallar
   por el límite de 400KB por ítem en fixtures con muchas casas/mercados
   (visto en producción: "Invalid string attribute value: The string
   exceeds the maximum permitted length"). No rompe la búsqueda (está en
   un `try/except`), pero para esos fixtures grandes el caché de 10 min no
   sirve y una búsqueda repetida vuelve a gastar cuota. Si se quiere
   arreglar, aplicar el mismo patrón de chunking que se usó para el
   catálogo (punto 10 de "Descubrimientos importantes").
