# ==============================================================================
# Miro Board Reconciler API - Production DSPy Web Service
# ==============================================================================
# A web service that compares Google Sheets with Miro boards via REST API

import dspy
import pandas as pd
import logging
from typing import List, Dict, Any, Optional
import asyncio
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# FastAPI imports
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Query, Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

# Google Sheets imports
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.exceptions import RefreshError

# Background task imports
import redis
from celery import Celery
import httpx

# Load environment variables
load_dotenv(override=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==============================================================================
# Configuration
# ==============================================================================

def get_required_env(key: str) -> str:
    """Get required environment variable or raise error"""
    value = os.getenv(key)
    if not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value

class Config:
    """Application configuration"""
    # Required API Keys
    MIRO_API_TOKEN = get_required_env("MIRO_API_TOKEN")
    OPENAI_API_KEY = get_required_env("OPENAI_API_KEY")
    GOOGLE_CLIENT_ID = get_required_env("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = get_required_env("GOOGLE_CLIENT_SECRET")
    
    # Optional with defaults
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    API_HOST = os.getenv("API_HOST", "0.0.0.0")
    API_PORT = int(os.getenv("API_PORT", "8000"))
    SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
    
    # Constants
    MIRO_BASE_URL = "https://api.miro.com/v2"
    DEFAULT_MODEL = "gpt-4o-mini"
    MAX_RETRIES = 3
    TIMEOUT = 30
    
    # Derived values
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

# ==============================================================================
# Enhanced Data Models
# ==============================================================================

class ProductFeature(BaseModel):
    """Represents a product feature from the spreadsheet"""
    name: str = Field(description="Feature name")
    status: str = Field(description="Current status")
    priority: str = Field(description="Priority level")
    description: str = Field(description="Feature description")
    owner: str = Field(description="Person responsible")
    due_date: Optional[str] = Field(default=None, description="Due date")
    requirements: Optional[str] = Field(default=None, description="Additional requirements")
    epic: Optional[str] = Field(default=None, description="Epic or theme")

class MiroItem(BaseModel):
    """Represents an item found on the Miro board"""
    id: str = Field(description="Miro item ID")
    type: str = Field(default="unknown", description="Item type")
    content: str = Field(default="", description="Text content")
    position: Dict[str, Any] = Field(default_factory=dict, description="Position data")
    tags: List[str] = Field(default_factory=list, description="Associated tags")
    color: Optional[str] = Field(default=None, description="Item color")
    parent_id: Optional[str] = Field(default=None, description="Parent frame/group ID")

class FeatureMatch(BaseModel):
    """Represents a match between a spreadsheet feature and Miro item."""
    feature: ProductFeature
    miro_item: MiroItem
    confidence_score: float
    similarity_reasons: List[str]

class UpdateAction(BaseModel):
    """Represents an action to take on the Miro board"""
    action_type: str = Field(description="Type: 'add', 'update', 'remove'")
    feature: Optional[ProductFeature] = None
    miro_item: Optional[MiroItem] = None
    reason: str = Field(description="Explanation for this action")
    priority: int = Field(description="Priority level 1-5")

class ReconciliationResult(BaseModel):
    """Results from comparing spreadsheet to Miro board"""
    actions: List[UpdateAction] = Field(description="Actions to take")
    summary: str = Field(description="High-level summary")
    confidence_score: float = Field(description="Confidence in recommendations")
    timestamp: datetime = Field(default_factory=datetime.now)
    job_id: Optional[str] = Field(default=None, description="Background job ID")

# ==============================================================================
# API Request/Response Models
# ==============================================================================

class GoogleSheetsSource(BaseModel):
    """Google Sheets data source configuration"""
    spreadsheet_id: str = Field(description="Google Sheets ID")
    sheet_name: Optional[str] = Field(default=None, description="Specific sheet name")
    range: Optional[str] = Field(default=None, description="Cell range (e.g., 'A1:F100')")

class ReconciliationRequest(BaseModel):
    """Request to perform reconciliation"""
    miro_board_id: str = Field(description="Miro board ID")
    google_sheets: GoogleSheetsSource = Field(description="Google Sheets source")
    dry_run: bool = Field(default=True, description="Whether to apply changes")
    user_token: str = Field(description="User's Google OAuth token")

class ReconciliationResponse(BaseModel):
    """Response from reconciliation request"""
    job_id: str = Field(description="Background job ID")
    status: str = Field(description="Job status")
    result: Optional[ReconciliationResult] = None
    error: Optional[str] = None
    req: dict

class JobStatus(BaseModel):
    """Background job status"""
    job_id: str
    status: str  # pending, running, completed, failed
    progress: int = Field(description="Progress percentage")
    result: Optional[ReconciliationResult] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class UserInfo(BaseModel):
    """User information for authentication"""
    user_id: str
    email: str
    google_token: str
    miro_token: str

# ==============================================================================
# DSPy Signatures (same as before)
# ==============================================================================

class SpreadsheetParser(dspy.Signature):
    """Parse spreadsheet content into structured product features with names, descriptions, and priorities."""
    
    spreadsheet_content: str = dspy.InputField(
        desc="Raw text content from Google Sheets containing product features"
    )
    features: List[ProductFeature] = dspy.OutputField(
        desc="Extracted and structured product features from the spreadsheet"
    )

class MiroBoardAnalyzer(dspy.Signature):
    """Analyze Miro board items to identify content relevant to product features."""
    
    miro_items_json: str = dspy.InputField(
        desc="JSON string representation of all Miro board items"
    )
    relevant_items: List[MiroItem] = dspy.OutputField(
        desc="Filtered Miro items that are relevant to product development"
    )

class FeatureMatcher(dspy.Signature):
    """Match spreadsheet features with Miro board items using intelligent semantic matching.
    
    Be smart about matching similar items:
    - "Gmail" should match "Google email"  
    - "User Profile" should match "User settings" or "Profile page"
    
    If items are 70%+ similar in meaning, consider them matched even if wording differs.
    Only mark items as unmatched if they are truly different features. Be generous with matching - 
    err on the side of matching similar items rather than creating duplicates
    """

    spreadsheet_features: List[ProductFeature] = dspy.InputField(
        desc="Product features extracted from spreadsheet"
    )
    miro_items: List[MiroItem] = dspy.InputField(
        desc="Relevant Miro board items to match against"
    )
    matches: List[FeatureMatch] = dspy.OutputField(
        desc="Paired matches between features and Miro items with confidence scores"
    )

class UpdateRecommender(dspy.Signature):
    """Generate specific update recommendations.
    
    For matched pairs that are similar but not identical:
    - Suggest updating the board item name/content to match spreadsheet
    - Include both the current board text and desired spreadsheet text
    
    For truly unmatched items:
    - Clearly state what needs to be added or removed
    """
    matched_pairs: List[FeatureMatch] = dspy.InputField(desc="List of matched pairs between features and Miro items")
    unmatched_features: List[ProductFeature] = dspy.InputField(desc="List of unmatched features from spreadsheet")
    unmatched_items: List[MiroItem] = dspy.InputField(desc="List of unmatched items from Miro board")
    recommendations: List[UpdateAction] = dspy.OutputField(desc="List of recommended actions to reconcile the differences")

# ==============================================================================
# Enhanced API Clients
# ==============================================================================

class GoogleSheetsClient:
    """Client for Google Sheets API"""
    
    def __init__(self):
        self.scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    
    async def get_sheet_data(self, user_token: str, spreadsheet_id: str, 
                           sheet_name: Optional[str] = None, 
                           range_name: Optional[str] = None) -> pd.DataFrame:
        """Fetch data from Google Sheets"""
        try:
            # Create credentials from user token
            creds = Credentials(token=user_token)
            
            # Build the service
            service = build('sheets', 'v4', credentials=creds)
            
            # Determine the range
            if range_name:
                sheet_range = f"{sheet_name}!{range_name}" if sheet_name else range_name
            elif sheet_name:
                sheet_range = sheet_name
            else:
                sheet_range = "A:Z"  # Default to all columns
            
            # Call the Sheets API
            result = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=sheet_range
            ).execute()
            
            values = result.get('values', [])
            
            if not values:
                raise ValueError("No data found in spreadsheet")
            
            # Convert to DataFrame
            df = pd.DataFrame(values[1:], columns=values[0])
            
            logger.info(f"Fetched {len(df)} rows from Google Sheets")
            return df
            
        except RefreshError:
            raise HTTPException(status_code=401, detail="Google token expired")
        except Exception as e:
            logger.error(f"Failed to fetch Google Sheets data: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to fetch spreadsheet: {e}")

class AsyncMiroClient:
    """Async client for Miro API"""
    
    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base_url = Config.MIRO_BASE_URL
        self.headers = {
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json'
        }
    
    async def get_board_items(self, board_id: str) -> List[MiroItem]:
        """Fetch all items from a Miro board"""
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.base_url}/boards/{board_id}/items"
                response = await client.get(url, headers=self.headers, timeout=Config.TIMEOUT)
                response.raise_for_status()
                
                data = response.json()
                items = []
                
                for item_data in data.get('data', []):
                    miro_item = MiroItem(
                        id=item_data['id'],
                        type=item_data['type'],
                        content=item_data.get('data', {}).get('content', ''),
                        position=item_data.get('position', {}),  # Keep the full position object
                        tags=item_data.get('tags', []),
                        color=item_data.get('style', {}).get('fillColor'),
                        parent_id=item_data.get('parent', {}).get('id')
                    )
                    items.append(miro_item)
                
                return items
                
            except httpx.RequestError as e:
                logger.error(f"Failed to fetch board items: {e}")
                raise HTTPException(status_code=500, detail="Failed to fetch Miro board")
    
    async def create_sticky_note(self, board_id: str, content: str, 
                               position: Dict[str, float], color: str = "yellow") -> bool:
        """Create a new sticky note on the board"""
        async with httpx.AsyncClient() as client:
            try:
                url = f"{self.base_url}/boards/{board_id}/sticky_notes"
                payload = {
                    "data": {"content": content},
                    "position": position,
                    "style": {"fillColor": color}
                }
                
                response = await client.post(url, json=payload, headers=self.headers, timeout=Config.TIMEOUT)
                response.raise_for_status()
                return True
                
            except httpx.RequestError as e:
                logger.error(f"Failed to create sticky note: {e}")
                return False

# ==============================================================================
# DSPy Modules (Enhanced for Async)
# ==============================================================================

class SpreadsheetProcessor(dspy.Module):
    def __init__(self):
        self.parser = dspy.ChainOfThought(SpreadsheetParser)
    
    def forward(self, spreadsheet_df: pd.DataFrame) -> List[ProductFeature]:
        # Convert DataFrame to text representation
        content = self._df_to_text(spreadsheet_df)
        
        # Call DSPy with proper typing
        result = self.parser(spreadsheet_content=content)
        
        # Result.features is now properly typed as List[ProductFeature]
        return result.features
    
    def _df_to_text(self, df: pd.DataFrame) -> str:
        """Convert DataFrame to text format for LLM processing."""
        if df.empty:
            return "No data available"
        
        # Create structured text representation
        text_parts = []
        for _, row in df.iterrows():
            row_text = " | ".join([f"{col}: {val}" for col, val in row.items() if pd.notna(val)])
            text_parts.append(row_text)
        
        return "\n".join(text_parts)

class MiroBoardProcessor(dspy.Module):
    def __init__(self):
        self.analyzer = dspy.ChainOfThought(MiroBoardAnalyzer)
    
    def forward(self, miro_items: List[MiroItem]) -> List[MiroItem]:
        if not miro_items:
            return []
        
        # Convert to JSON string for LLM
        import json
        miro_json = json.dumps([item.dict() for item in miro_items], indent=2)
        
        # Call DSPy with proper typing
        result = self.analyzer(miro_items_json=miro_json)
        
        # Result.relevant_items is now properly typed as List[MiroItem]
        return result.relevant_items

class ReconciliationEngine(dspy.Module):
    """Main engine that reconciles spreadsheet features with Miro board"""
    
    def __init__(self):
        super().__init__()
        self.matcher = dspy.ChainOfThought(FeatureMatcher)
        self.recommender = dspy.ChainOfThought(UpdateRecommender)
    
    def forward(self, features: List[ProductFeature], miro_items: List[MiroItem]) -> ReconciliationResult:
        """Perform reconciliation and generate recommendations"""
        
        # Step 1: Match features with Miro items
        matches = self.matcher(
            spreadsheet_features=features,
            miro_items=miro_items
        )
        
        # Extract matched and unmatched items from the matches result
        matched_pairs = matches.matches
        
        # Determine unmatched features and items
        matched_feature_ids = {match.feature.name for match in matched_pairs}
        matched_item_ids = {match.miro_item.id for match in matched_pairs}
        
        unmatched_features = [f for f in features if f.name not in matched_feature_ids]
        unmatched_items = [i for i in miro_items if i.id not in matched_item_ids]
        
        # Step 2: Generate recommendations using the updated signature
        rec_result = self.recommender(
            matched_pairs=matched_pairs,
            unmatched_features=unmatched_features,
            unmatched_items=unmatched_items
        )
        
        # rec_result.recommendations is now List[UpdateAction]
        actions = rec_result.recommendations
        
        summary = self._generate_summary(actions)
        confidence = self._calculate_confidence(actions, features, miro_items)
        
        return ReconciliationResult(
            actions=actions,
            summary=summary,
            confidence_score=confidence
        )
    
    def _generate_summary(self, actions: List[UpdateAction]) -> str:
        """Generate summary"""
        add_count = sum(1 for a in actions if a.action_type == 'add')
        update_count = sum(1 for a in actions if a.action_type == 'update')
        remove_count = sum(1 for a in actions if a.action_type == 'remove')
        
        return f"Found {add_count} items to add, {update_count} to update, {remove_count} to remove"
    
    def _calculate_confidence(self, actions: List[UpdateAction], 
                            features: List[ProductFeature], miro_items: List[MiroItem]) -> float:
        """Calculate confidence score"""
        if not features or not actions:
            return 0.0
        
        total_items = len(features) + len(miro_items)
        changes_needed = len(actions)
        
        if total_items == 0:
            return 1.0
            
        return max(0.0, min(1.0, 1.0 - (changes_needed / total_items)))

# ==============================================================================
# Reconciliation Service
# ==============================================================================

class ReconciliationService:
    """Service class that handles the main reconciliation logic"""
    
    def __init__(self):
        # Initialize DSPy
        dspy.settings.configure(
            lm=dspy.LM(Config.DEFAULT_MODEL, api_key=Config.OPENAI_API_KEY, cache=False)
        )
        
        # Initialize all processors
        self.spreadsheet_processor = SpreadsheetProcessor()
        self.board_processor = MiroBoardProcessor()
        self.reconciliation_engine = ReconciliationEngine()
        self.sheets_client = GoogleSheetsClient()
        self.miro_client = AsyncMiroClient(Config.MIRO_API_TOKEN)
        
    
    async def reconcile(self, request: ReconciliationRequest) -> ReconciliationResult:
        """Perform the reconciliation"""
        
        # Initialize clients
        # Fetch data
        df = await self.sheets_client.get_sheet_data(
            request.user_token,
            request.google_sheets.spreadsheet_id,
            request.google_sheets.sheet_name,
            request.google_sheets.range
        )
        
        miro_items = await self.miro_client.get_board_items(request.miro_board_id)
        
        # Process data
        features = self.spreadsheet_processor(df)
        relevant_items = self.board_processor(miro_items)
        result = self.reconciliation_engine(features, relevant_items)
        
        # Apply changes if needed
        if not request.dry_run:
            applied_count = await self._apply_changes(self.miro_client, request.miro_board_id, result.actions)
            result.summary += f" | Added {applied_count} notes to board"
        
        return result
    
    async def _apply_changes(self, miro_client: AsyncMiroClient, board_id: str, actions: List[UpdateAction]) -> int:
        """Apply the recommended changes"""
        applied_count = 0
        
        for i, action in enumerate(actions):
            try:
                if action.action_type == 'add' and action.feature:
                    content = f"ADD: {action.feature.name}\nStatus: {action.feature.status}\nOwner: {action.feature.owner}"
                elif action.action_type == 'update' and action.miro_item:
                    item_name = action.miro_item.content[:30] if action.miro_item.content else "Unknown item"
                    content = f"UPDATE: {item_name}\n{action.reason}"
                else:
                    content = f"{action.action_type.upper()}: {action.reason}"
                
                position = {'x': -300.0, 'y': 100.0 + (i * 120.0)}
                
                success = await miro_client.create_sticky_note(board_id, content, position)
                if success:
                    applied_count += 1
                    
            except Exception as e:
                logger.error(f"Failed to create note: {e}")
        
        return applied_count

# ==============================================================================
# Background Tasks with Celery
# ==============================================================================

# Initialize Celery
celery_app = Celery(
    'miro_reconciler',
    broker=Config.CELERY_BROKER_URL,
    backend=Config.CELERY_RESULT_BACKEND
)

# Initialize Redis for caching
redis_client = redis.from_url(Config.REDIS_URL)

service = ReconciliationService()

@celery_app.task(bind=True)
def perform_reconciliation_task(self, request_data: dict):
    """Background task to perform reconciliation"""
    
    async def main():
        try:
            req = ReconciliationRequest(**request_data)
            
            self.update_state(state='PROGRESS', meta={'current': 20, 'total': 100})
            
            self.update_state(state='PROGRESS', meta={'current': 50, 'total': 100})
            
            result = await service.reconcile(req)
            result.job_id = self.request.id
            
            self.update_state(state='PROGRESS', meta={'current': 100, 'total': 100})
            return result.dict()
            
        except Exception as e:
            logger.error(f"Task failed: {e}")
            self.update_state(state='FAILURE', meta={'error': str(e)})
            raise
    
    return asyncio.run(main())
# ==============================================================================
# FastAPI Application
# ==============================================================================

app = FastAPI(
    title="Miro Board Reconciler API",
    description="DSPy-powered service to reconcile Google Sheets with Miro boards",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()

# ==============================================================================
# API Dependencies
# ==============================================================================

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserInfo:
    """Extract user info from JWT token (simplified)"""
    # In production, validate JWT token here
    token = credentials.credentials
    
    # For demo purposes, return mock user
    return UserInfo(
        user_id="user123",
        email="user@example.com",
        google_token=token,
        miro_token=token,
    )

# ==============================================================================
# API Endpoints
# ==============================================================================

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "Miro Board Reconciler API", "status": "healthy"}

@app.post("/reconcile", response_model=ReconciliationResponse)
async def start_reconciliation(
    request: ReconciliationRequest,
    background_tasks: BackgroundTasks,
    current_user: UserInfo = Depends(get_current_user)
):
    """Start a reconciliation job"""
    try:
        # Start background task
        task = perform_reconciliation_task.delay(request.model_dump())
        
        # Store job info in Redis
        job_info = JobStatus(
            job_id=task.id,
            status="pending",
            progress=0,
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
        
        redis_client.setex(
            f"job:{task.id}",
            timedelta(hours=24),
            job_info.json()
        )
        
        return ReconciliationResponse(
            job_id=task.id,
            status="pending",
            req=request.dict()
        )
        
    except Exception as e:
        logger.error(f"Failed to start reconciliation: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job_status(
    job_id: str = PathParam(..., description="Job ID"),
    current_user: UserInfo = Depends(get_current_user)
):
    """Get the status of a reconciliation job"""
    try:
        # Check Celery task status
        task = perform_reconciliation_task.AsyncResult(job_id)
        
        if task.state == 'PENDING':
            status = "pending"
            progress = 0
            result = None
            error = None
        elif task.state == 'PROGRESS':
            status = "running"
            progress = task.info.get('current', 0)
            result = None
            error = None
        elif task.state == 'SUCCESS':
            status = "completed"
            progress = 100
            result = ReconciliationResult(**task.result)
            error = None
        elif task.state == 'FAILURE':
            status = "failed"
            progress = 0
            result = None
            error = str(task.info)
        else:
            status = task.state.lower()
            progress = 0
            result = None
            error = None
        
        return JobStatus(
            job_id=job_id,
            status=status,
            progress=progress,
            result=result,
            error=error,
            created_at=datetime.now(),  # Would be stored properly
            updated_at=datetime.now()
        )
        
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Job not found. error: {e}")

@app.get("/sheets/{spreadsheet_id}/preview")
async def preview_spreadsheet(
    spreadsheet_id: str = PathParam(..., description="Google Sheets ID"),
    sheet_name: Optional[str] = Query(None, description="Sheet name"),
    range_name: Optional[str] = Query(None, description="Cell range"),
    current_user: UserInfo = Depends(get_current_user)
):
    """Preview Google Sheets data"""
    try:
        sheets_client = GoogleSheetsClient()
        df = await sheets_client.get_sheet_data(
            current_user.google_token,
            spreadsheet_id,
            sheet_name,
            range_name
        )
        
        # Return preview (first 10 rows)
        preview_df = df.head(10)
        
        return {
            "columns": df.columns.tolist(),
            "total_rows": len(df),
            "preview_data": preview_df.to_dict('records'),
            "sample_features": "Feature extraction would happen here..."
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/boards/{board_id}/preview")
async def preview_miro_board(
    board_id: str = PathParam(..., description="Miro board ID"),
    current_user: UserInfo = Depends(get_current_user)
):
    """Preview Miro board items"""
    try:
        print(f"Token: {Config.MIRO_API_TOKEN[:10]}...")
        miro_client = AsyncMiroClient(Config.MIRO_API_TOKEN)
        items = await miro_client.get_board_items(board_id)
        
        return {
            "total_items": len(items),
            "item_types": list(set(item.type for item in items)),
            "sample_items": [item.dict() for item in items[:5]],
            "relevant_items_count": "Analysis would happen here..."
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/jobs/{job_id}/apply")
async def apply_recommendations(
    job_id: str = PathParam(..., description="Job ID"),
    current_user: UserInfo = Depends(get_current_user)
):
    """Apply the recommendations from a completed job"""
    try:
        # Get job result
        task = perform_reconciliation_task.AsyncResult(job_id)
        
        if task.state != 'SUCCESS':
            raise HTTPException(status_code=400, detail="Job not completed successfully")
        
        result = ReconciliationResult(**task.result)
        
        # Apply recommendations (simplified)
        miro_client = AsyncMiroClient(Config.MIRO_API_TOKEN)
        
        applied_count = 0
        for action in result.actions:
            if action.action_type == 'add' and action.feature:
                # Create new item
                success = await miro_client.create_sticky_note(
                    "board_id",  # Would get from job data
                    f"{action.feature.name}\n{action.feature.status}",
                    {'x': 0, 'y': applied_count * 100}
                )
                if success:
                    applied_count += 1
        
        return {
            "message": f"Applied {applied_count}/{len(result.actions)} recommendations",
            "applied_count": applied_count,
            "total_actions": len(result.actions)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==============================================================================
# Google OAuth Endpoints
# ==============================================================================

@app.get("/auth/google")
async def google_oauth_url():
    """Get Google OAuth URL for authentication"""
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": Config.GOOGLE_CLIENT_ID,
                "client_secret": Config.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [Config.GOOGLE_REDIRECT_URI]
            }
        },
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    flow.redirect_uri = Config.GOOGLE_REDIRECT_URI
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    
    return {"auth_url": authorization_url, "state": state}

@app.get("/auth/google/callback")
async def google_oauth_callback(code: str, state: str):
    """Handle Google OAuth callback"""
    try:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": Config.GOOGLE_CLIENT_ID,
                    "client_secret": Config.GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [Config.GOOGLE_REDIRECT_URI]
                }
            },
            scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
        )
        flow.redirect_uri = Config.GOOGLE_REDIRECT_URI
        
        flow.fetch_token(code=code)
        
        credentials = flow.credentials
        
        return {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "message": "Authentication successful"
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authentication failed: {e}")

# ==============================================================================
# Run the Application
# ==============================================================================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=Config.API_HOST,
        port=Config.API_PORT,
        reload=True,
        log_level="info"
    )

# ==============================================================================
# Example Usage
# ==============================================================================

"""
Example API Usage:

1. Start the API:
   uvicorn main:app --reload

2. Get Google OAuth URL:
   GET /auth/google

3. Complete OAuth flow and get token

4. Preview spreadsheet:
   GET /sheets/1ABC.../preview?sheet_name=Features

5. Preview Miro board:
   GET /boards/board123/preview

6. Start reconciliation:
   POST /reconcile
   {
     "miro_board_id": "board123",
     "google_sheets": {
       "spreadsheet_id": "1ABC...",
       "sheet_name": "Features",
       "range": "A1:F100"
     },
     "dry_run": true,
     "user_token": "google_token_here"
   }

7. Check job status:
   GET /jobs/{job_id}

8. Apply recommendations:
   POST /jobs/{job_id}/apply

Docker Compose setup:

version: '3.8'
services:
  api:
    build: .
    ports:
      - "8000:8000"
    environment:
      - REDIS_URL=redis://redis:6379
      - CELERY_BROKER_URL=redis://redis:6379
    depends_on:
      - redis
  
  worker:
    build: .
    command: celery -A main.celery_app worker --loglevel=info
    environment:
      - REDIS_URL=redis://redis:6379
    depends_on:
      - redis
  
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
"""