# Miro Board Reconciler

Syncs Google Sheets with Miro boards using AI to intelligently match features and add sync status notes.

## Setup

1. **Install dependencies**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn dspy-ai pandas python-dotenv pydantic requests google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client redis celery httpx openpyxl
```

2. **Get API keys**
- OpenAI: https://platform.openai.com/api-keys
- Miro: https://developers.miro.com/ (create app → get token)
- Google: https://console.cloud.google.com/ (enable Sheets API → create OAuth credentials)

3. **Create .env file**
```env
OPENAI_API_KEY=sk-proj-your-key
MIRO_API_TOKEN=your-miro-token
GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-google-secret
REDIS_URL=redis://localhost:6379
```

4. **Start services**
```bash
# Terminal 1
redis-server

# Terminal 2  
celery -A main.celery_app worker --loglevel=info

# Terminal 3
python main.py
```

## Usage

1. **Get Google auth token**
```bash
curl http://localhost:8000/auth/google
# Visit URL, complete OAuth, get token from callback
```

2. **Start reconciliation**
```bash
curl -X POST "http://localhost:8000/reconcile" \
  -H "Authorization: Bearer your-google-token" \
  -H "Content-Type: application/json" \
  -d '{
    "miro_board_id": "your-board-id",
    "google_sheets": {
      "spreadsheet_id": "your-sheet-id",
      "sheet_name": "Sheet1"
    },
    "dry_run": false,
    "user_token": "your-google-token"
  }'
```

3. **Check progress**
```bash
curl http://localhost:8000/jobs/JOB_ID
```

## What it does

- Reads your Google Sheet with feature data
- Compares with your Miro board items
- Uses AI to match similar items (e.g., "Google Login" ↔ "Google Auth")
- Adds colored sticky notes to show what's out of sync
- Non-destructive - only adds notes, doesn't change existing work

## Spreadsheet format

| Feature Name | Status | Priority | Owner | Description |
|-------------|--------|----------|-------|-------------|
| Google Login | Done | High | Alice | OAuth integration |
| Dark Mode | In Progress | Medium | Bob | Theme switcher |

API docs: http://localhost:8000/docs