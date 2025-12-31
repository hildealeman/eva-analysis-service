# EVA Analysis Service – API Contract

**Versión del contrato:** v0.5

Este documento define el **contrato oficial** (API pública) del servicio **EVA Analysis Service** (`eva-analysis-service`) tal como existe en el código actual.

- **Stack:** FastAPI + SQLModel
- **Persistencia:** `EVA_DB_URL` (por defecto `sqlite:///./eva.db`)
- **Objetivo:** recibir *shards* de audio, analizarlos (transcripción/emoción/semántica), persistir resultados y exponer endpoints de lectura/edición para dashboards y UI.

---

## Tabla rápida de endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Health-check del servicio y estado de carga de modelos. |
| POST | `/analyze-shard` | Analiza un shard de audio (multipart), responde con el análisis y persiste en DB. |
| GET | `/me` | Devuelve el perfil local y resúmenes de progreso e invitaciones (capa comunidad). |
| GET | `/me/progress` | Devuelve progreso de hoy y el histórico de los últimos 30 días (capa comunidad). |
| GET | `/me/invitations` | Lista invitaciones creadas por el perfil actual (capa comunidad). |
| POST | `/invitations` | Crea una invitación si el perfil tiene invitaciones restantes (capa comunidad). |
| GET | `/episodes` | Lista episodios con stats agregadas (conteo shards, duración aprox, emoción principal). |
| GET | `/episodes/insights` | Insights globales para dashboards (totales, top tags/status/emociones, último episodio). |
| GET | `/episodes/{episode_id}/insights` | Insights por episodio (stats + resumen emocional + key moments). |
| GET | `/episodes/{episode_id}` | Devuelve el detalle de un episodio (summary + shards con analysis). |
| PATCH | `/episodes/{episode_id}` | Actualiza `title` y/o `note` de un episodio (semántica PATCH). |
| PATCH | `/shards/{shard_id}` | Actualiza campos de usuario en `analysis.user` (status/tags/notes/transcriptOverride). |
| POST | `/shards/{shard_id}/publish` | Marca un shard como publicado (ciclo de vida básico). |
| POST | `/shards/{shard_id}/delete` | Borrado lógico de un shard (razón opcional). |
| GET | `/me/feed` | Feed interno del perfil actual (shards publicados por el usuario). |

---

## Convenciones generales

- **Base URL (dev):** `http://localhost:5005`
- **Formato de fechas:** ISO-8601 en UTC cuando aplique.
- **Compatibilidad:** el servicio agrega campos de forma **aditiva**; no elimina campos existentes.
- **Persistencia JSON:** `meta_json`, `features_json` y `analysis_json` se almacenan como JSON (dict) en la DB.

---

## Endpoints

### 1) GET `/health`

**Descripción:**
Health-check básico y flags de disponibilidad de modelos.

**Request:**
- Sin parámetros.

**Response 200 (JSON):**

```ts
type HealthResponse = {
  status: 'ok' | 'degraded';
  modelRootAvailable: boolean;
  whisperLoaded: boolean;
  emotionModelLoaded: boolean;
  timestamp: string; // ISO datetime
};
```

---

### 2) POST `/analyze-shard`

**Descripción:**
Analiza un shard de audio. Devuelve el resultado (transcripción/emoción/semántica) y persiste `Episode` + `Shard` en DB.

**Request:**
- **Content-Type:** `multipart/form-data`

```ts
type AnalyzeShardRequest = {
  // multipart/form-data
  audio: File;               // WAV soportado (ver notas)
  sampleRate: string;        // se parsea a número (ej: "16000")
  durationSeconds: string;   // se parsea a número (ej: "12.5")
  features?: string;         // JSON string. Default: "{}"
  meta?: string;             // JSON string. Default: "{}"
};

type ShardMetaInput = {
  shardId?: string;
  episodeId?: string;
  startTime?: number;
  endTime?: number;
  source?: string;
};

type ShardFeaturesInput = {
  rms?: number;
  zcr?: number;
  spectralCentroid?: number;
  intensity?: number;
};
```

**Notas del audio (validación actual):**
- Se valida header WAV (`RIFF`/`WAVE`).
- Se rechazan `content-type` fuera de:
  - `audio/wav`, `audio/x-wav`, `audio/wave`, `audio/vnd.wave`

**Response 200 (`ShardAnalysisResult`):**

```ts
type EmotionDistribution = {
  primary?: string;
  valence?: 'positive' | 'neutral' | 'negative';
  activation?: 'low' | 'medium' | 'high';
  distribution?: Record<string, number>; // probabilidades [0..1]
  headline?: string | null;
  explanation?: string | null;
};

type EmotionLegacy = {
  primary?: string;  // enum interno (ej: 'enojo', 'neutro', ...)
  valence?: string;  // legacy ES: 'negativo'|'neutral'|'positivo'
  activation?: string; // legacy ES: 'bajo'|'medio'|'alto'
  scores?: { label: string; score: number }[];
};

type SignalFeatures = {
  rms?: number | null;
  peak?: number | null;
  centerFrequency?: number | null;
  zcr?: number | null;
};

type SemanticFlags = {
  needsFollowup: boolean;
  possibleCrisis: boolean;
};

type SemanticBlock = {
  summary?: string | null;
  topics?: string[] | null;
  momentType?: string | null;
  flags?: SemanticFlags | null;
};

type ShardAnalysisResult = {
  transcript?: string | null;
  transcriptLanguage?: string | null;
  transcriptionConfidence?: number | null;

  // campos enriquecidos (aditivos)
  language?: string | null;
  emotion?: EmotionDistribution | null;      // NUEVO: contrato estable para UI
  emotionLegacy?: EmotionLegacy | null;      // legacy preservado
  signalFeatures?: SignalFeatures | null;
  semantic?: SemanticBlock | null;

  // legacy existentes (no se eliminan)
  primaryEmotion?: string | null;
  emotionLabels?: { label: string; score: number }[] | null;
  valence?: string | null;   // legacy ES
  arousal?: string | null;   // legacy ES
  prosodyFlags?: {
    laughter?: 'none' | 'light' | 'strong' | null;
    crying?: 'none' | 'present' | null;
    shouting?: 'none' | 'present' | null;
    sighing?: 'none' | 'present' | null;
    tension?: 'none' | 'light' | 'high' | null;
  } | null;

  analysisSource: 'local' | 'cloud';
  analysisMode: 'automatic' | 'manual';
  analysisVersion?: string | null;
  analysisAt: string; // ISO datetime
};
```

**Errores relevantes:**
- **503** `model_root_not_available`: cuando `EVA_MODEL_ROOT`/ruta de modelos no está disponible.
- **400** `invalid_audio_type`: `content-type` no soportado.
- **400** `invalid_parameters`: `sampleRate` o `durationSeconds` no numéricos / <= 0.
- **400** `invalid_wav`: archivo no es WAV válido o no se puede validar header.
- **500** `internal_error`: error no controlado durante el pipeline.

---

### 3) GET `/episodes`

**Descripción:**
Lista episodios con información agregada básica.

**Response 200:**

```ts
type EpisodeSummary = {
  id: string;
  createdAt: string; // ISO datetime
  title?: string | null;
  note?: string | null;
  shardCount: number;
  durationSeconds?: number | null;
  primaryEmotion?: string | null;
  valence?: string | null;
  arousal?: string | null;
};

type ListEpisodesResponse = EpisodeSummary[];
```

---

### 4) GET `/episodes/insights`

**Descripción:**
Resumen global para dashboards (totales, top tags/status/emociones, último episodio).

**Response 200:**

```ts
type TagStat = { tag: string; count: number };
type StatusStat = { status: string; count: number };
type EmotionStat = { emotion: string; count: number };

type EpisodeInsightsResponse = {
  totalEpisodes: number;
  totalShards: number;
  totalDurationSeconds?: number | null;
  tags: TagStat[];
  statuses: StatusStat[];
  emotions: EmotionStat[];
  lastEpisode?: EpisodeSummary | null;
};
```

---

### 5) GET `/episodes/{episode_id}`

**Descripción:**
Devuelve el detalle de un episodio: `summary` + lista de `shards` con `meta/features/analysis`.

**Path params:**
- `episode_id: string`

**Response 200:**

```ts
type ShardWithAnalysis = {
  id: string;
  episodeId?: string | null;
  startTime?: number | null;
  endTime?: number | null;
  source?: string | null;
  publishState?: 'draft' | 'reviewed' | 'readyToPublish' | 'published' | null;
  deleted?: boolean | null;
  deletedReason?: string | null;
  deletedAt?: string | null; // ISO datetime
  meta: Record<string, any>;
  features: Record<string, any>;
  analysis: Record<string, any>; // incluye analysis.emotion si existe
};

type EpisodeDetail = {
  summary: EpisodeSummary;
  shards: ShardWithAnalysis[];
};
```

**Errores relevantes:**
- **404** `Episode not found` si no existe.

---

### 6) PATCH `/episodes/{episode_id}`

**Descripción:**
Actualiza `title` y/o `note` de un episodio (semántica PATCH: solo campos presentes).

**Path params:**
- `episode_id: string`

**Request body (JSON):**

```ts
type EpisodeUpdateRequest = {
  title?: string | null;
  note?: string | null;
};
```

**Response 200:**
- Devuelve `EpisodeSummary`.

**Errores relevantes:**
- **404** `Episode not found` si no existe.

---

### 7) PATCH `/shards/{shard_id}`

**Descripción:**
Actualiza campos de usuario del shard bajo `analysis.user` (merge; ignora `null/undefined`).

**Path params:**
- `shard_id: string`

**Request body (JSON):**

```ts
type ShardUpdateRequest = {
  status?: string | null;
  userTags?: string[] | null;
  userNotes?: string | null;
  transcriptOverride?: string | null;
};
```

**Response 200:**
- Devuelve `ShardWithAnalysis` (con `analysis` actualizado; incluye `analysis.user`).

**Errores relevantes:**
- **404** `Shard not found` si no existe.

---

### 8) POST `/shards/{shard_id}/publish`

**Descripción:**
Marca un shard como publicado (ciclo de vida básico). No elimina ni reescribe `analysis.user` ni `analysis.emotion`.

**Path params:**
- `shard_id: string`

**Request body (JSON):**

```ts
type ShardPublishRequest = {
  force?: boolean; // default false
};
```

**Comportamiento:**
- Si el shard no existe: **404**.
- Si `deleted == true`: **400** (no se puede publicar un shard eliminado).
- Si ya está en `publishState == "published"` y `force == false`: **200** (sin cambios).
- Caso normal: setea `publishState = "published"` y devuelve `ShardWithAnalysis`.
- Regla A5: si `analysis.user.status != "readyToPublish"` y `force == false`: **400** `{ "detail": "not_ready_to_publish" }`.

**Response 200:**
- Devuelve `ShardWithAnalysis` actualizado.

---

### 9) POST `/shards/{shard_id}/delete`

**Descripción:**
Borrado lógico de un shard. No hace hard-delete.

**Path params:**
- `shard_id: string`

**Request body (JSON, opcional):**

```ts
type ShardDeleteRequest = {
  reason?: string; // default "user_deleted"
};
```

**Comportamiento:**
- Soft-delete del shard:
  - `deleted = true`
  - `deletedReason = reason` (o `"user_deleted"`)
  - `deletedAt = now (UTC ISO)`
- Además remueve el shard del feed del perfil actual (si existía).
- Devuelve `ShardWithAnalysis` actualizado.

**Response 200:**
- Devuelve `ShardWithAnalysis` actualizado.

---

## Modelos de datos (resumen)

### EpisodeSummary
Ver `GET /episodes`.

### EpisodeDetail
Ver `GET /episodes/{episode_id}`.

### ShardWithAnalysis
Ver `GET /episodes/{episode_id}` y `PATCH /shards/{shard_id}`.

### Bloque emocional normalizado (`analysis.emotion`)

```ts
type EmotionDistribution = {
  primary?: string;
  valence?: 'positive' | 'neutral' | 'negative';
  activation?: 'low' | 'medium' | 'high';
  distribution?: Record<string, number>; // [0..1]
  headline?: string | null;
  explanation?: string | null;
};
```

### Legacy emocional (compatibilidad)

- `analysis.emotionLegacy` (cuando la respuesta es `ShardAnalysisResult`)
- `analysis.primaryEmotion`, `analysis.emotionLabels`, `analysis.valence`, `analysis.arousal`

---

## Flujos típicos

### Flujo: escucha en tiempo real
1. Cliente captura audio y lo segmenta en shards.
2. Por cada shard, llama `POST /analyze-shard` (multipart).
3. El backend analiza y persiste `Episode` + `Shard` en DB.
4. Cliente consulta `GET /episodes` y `GET /episodes/{episode_id}` para reconstruir sesiones.

### Flujo: edición manual
1. UI llama `PATCH /episodes/{episode_id}` para actualizar `title` y/o `note`.
2. UI llama `PATCH /shards/{shard_id}` para actualizar `status/userTags/userNotes/transcriptOverride` bajo `analysis.user`.

---

## Versionado y compatibilidad

- **Contrato v0.5**.
- Cambios futuros deben ser **aditivos**: agregar campos, no eliminar.
- Para compatibilidad hacia atrás, se mantienen:
  - Campos legacy: `primaryEmotion`, `emotionLabels`, `valence`, `arousal`.
  - `emotionLegacy` junto al nuevo bloque estable `emotion`.

---

## 10) GET `/episodes/{episode_id}/insights` (v0.4)

**Descripción:**
Devuelve un resumen *solo lectura* con estadísticas del episodio, un resumen agregado de emoción y una lista de hasta 5 “key moments” sugeridos para revisión.

**Response 200:**

```ts
type EpisodeInsightsResponse = {
  episodeId: string;
  stats: {
    totalShards: number;
    durationSeconds: number | null;
    shardsWithEmotion: number;
    firstShardAt: number | null;
    lastShardAt: number | null;
  };
  emotionSummary: {
    primaryCounts: Record<string, number>;
    valenceCounts: Record<string, number>;
    activationCounts: Record<string, number>;
  };
  keyMoments: {
    shardId: string;
    episodeId: string;
    startTime: number | null;
    endTime: number | null;
    reason: 'highestIntensity' | 'strongNegative' | 'strongPositive';
    emotion: {
      primary: string | null;
      valence: 'positive' | 'neutral' | 'negative' | null;
      activation: 'low' | 'medium' | 'high' | null;
      headline: string | null;
    };
    transcriptSnippet: string | null;
  }[];
};
```

**Ejemplo (200, resumido):**

```json
{
  "episodeId": "ep-1",
  "stats": {
    "totalShards": 12,
    "durationSeconds": 98.5,
    "shardsWithEmotion": 12,
    "firstShardAt": 0.0,
    "lastShardAt": 98.5
  },
  "emotionSummary": {
    "primaryCounts": {"enojo": 3, "neutro": 5},
    "valenceCounts": {"negative": 4, "neutral": 6, "positive": 2},
    "activationCounts": {"low": 2, "medium": 6, "high": 4}
  },
  "keyMoments": [
    {
      "shardId": "shard-7",
      "episodeId": "ep-1",
      "startTime": 54.0,
      "endTime": 62.0,
      "reason": "highestIntensity",
      "emotion": {
        "primary": "enojo",
        "valence": "negative",
        "activation": "high",
        "headline": "Alza de voz."
      },
      "transcriptSnippet": "..."
    }
  ]
}
```

---

## 11) Publicación de Emo-Shards + Feed interno (A5, v0.5)

Esta sección habilita el flujo de “publicar” un shard (para revisión/curación) y listarlo en un feed interno del perfil actual.

### 11.1 Reglas

- **Perfil actual:** se resuelve por header `X-Profile-Id` (default `local_profile_1`).
- **Regla ética:** solo se publica si `analysis.user.status == "readyToPublish"`.
- **Excepción:** si se usa `force=true` en `POST /shards/{id}/publish`, se permite publicar aunque no esté `readyToPublish`.
- **Idempotencia:** publicar el mismo `shardId` para el mismo perfil no crea duplicados activos.

### 11.2 GET `/me/feed`

**Response 200:**

```ts
type FeedResponse = {
  items: {
    id: string; // PublishedShard.id
    shardId: string;
    episodeId: string;
    publishedAt: string; // ISO datetime
    startTimeSec?: number | null;
    endTimeSec?: number | null;
    status?: string | null;
    userTags: string[];
    emotion: {
      primary?: string | null;
      valence?: 'positive' | 'neutral' | 'negative' | null;
      activation?: 'low' | 'medium' | 'high' | null;
      headline?: string | null;
      intensity?: number | null;
    };
    transcriptSnippet?: string | null;
  }[];
};
```

### 11.3 POST `/shards/{shard_id}/publish`

- Si el shard no existe: **404**.
- Si está eliminado: **400**.
- Si no está `readyToPublish` y `force=false`: **400** `not_ready_to_publish`.

### 11.4 POST `/shards/{shard_id}/delete`

- Soft-delete del shard (lifecycle) y además remueve del feed del perfil actual (si existía).

---

## Ejemplo resumido: POST `/analyze-shard`

### Request (curl)

```bash
curl -sS -X POST http://localhost:5005/analyze-shard \
  -F "audio=@./sample.wav;type=audio/wav" \
  -F "sampleRate=16000" \
  -F "durationSeconds=1.0" \
  -F 'features={"rms":0.12,"zcr":0.03,"spectralCentroid":1200,"intensity":0.8}' \
  -F 'meta={"shardId":"shard-1","episodeId":"ep-1","startTime":0,"endTime":1,"source":"mic"}'
```

### Response (200, ejemplo)

```json
{
  "transcript": "...",
  "transcriptLanguage": "es",
  "transcriptionConfidence": 0.92,
  "language": "es",
  "emotion": {
    "primary": "enojo",
    "valence": "negative",
    "activation": "high",
    "distribution": {"enojo": 0.6, "neutro": 0.4},
    "headline": "Alza de voz.",
    "explanation": null
  },
  "emotionLegacy": {
    "primary": "enojo",
    "valence": "negativo",
    "activation": "alto",
    "scores": [{"label": "enojo", "score": 0.6}, {"label": "neutro", "score": 0.4}]
  },
  "primaryEmotion": "enojo",
  "emotionLabels": [{"label": "enojo", "score": 0.6}, {"label": "neutro", "score": 0.4}],
  "valence": "negativo",
  "arousal": "alto",
  "analysisSource": "local",
  "analysisMode": "automatic",
  "analysisVersion": "0.1.0-local",
  "analysisAt": "2025-12-26T21:00:00Z"
}
```

---

## 5. Comunidad: Perfiles, Progreso e Invitaciones

Esta sección describe el contrato de la capa de comunidad **implementada**.

### 5.1 Header de perfil actual (sin auth)

- Por defecto el servicio usa un perfil local fijo: `local_profile_1`.
- (Opcional) el cliente puede mandar `X-Profile-Id: <string>` para simular otro perfil local.

### 5.2 Modelo `Profile`

```ts
type Profile = {
  id: string;
  createdAt: string; // ISO datetime (UTC)
  updatedAt: string; // ISO datetime (UTC)
  role: 'ghost' | 'active';
  state: 'passive' | 'active';
  tevScore: number;
  dailyStreak: number;
  lastActiveAt: string; // ISO datetime (UTC)
  invitationsGrantedTotal: number;
  invitationsUsed: number;
  invitationsRemaining: number;
};
```

### 5.3 Modelo `ProgressSummary`

```ts
type ProgressSummary = {
  profileId: string;
  date: string; // YYYY-MM-DD
  activitySeconds: number;
  sessionCount: number;
  shardCount: number;
  votes: {
    upvotes: number;
    downvotes: number;
  };
  progressTowardsActivation: number; // 0..1
  ethicalTrend: 'onTrack' | 'warning' | 'critical';
  canPromoteToActive: boolean;
};
```

### 5.4 Modelo `Invitation`

```ts
type Invitation = {
  id: string;
  createdAt: string; // ISO datetime (UTC)
  updatedAt: string; // ISO datetime (UTC)
  inviterId: string;
  inviteeId?: string | null;
  email: string;
  code: string;
  state: 'pending' | 'accepted' | 'revoked' | 'expired';
  expiresAt: string; // ISO datetime (UTC)
  acceptedAt?: string | null;
  revokedAt?: string | null;
};
```

### 5.5 Endpoints

#### 5.5.1 GET `/me`

**Response 200:**

```ts
type MeResponse = {
  profile: Profile;
  todayProgress: ProgressSummary;
  invitationsSummary: {
    grantedTotal: number;
    used: number;
    remaining: number;
  };
};
```

#### 5.5.2 GET `/me/progress`

**Response 200:**

```ts
type MeProgressResponse = {
  today: ProgressSummary;
  history: ProgressSummary[]; // últimos 30 días
};
```

#### 5.5.3 GET `/me/invitations`

**Response 200:**

```ts
type MeInvitationsResponse = {
  invitations: Invitation[];
};
```

#### 5.5.4 POST `/invitations`

**Request body (JSON):**

```ts
type CreateInvitationRequest = {
  email: string;
};
```

**Response 200:**

```ts
type CreateInvitationResponse = {
  invitation: Invitation;
};
```

**Errores relevantes:**

- **400** si `email` está vacío.
- **400** si no hay invitaciones restantes.

