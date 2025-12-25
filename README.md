# eva-analysis-service (EVA)

Microservicio local para analizar shards de audio generados por el frontend **EVA**.

Este servicio está pensado para correr en el Mac del usuario (local), con modelos almacenados en un disco externo.

## Endpoints

- `GET /health`
- `POST /analyze-shard` (multipart/form-data)

## Requisitos

- Python 3.11+

## Instalación

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuración (.env)

Crea un archivo `.env` en la raíz del proyecto (puedes copiar `.env.example`).

Ejemplo (Mac con disco externo):

```bash
EVA_MODEL_ROOT=/Volumes/Hildecornia/vistadev/HGI/Modelos Locales HGI
EVA_DEVICE=cpu
```

Dentro de `EVA_MODEL_ROOT`, este servicio asume esta convención:

- `${EVA_MODEL_ROOT}/whisper` → modelo de transcripción
- `${EVA_MODEL_ROOT}/emotion-ser` → modelo SER (emociones por voz)
- `${EVA_MODEL_ROOT}/llm` → opcional

**Nota:** la ruta `/Volumes/...` NO está hardcodeada en el código. Solo es un ejemplo.

## Ejecutar

```bash
uvicorn src.main:app --host 0.0.0.0 --port 5005 --reload
```

## Conectar con el frontend EVA

En el proyecto Next.js (EVA frontend), configura:

```bash
NEXT_PUBLIC_EVA_ANALYSIS_MODE=local
NEXT_PUBLIC_EVA_LOCAL_ANALYSIS_BASE=http://localhost:5005
```

## Probar con curl

```bash
curl -X POST "http://localhost:5005/analyze-shard" \
  -F "audio=@./sample.wav;type=audio/wav" \
  -F "sampleRate=44100" \
  -F "durationSeconds=8" \
  -F 'features={"rms":0.1,"zcr":0.05,"spectralCentroid":1200,"intensity":0.4}' \
  -F 'meta={"shardId":"test","source":"mic","startTime":0,"endTime":8}'
```

La respuesta debe seguir el contrato `ShardAnalysisResult` esperado por el frontend.




# EVA Analysis Service (Backend)

Servicio de análisis de audio para EVA (Human Grounded Intelligence).  
Expone una API HTTP que recibe **shards** de audio (pequeños fragmentos), calcula rasgos de la señal, transcribe con Whisper y genera un análisis emocional y semántico.

---

## 1. Tecnologías principales

- **Python 3.10+**
- **FastAPI** + **Uvicorn**
- **faster-whisper** (transcripción local / GPU o CPU)
- **OpenAI SDK** (análisis semántico con LLM)
- **Pydantic** (esquemas y validación)
- Se ejecuta típicamente en `http://localhost:5005`

---

## 2. Estructura del proyecto

Carpetas principales:

- `src/`
  - `main.py`  
    Punto de entrada del servidor FastAPI. Define:
    - `GET /health`
    - `POST /analyze-shard`
    - Inicialización de modelos (Whisper, Emotion, Semantic) en `app.state`.
  - `config.py`  
    Carga configuración desde variables de entorno:
    - `EVA_MODEL_ROOT`
    - `EVA_WHISPER_MODEL_ROOT`
    - flags como `EVA_USE_REAL_WHISPER`, etc.
  - `models/`
    - `whisper_model.py`  
      Wrapper alrededor de `faster-whisper`.  
      Hace **lazy-load** del modelo (se carga en el primer request) y lo cachea en memoria.
    - `emotion_model.py`  
      Modelo de emociones (por ahora lógica determinista / stub).
    - `semantic_model.py`  
      Cliente de OpenAI que recibe el transcript y devuelve:
      - `summary`
      - `topics[]`
      - `momentType`
      - `flags.needsFollowup`
      - `flags.possibleCrisis`
  - `schemas/analysis.py`  
    Define los modelos Pydantic que se devuelven al frontend:
    - `ShardAnalysisResult`
    - `SignalFeaturesBlock`
    - `EmotionBlock`
    - `SemanticBlock`
    - `SemanticFlags`
- `requirements.txt`  
  Lista de dependencias (FastAPI, Uvicorn, faster-whisper, openai, etc.)

---

## 3. Prerrequisitos

1. **Python 3.10+** instalado.
2. (Opcional, recomendado) Crear un entorno virtual:

   ```bash
   python -m venv .venv
   source .venv/bin/activate   # macOS / Linux
   # o en Windows:
   # .venv\Scripts\activate

	3.	Instalar dependencias:

pip install -r requirements.txt


	4.	Tener una clave de OpenAI válida (para el análisis semántico).

⸻

4. Variables de entorno

Crear un archivo .env.local en la raíz de eva-analysis-service (NO subir al repo) con algo como:

# Ruta base donde viven los modelos de EVA (si se usa)
EVA_MODEL_ROOT=/ruta/a/EVA_MODELS

# Ruta donde está el modelo de Whisper para faster-whisper
# Ejemplo:
# EVA_WHISPER_MODEL_ROOT=/Users/tu_usuario/vistedev/HGI/EVA_MODELS/whisper
EVA_WHISPER_MODEL_ROOT=/ruta/a/EVA_MODELS/whisper

# Activa el uso real de faster-whisper (1 = on, 0 = off)
EVA_USE_REAL_WHISPER=1

# Clave de OpenAI para el análisis semántico
OPENAI_API_KEY=sk-...

# (Opcional) device: cpu / cuda, etc. según config.py
# EVA_DEVICE=cpu

Importante: no subir .env.local ni la API key al repositorio.

⸻

5. Levantar el servidor

Desde la carpeta eva-analysis-service:

uvicorn src.main:app --host 0.0.0.0 --port 5005 --reload

	•	--reload: recarga automática al cambiar código (modo desarrollo).
	•	El servidor quedará accesible en:
http://localhost:5005

⸻

6. Endpoint /health

Método

GET /health

Ejemplo de respuesta

{
  "status": "ok",
  "modelRootAvailable": true,
  "whisperLoaded": true,
  "emotionModelLoaded": true,
  "timestamp": "2025-12-25T09:55:05.372189+00:00"
}

	•	whisperLoaded y emotionModelLoaded se ponen en true después del primer análisis exitoso (lazy-load real).

⸻

7. Endpoint /analyze-shard

Método

POST /analyze-shard (multipart/form-data)

Campos esperados
	•	audio: archivo de audio binario (ej. WAV/PCM 16-bit mono).
	•	sampleRate: número (ej. 44100).
	•	durationSeconds: número (segundos de duración del shard).
	•	features: JSON con rasgos básicos de la señal, por ejemplo:

{
  "rms": 0.0014,
  "zcr": 180,
  "spectralCentroid": 63.72,
  "intensity": 1
}


	•	meta: JSON con metadatos del shard, por ejemplo:

{
  "shardId": "SJ2rCgoOdf2LR056sBIAa",
  "source": "mic",
  "startTime": 1.16,
  "endTime": 12.35
}



Flujo interno del análisis
	1.	Carga de audio temporal en un archivo (directorio de trabajo).
	2.	Whisper (faster-whisper):
	•	Si EVA_USE_REAL_WHISPER=1 y encuentra el modelo en EVA_WHISPER_MODEL_ROOT, transcribe el audio.
	•	Detecta idioma (language) y probabilidad (language_probability).
	•	Devuelve transcript, transcriptLanguage, transcriptionConfidence.
	•	Si no hay modelo o hay error, se cae a transcript vacío.
	3.	EmotionModel:
	•	Usa los features (rms, peak, etc.) y el audio para estimar:
	•	primaryEmotion
	•	emotionLabels[]
	•	valence (positivo, neutral, negativo)
	•	arousal (alto / medio / bajo)
	•	prosodyFlags (risa, llanto, shouting, tensión…)
	4.	SemanticModel (OpenAI):
	•	Recibe:
	•	transcript
	•	language
	•	signalFeatures completos
	•	Construye un prompt sistemático y llama al modelo (por defecto gpt-4.1-mini) con response_format={"type": "json_object"}.
	•	Devuelve:
	•	summary (1–3 frases)
	•	topics[] (2–5 palabras clave)
	•	momentType (check-in, desahogo, crisis, recuerdo, meta, agradecimiento, otro)
	•	flags.needsFollowup / flags.possibleCrisis (booleans)
	•	Si falla o no hay OPENAI_API_KEY, devuelve un SemanticBlock vacío seguro (summary=””, topics=[], momentType=“otro”, flags=false).
	5.	El backend empaqueta todo en un ShardAnalysisResult y lo devuelve al frontend.

Ejemplo de respuesta completa

{
  "transcript": "Me siento pensativo y un poco preocupado, pero feliz por estar con mi familia, por ser hoy un día tan especial, doy gracias a Dios por todo eso, gracias Señor.",
  "transcriptLanguage": "es",
  "transcriptionConfidence": 0.96,
  "language": "es",
  "emotion": {
    "primary": "alegria",
    "valence": "positivo",
    "activation": "alto",
    "scores": [
      { "label": "alegria", "score": 0.6 },
      { "label": "neutro", "score": 0.4 }
    ]
  },
  "signalFeatures": {
    "rms": 0.0024,
    "peak": 0.8877,
    "centerFrequency": 64.02,
    "zcr": 96.0
  },
  "semantic": {
    "summary": "La persona expresa sentimientos mixtos de preocupación y felicidad, destacando la importancia de estar con su familia en un día especial y mostrando gratitud a Dios.",
    "topics": ["familia", "gratitud", "preocupación", "día especial"],
    "momentType": "agradecimiento",
    "flags": {
      "needsFollowup": false,
      "possibleCrisis": false
    }
  },
  "primaryEmotion": "alegria",
  "emotionLabels": [
    { "label": "alegria", "score": 0.6 },
    { "label": "neutro", "score": 0.4 }
  ],
  "valence": "positivo",
  "arousal": "alto",
  "prosodyFlags": {
    "laughter": "none",
    "crying": "none",
    "shouting": "present",
    "sighing": "none",
    "tension": "light"
  },
  "analysisSource": "local",
  "analysisMode": "automatic",
  "analysisVersion": "0.1.0-local",
  "analysisAt": "2025-12-25T11:53:56.000000Z"
}


⸻

8. Apagar el servidor

En la terminal donde corre uvicorn:
	•	Presiona CTRL + C una vez.
	•	Espera el mensaje de “Shutting down / Application shutdown complete”.

Listo, backend apagado.

# EVA – Interfaz de análisis emocional y semántico

Aplicación web (Next.js + React) para visualizar y revisar **clips de audio** analizados por el backend `eva-analysis-service`.

Permite:
- Grabar o subir audio (shards).
- Enviar shards al backend `/analyze-shard`.
- Ver transcripción, emoción principal, rasgos de la señal.
- Ver el **análisis semántico** (resumen, temas, tipo de momento, flags).
- Añadir etiquetas y notas manuales.

---

## 1. Tecnologías

- **Next.js 16 (Turbopack)**
- **React 18**
- **TypeScript**
- **Tailwind CSS**
- Almacenamiento local de shards (IndexedDB) mediante store propio.

---

## 2. Estructura del proyecto

Carpetas relevantes:

- `src/app/`
  - `page.tsx`  
    Pantalla principal (lista de clips, grabación, etc.).
  - `clips/[id]/page.tsx`  
    Página de **detalle del clip**:
    - Waveform (placeholder controlado por flag).
    - Tarjeta de “Análisis semántico”.
    - Transcripción.
    - Lectura emocional.
    - Rasgos de la señal.
    - Etiquetas sugeridas dinámicas.
- `src/components/emotion/`
  - `ShardDetailPanel.tsx`  
    Componente que renderiza el detalle del shard (lo que vemos en las capturas).
    - Muestra `transcript`, emoción, features, análisis semántico, etiquetas sugeridas.
- `src/lib/api/`
  - `evaAnalysisClient.ts`  
    Cliente HTTP hacia el backend `eva-analysis-service`:
    - `GET /health`
    - `POST /analyze-shard`
    - Maneja timeout mediante `AbortController`.
- `src/lib/store/`
  - `EmoShardStore.ts`  
    Store que persiste shards y análisis en IndexedDB.
- `src/types/emotion.ts`  
  Tipos TypeScript compartidos (EmoShard, SemanticAnalysis, ProsodyFlags, etc.).

---

## 3. Variables de entorno

Se usan variables tipo `NEXT_PUBLIC_...` para configuración en el navegador.

Crea un archivo `.env.local` en la raíz de `eva` (no subir al repo), por ejemplo:

```env
# URL del backend de análisis (FastAPI / uvicorn)
# Si en el código hay un BASE_URL fijo, respétalo; si hay env, usar algo así:
NEXT_PUBLIC_EVA_ANALYSIS_BASE_URL=http://localhost:5005

# Mostrar u ocultar el placeholder del waveform MVP
# 1 = mostrar, cualquier otra cosa = ocultar
NEXT_PUBLIC_SHOW_WAVEFORM_MVP=0

Nota: si la URL del backend está hardcodeada en evaAnalysisClient.ts, puedes documentar eso y cambiarla ahí cuando sea necesario.

⸻

4. Instalación y scripts

Desde la carpeta eva:
	1.	Instalar dependencias:

npm install
# o pnpm install / yarn install según tu preferencia


	2.	Entorno de desarrollo:

npm run dev

	•	Abre http://localhost:3000 en el navegador.
	•	Asegúrate de que el backend esté corriendo en http://localhost:5005.

	3.	Build de producción:

npm run build
npm start


	4.	Lint:

npm run lint



⸻

5. Flujo de análisis en el frontend
	1.	El usuario graba o selecciona un shard de audio.
	2.	El frontend construye un FormData con:
	•	audio (blob del audio).
	•	sampleRate.
	•	durationSeconds.
	•	features (JSON con rms, zcr, spectralCentroid, intensity).
	•	meta (JSON con shardId, source, startTime, endTime).
	3.	Llama a evaAnalysisClient.analyzeShardAudioSafe(...), que:
	•	Hace fetch a POST /analyze-shard.
	•	Usa un AbortController con DEFAULT_TIMEOUT_MS = 60000 (60s).
	4.	Si la respuesta es 200 OK, se parsea como ShardAnalysisResult y se guarda en EmoShardStore.
	5.	La UI se actualiza:
	•	Transcripción debajo del audio.
	•	Lectura emocional (emoción principal, valencia, activación, lista top 5 emociones).
	•	Análisis semántico:
	•	summary → párrafo.
	•	topics[] → chips.
	•	momentType → badge coloreado (check-in, desahogo, crisis, recuerdo, meta, agradecimiento, otro).
	•	flags → banner informativo si needsFollowup o possibleCrisis son true.
	•	Etiquetas sugeridas:
	•	Construidas dinámicamente a partir de:
	•	semantic.topics
	•	emoción principal
	•	activación (arousal)
	•	prosodia (shouting, etc.)
	•	Campos para tus etiquetas y notas manuales.

⸻

6. Waveform MVP

El contenedor del waveform es solo un placeholder controlado por la flag:

NEXT_PUBLIC_SHOW_WAVEFORM_MVP=1   # para mostrar
NEXT_PUBLIC_SHOW_WAVEFORM_MVP=0   # para ocultar (default recomendado)

En clips/[id]/page.tsx el bloque se renderiza solo si showWaveformMvp === true, por lo que puedes mantener la UI limpia hasta conectar un waveform real (Wavesurfer, etc.) más adelante.

⸻

7. Etiquetas sugeridas

En ShardDetailPanel.tsx se usa un helper buildSuggestedTags(shard) que:
	•	Toma shard.semantic.topics y los convierte en chips.
	•	Añade tags basadas en:
	•	emoción principal (alegría, neutro, etc.)
	•	nivel de activación (ej. alta activación)
	•	prosodia (voz elevada si hay shouting, etc.)
	•	Si la lista queda vacía, la sección “Etiquetas sugeridas” no se muestra.

⸻

8. Apagar el frontend

En la terminal donde está corriendo npm run dev:
	•	Presiona CTRL + C para detener el servidor de Next.js.
	•	Cierra también la ventana del navegador si quieres.

Listo, frontend apagado.
