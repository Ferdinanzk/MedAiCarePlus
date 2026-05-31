# MedAiCarePlus — React Frontend

MedAiCarePlus patient-facing web application. Built with React + TypeScript + Vite + Tailwind CSS.

## Architecture

This is the **frontend** repo. The **backend** (FastAPI) lives in a separate repo at `D:\claude_tool\repo\MedaiCarePlus`.

```
┌─────────────────────────────┐     ┌──────────────────────────────┐
│  medaicareplus-web (Vercel) │────▶│  MedaiCarePlus (FastAPI)     │
│  React + Vite               │     │  PostgreSQL + Docker         │
└─────────────────────────────┘     └──────────────────────────────┘
```

## Tech Stack

- **Framework:** React 19 + TypeScript
- **Bundler:** Vite
- **Styling:** Tailwind CSS v4
- **Routing:** react-router-dom
- **i18n:** react-i18next (zh-TW default, en fallback)
- **Auth:** Supabase Auth (JWT)
- **UI Library:** shadcn/ui
- **Icons:** Lucide React

## Quick Start

```bash
# Install dependencies
npm install

# Start dev server (localhost:5173)
npm run dev

# Build for production
npm run build
```

## Sync with Backend

After making frontend changes, build and copy the output to the backend repo so it can be served from FastAPI.

**Windows (PowerShell):**
```powershell
.\sync-to-backend.ps1
```

**Linux/macOS (Bash):**
```bash
./sync-to-backend.sh
```

Then run the backend:
```bash
cd ../MedaiCarePlus
docker compose up -d
# Or: python -m uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 to see the full app.

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Description |
|----------|-------------|
| `VITE_SUPABASE_URL` | Supabase project URL |
| `VITE_SUPABASE_ANON_KEY` | Supabase publishable key |
| `VITE_AI_API_URL` | FastAPI backend URL (e.g. `http://localhost:8000` or Vercel URL) |

When `VITE_AI_API_URL` is **unset**, API calls use relative paths (same origin). This is used when the frontend is served by FastAPI.

## Pages

| Route | Page | Description |
|-------|------|-------------|
| `/login` | Login | Face recognition + email login |
| `/register` | Register | New account with face enrollment |
| `/onboarding` | Onboarding | Face + family + first medication setup |
| `/dashboard` | Dashboard | Today's medications, adherence, emotions |
| `/medications` | Medications | Manage all medications |
| `/schedule` | Schedule | Weekly medication schedule |
| `/intake` | Intake | Take medication + AI emotion detection |
| `/emotion` | Emotion | Log emotions + history |
| `/scan` | Scan Prescription | OCR + AI prescription parsing |
| `/family` | Family Contacts | Manage caregivers + verification codes |
| `/history` | History | Medication & emotion history |

## Deployment

This frontend is deployed to **Vercel** automatically on push.

The backend is deployed separately (Docker / Render / Railway).

## Design System

See Google Stitch project for UI mockups:
- Project: `14242123678313225481`
- Design System: Clinical Mint (mint green + deep emerald)
- Colors: `#006d36` primary, `#4ade80` accent, `#0b1c30` text
