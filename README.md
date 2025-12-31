

# EVA Analysis Service

Backend de análisis de audio y emociones para **EVA** (EVA 1 = frontend, EVA 2 = backend).

Este servicio recibe *shards* de audio, los analiza (transcripción, emociones, momentos críticos) y persiste los resultados en una base de datos SQLite/Postgres. El frontend EVA consume estos resultados vía HTTP.

---

## 1. Repositorio y estructura de proyecto

Este directorio `eva-analysis-service/` forma parte del workspace de EVA junto con el frontend:

- `eva/` → Frontend Next.js (EVA 1, interfaz y IndexedDB)
- `eva-analysis-service/` → Backend FastAPI (EVA 2, análisis y persistencia)

Si tienes un repositorio remoto para este proyecto, puedes enlazarlo aquí, por ejemplo:

```text
Repo raíz: https://github.com/hildealeman/eva-analysis-service.git
Frontend:  ./eva
Backend:   ./eva-analysis-service

⸻

2. Descripción general

EVA Analysis Service es un servicio HTTP basado en FastAPI con las siguientes responsabilidades:
	•	Recibir audio de voz (como “shards” de una sesión).
	•	Ejecutar análisis:
	•	Transcripción de audio.
	•	Extracción de rasgos / features.
	•	Análisis emocional y semántico.
	•	Guardar:
	•	Información de episodios (sessions) en la tabla episode.
	•	Shards y su análisis en la tabla shard (meta_json, features_json, analysis_json).
	•	Exponer endpoints de lectura y edición para que el frontend liste episodios y actualice notas/etiquetas.

Tecnologías principales:
	•	Python 3
	•	FastAPI + Uvicorn
	•	SQLModel + SQLite (por defecto, configurable a Postgres).
	•	Librerías de audio/IA (p. ej. faster-whisper, openai) según la configuración.

⸻

3. Requisitos
	•	Python 3.10+ (recomendado 3.11).
	•	ffmpeg instalado en el sistema (si el pipeline de audio lo requiere).
	•	Herramientas de compilación típicas de tu sistema (para instalar dependencias de audio si hacen falta).

Instala dependencias de Python:

cd eva-analysis-service
python3 -m venv .venv
source .venv/bin/activate   # en macOS/Linux
# .venv\Scripts\activate    # en Windows PowerShell

pip install -r requirements.txt


⸻

4. Configuración (variables de entorno)

Las variables mínimas/útiles son:
	•	EVA_DB_URL (opcional)
	•	URL de la base de datos en formato SQLAlchemy.
	•	Por defecto: sqlite:///./eva.db
	•	Ejemplos:
	•	SQLite (default): sqlite:///./eva.db
	•	Postgres: postgresql+psycopg2://user:password@host:5432/eva
	•	Variables relacionadas con modelos (nombres concretos pueden variar según tu implementación):
	•	OPENAI_API_KEY si usas modelos de OpenAI.
	•	Otras variables específicas del modelo de transcripción/análisis (consulta el código en src/ para ver las opciones que ya tengas configuradas).

Configura también el frontend para apuntar a este backend:

En eva/.env.local (frontend):

NEXT_PUBLIC_EVA_ANALYSIS_MODE=local
NEXT_PUBLIC_EVA_LOCAL_ANALYSIS_BASE=http://localhost:5005
NEXT_PUBLIC_EVA_DATA_MODE=api   # si quieres que el frontend use los endpoints GET/PATCH


⸻

5. Ejecutar el servidor en desarrollo

Desde eva-analysis-service/ con tu entorno virtual activo:

uvicorn src.main:app --host 0.0.0.0 --port 5005 --reload

El API quedará disponible en:
	•	http://localhost:5005

⸻

6. Modelo de datos

La base de datos se gestiona con SQLModel. Hay dos tablas principales:

6.1 Episode

Representa una sesión completa de escucha con EVA.

Campos típicos (resumen):
	•	id: str — identificador de episodio.
	•	created_at: datetime
	•	title: Optional[str]
	•	note: Optional[str]

Los episodios se agregan y actualizan a medida que llegan shards y que el usuario edita metadatos (título, nota).

6.2 Shard

Representa un “momento” (clip) dentro de un episodio.

Campos típicos:
	•	id: str — identificador único del shard.
	•	episode_id: Optional[str] — referencia al episodio.
	•	start_time: Optional[float]
	•	end_time: Optional[float]
	•	source: Optional[str]
	•	meta_json: dict — metadatos (ej. shardId, episodeId, source, etc.).
	•	features_json: dict — features numéricos o rasgos derivados.
	•	analysis_json: dict — resultado del modelo (emociones, transcripción, etc.).
	•	Dentro de este dict, el bloque analysis_json["user"] se reserva para ediciones del usuario:
	•	status
	•	userTags
	•	userNotes
	•	transcriptOverride

⸻

7. Endpoints de la API

7.1 Health check
	•	GET /health
Devuelve un JSON simple indicando que el servicio está vivo.

Ejemplo:

curl -sS http://localhost:5005/health | python3 -m json.tool


⸻

7.2 Analizar shard de audio
	•	POST /analyze-shard
Recibe audio + metadatos de un shard, ejecuta el pipeline de análisis y guarda en la DB.

Campos típicos (multipart form-data):
	•	audio: archivo de audio (audio/wav, etc.).
	•	sampleRate: número (por ejemplo 16000).
	•	durationSeconds: duración estimada.
	•	features: JSON (string) con features opcionales.
	•	meta: JSON (string) con info como:
	•	shardId
	•	episodeId
	•	startTime
	•	endTime
	•	source

Respuesta: objeto JSON con el resultado de análisis del shard (transcripción, emociones, etc.), compatible con el tipo ShardAnalysisResult usado por el frontend.

⸻

7.3 Listar episodios
	•	GET /episodes
Devuelve una lista de resúmenes de episodio.

Cada elemento incluye, por ejemplo:
	•	id: string
	•	createdAt: string (ISO)
	•	title: string | null
	•	note: string | null
	•	shardCount: number
	•	durationSeconds: number | null
	•	Campos agregados sobre emociones (según implementación actual).

Este endpoint es usado por el frontend EVA cuando NEXT_PUBLIC_EVA_DATA_MODE=api para poblar la vista /clips.

⸻

7.4 Detalle de episodio
	•	GET /episodes/{episode_id}
Devuelve:
	•	summary: resumen de episodio (como en /episodes).
	•	shards: lista de shards con:
	•	id, episodeId, startTime, endTime, source
	•	meta, features, analysis

El frontend usa esto para la vista de detalle /clips/[id].

7.4.1 Insights por episodio (v0.4)
	•	GET /episodes/{episode_id}/insights

Quick test:

curl -sS http://localhost:5005/episodes/{episodeId}/insights | python3 -m json.tool

⸻

7.5 Actualizar metadatos de episodio
	•	PATCH /episodes/{episode_id}
Permite actualizar título y nota de un episodio.

Body (JSON):

{
  "title": "Nuevo título opcional",
  "note": "Alguna nota opcional"
}

Solo los campos presentes se actualizan (semántica PATCH). El endpoint devuelve un EpisodeSummaryResponse actualizado.

⸻

7.6 Actualizar shard (ediciones del usuario)
	•	PATCH /shards/{shard_id}

Body (JSON):

{
  "status": "reviewed",
  "userTags": ["tag1", "tag2"],
  "userNotes": "Comentario del usuario",
  "transcriptOverride": "Transcripción corregida por el usuario"
}

El backend:
	•	Lee analysis_json del shard.
	•	Hace merge en analysis_json["user"] con estos campos.
	•	No toca el resto del análisis automático.

Devuelve el shard actualizado con meta, features, analysis.


7.6.1 Publicar/Eliminar shard (A5)
	•	POST /shards/{shard_id}/publish
	•	POST /shards/{shard_id}/delete
	•	GET /me/feed

Ejemplos:

Marcar shard como listo para publicar:

curl -sS -X PATCH http://localhost:5005/shards/{shardId} \
  -H 'Content-Type: application/json' \
  -d '{"status":"readyToPublish"}' | python3 -m json.tool

curl -sS -X POST http://localhost:5005/shards/{shardId}/publish | python3 -m json.tool

curl -sS -X POST http://localhost:5005/shards/{shardId}/delete \
  -H 'Content-Type: application/json' \
  -d '{"reason":"user_deleted"}' | python3 -m json.tool

Feed interno:

curl -sS http://localhost:5005/me/feed | python3 -m json.tool

Ejemplo usando `X-Profile-Id`:

curl -sS -X POST http://localhost:5005/shards/{shardId}/publish \
  -H 'X-Profile-Id: local_profile_2' | python3 -m json.tool


7.6.2 Seed de shards desde export JSON (A6)

Si tienes shards/episodios que existen solo en EVA 1 (IndexedDB/export JSON), puedes “sembrarlos” en la DB local de EVA 2 para que dejen de responder `Shard not found` en:
	•	PATCH /shards/{id}
	•	POST /shards/{id}/publish
	•	GET /me/feed
	•	GET /episodes/{id}/insights

Comando (desde `eva-analysis-service/` con tu venv activo):

python3 -m eva_seed_from_json /ruta/al/export.json

Verificación rápida (reemplaza `<SHARD_ID>`):

curl -sS -X PATCH "http://localhost:5005/shards/<SHARD_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"status":"readyToPublish"}' | python3 -m json.tool

curl -sS -X POST "http://localhost:5005/shards/<SHARD_ID>/publish" | python3 -m json.tool


7.7 Endpoints de comunidad (perfil/progreso/invitaciones)

Estos endpoints operan sobre un perfil local.

- Por defecto el backend usa `local_profile_1`.
- (Opcional) puedes simular otro perfil con el header `X-Profile-Id`.

Ejemplos:

GET /me

curl -sS http://localhost:5005/me | python3 -m json.tool

GET /me/progress

curl -sS http://localhost:5005/me/progress | python3 -m json.tool

GET /me/invitations

curl -sS http://localhost:5005/me/invitations | python3 -m json.tool

POST /invitations

curl -sS -X POST http://localhost:5005/invitations \
  -H 'Content-Type: application/json' \
  -d '{"email":"test@example.com"}' | python3 -m json.tool

Ejemplo usando `X-Profile-Id`:

curl -sS http://localhost:5005/me \
  -H 'X-Profile-Id: local_profile_2' | python3 -m json.tool

⸻

8. Relación con el frontend EVA

Cuando el frontend está configurado con:

NEXT_PUBLIC_EVA_ANALYSIS_MODE=local
NEXT_PUBLIC_EVA_LOCAL_ANALYSIS_BASE=http://localhost:5005
NEXT_PUBLIC_EVA_DATA_MODE=api

El flujo típico es:
	1.	EVA 1 (frontend) detecta un momento intenso y manda el audio a POST /analyze-shard.
	2.	EVA 2 analiza, guarda y responde con el análisis del shard.
	3.	EVA 1 agrupa shards en episodios, los muestra en /clips y /clips/[id].
	4.	Cuando el usuario edita título/nota del episodio o etiquetas/notas del shard:
	•	EVA 1:
	•	Guarda cambios localmente en IndexedDB.
	•	Si EVA_DATA_MODE=api, también llama PATCH /episodes/{id} y PATCH /shards/{id} para sincronizar con EVA 2.

⸻

9. Producción

Para desplegar en producción:
	•	Usa un servidor WSGI/ASGI robusto (por ejemplo gunicorn + uvicorn worker).
	•	Configura una base de datos duradera (Postgres recomendado) mediante EVA_DB_URL.
	•	Protege el servicio detrás de un reverse proxy (Nginx, Caddy, etc.).
	•	Asegura las claves de API y variables sensibles mediante un gestor de secretos.

⸻

10. Licencia y notas
	•	Ajusta esta sección según la licencia que quieras usar (MIT, Propietaria, etc.).
	•	Añade aquí cualquier nota adicional sobre uso, privacidad o términos específicos del proyecto HGI/EVA.

---



# Playbook EVA 1 → EVA 2 (lado backend)

## 1. Prerrequisitos

1. Tener el repo de EVA 2 en:
   - `/Users/hildebertoalemanguerrero/HGI - Christmas/CascadeProjects/windsurf-project/eva-analysis-service`
2. Tener el virtualenv activo:
   - `.venv` (en esa carpeta)
3. Tener EVA 2 corriendo y accesible en:
   - `http://localhost:5005`

---

## 2. Importar el export de EVA 1

1. Ejecuta el seed (copy/paste):

```bash
cd "/Users/hildebertoalemanguerrero/HGI - Christmas/CascadeProjects/windsurf-project/eva-analysis-service"
source .venv/bin/activate
python3 -m eva_seed_from_json "../eva/tmp/eva1-shards-export.json"
```

2. Cómo interpretar el JSON de salida (máximo 3 líneas):
- **`shardsInserted`**: shards nuevos que EVA 2 no tenía y fueron creados.
- **`shardsUpdated`**: shards existentes que fueron actualizados (idempotencia).
- **`shardsSkipped`**: entradas sin `id`/`shardId` válido (no se importan).

---

## 3. Verificar shards con el debug script

1. Ejecuta el script:

```bash
cd "/Users/hildebertoalemanguerrero/HGI - Christmas/CascadeProjects/windsurf-project/eva-analysis-service"
source .venv/bin/activate
python3 eva_debug_check_shards.py
```

2. Cómo usarlo:
- Edita la lista `DEBUG_SHARD_IDS = [...]` dentro de `eva_debug_check_shards.py`.
- Pon ahí los shard IDs que quieras auditar (los que EVA 1 dice que exportó).
- Re-ejecuta el script y revisa `exists`, `publishState`, `meta_status`, `emotion_headline`, etc.

---

## 4. Marcar shards como listos y publicar

### 4.1 Marcar como listo (`readyToPublish`)

```bash
curl -sS -X PATCH "http://localhost:5005/shards/<ID>" \
  -H 'Content-Type: application/json' \
  -d '{"status":"readyToPublish"}' | python3 -m json.tool
```

### 4.2 Publicar

```bash
curl -sS -X POST "http://localhost:5005/shards/<ID>/publish" | python3 -m json.tool
```

### 4.3 Interpretación rápida de respuestas

- **`{"detail":"Shard not found"}`**
  - EVA 2 no tiene ese shard en su DB.
  - Solución: confirma que el shard aparece en el export de EVA 1 y re-ejecuta el seed. Si no aparece, EVA 1 todavía no lo sincronizó.
- **`{"detail":"not_ready_to_publish"}`**
  - El shard existe, pero **no cumple** la regla de publicación (no está listo).
  - Solución: hacer el `PATCH` a `readyToPublish` (o usar `force` si aplica a tu flujo).
- **Caso exitoso**
  - Respuesta **200** con el shard (modelo `ShardWithAnalysisResponse`), y típicamente `publishState` pasa a `"published"`.

> Opcional (si necesitas otro perfil): agrega header `X-Profile-Id: <profile>` en las requests.

---

## 5. Verificar feed

1. Consultar feed:

```bash
curl -sS "http://localhost:5005/me/feed" | python3 -m json.tool
```

2. Qué buscar:
- Dentro de `items`, busca un objeto cuyo `shardId` coincida con el shard que publicaste.
- Si aparece, el publish creó/actualizó el registro correspondiente en el feed para ese perfil.

---

## 6. Errores comunes y soluciones rápidas

- **Servidor no responde / puerto incorrecto**
  - Verifica que EVA 2 esté corriendo en `http://localhost:5005`.
  - Revisa `GET /health` o `GET /docs`.
- **`Shard not found` incluso después del seed**
  - Confirma que el export de EVA 1 realmente incluye ese `shardId`.
  - Re-ejecuta `python3 -m eva_seed_from_json "../eva/tmp/eva1-shards-export.json"`.
  - Corre `python3 eva_debug_check_shards.py` con ese ID para confirmar `exists: true/false`.
- **Export sin shards (seed no inserta nada)**
  - El JSON puede estar vacío o incompleto (ej. `episodes: []`).
  - Solución: regenerar el export en EVA 1 y re-seed.
- **Publish siempre da `not_ready_to_publish`**
  - Falta marcar el shard como listo:
    - `PATCH /shards/<ID>` con `{"status":"readyToPublish"}`
- **Feed no muestra el shard publicado**
  - Asegúrate de estar usando el mismo perfil (por defecto `local_profile_1`).
  - Si usas `X-Profile-Id` en publish, úsalo también al leer el feed.
