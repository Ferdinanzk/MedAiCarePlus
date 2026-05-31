# MedAiCarePlus Deployment Guide

## Architecture

- **React Frontend** (Vercel) → talks to Supabase for data, Python API for AI
- **Python Backend** (Docker/VPS) → AI inference server (Face Recognition, OCR, Emotion)
- **Supabase** (Singapore) → PostgreSQL database, Auth, Realtime

## Environment Variables

### Frontend (.env)

```
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your-anon-key
VITE_AI_API_URL=https://your-ai-api.com
```

### Python Backend (.env)

```
DATABASE_URL=postgresql://...
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_JWT_SECRET=your-jwt-secret
FRONTEND_URL=https://your-frontend.vercel.app
LINE_CHANNEL_ACCESS_TOKEN=your-line-token
FACE_REC_BASE=/path/to/face_recognition
EMOTION_MODEL_PATH=/path/to/model.pth
YOLO_MODEL_PATH=/path/to/prescription_best.pt
OLLAMA_URL=http://localhost:11434/api/generate
```

## AI API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/face/identify` | POST | Upload photo, get face recognition result |
| `/api/face/enroll` | POST | Upload 3 photos to register a face |
| `/api/ocr/parse` | POST | Upload prescription, get parsed JSON |
| `/api/emotion/analyze` | POST | Upload photo, get detected emotion |
| `/api/notify/missed-dose` | POST | Send LINE alert for missed dose |
| `/api/notify/emotion-alert` | POST | Send LINE alert for low emotion |
| `/api/notify/weekly-summary` | POST | Send weekly health summary |
| `/api/notify/status` | GET | Check LINE notification configuration |
| `/health` | GET | Service health check |

## LINE Messaging API Setup

1. Go to [LINE Developers](https://developers.line.biz/)
2. Create a provider and messaging channel
3. Get Channel Access Token
4. Add the token to `LINE_CHANNEL_ACCESS_TOKEN` env var
5. Family contacts need their LINE user ID (not display name)
   - Use LINE Login or the bot's friend list to get user IDs

## Supabase Setup

1. Create project in Singapore region (ap-southeast-1)
2. Apply migrations (schema, RLS, extensions)
3. Enable Realtime on: medications, intake_logs, emotion_logs
4. Get JWT Secret from Project Settings → API → JWT Settings
5. Configure Auth → URL Configuration with your frontend URL

## Local Development

```bash
# Frontend
cd medaicareplus-web
npm install
npm run dev

# Backend
cd MedAiCarePlus
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Deployment Checklist

- [ ] Supabase project created and migrations applied
- [ ] Frontend env vars set in Vercel
- [ ] Python backend Docker image built and pushed
- [ ] AI model files (OpenVINO, PyTorch, YOLO) uploaded to server
- [ ] Ollama running with vision model (minicpm-v recommended)
- [ ] LINE channel access token configured
- [ ] Supabase JWT secret configured in Python backend
- [ ] CORS origins updated with production frontend URL
