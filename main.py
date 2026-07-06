import time
from fastapi import FastAPI, Header, HTTPException, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI()

# --- CORS MIDDLEWARE ---
# This allows the grading page to securely communicate with your API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"], # Important so the browser can read the 429 timer
)

# --- MOCK DATA / IN-MEMORY STORAGE ---
TOTAL_ORDERS = 48
RATE_LIMIT_MAX = 16
RATE_LIMIT_WINDOW = 10.0  # 10 seconds

# Generate a fixed catalog of orders: ID 1 through 48
ORDERS_CATALOG = [{"id": i, "item": f"Item {i}", "price": 10.0 * i} for i in range(1, TOTAL_ORDERS + 1)]

# Storage for Idempotency: maps "key" -> existing order response dict
idempotency_store = {}

# Storage for Rate Limiting: maps "client_id" -> list of timestamps
rate_limit_store = {}


# --- DATA MODELS ---
class OrderCreate(BaseModel):
    item: str
    price: float


# --- 1. IDEMPOTENT ORDER CREATION ---
@app.post("/orders", status_code=201)
async def create_order(
    order_data: OrderCreate, 
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")
):
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Missing Idempotency-Key header")
    
    # If we've seen this key before, return the EXACT same response
    if idempotency_key in idempotency_store:
        return idempotency_store[idempotency_key]
    
    # Otherwise, create a "new" order ID (mocked here based on store size)
    new_id = 1000 + len(idempotency_store) + 1
    new_order = {"id": new_id, "item": order_data.item, "price": order_data.price}
    
    # Save it so repeat requests return the exact same thing
    idempotency_store[idempotency_key] = new_order
    return new_order


# --- 2. CURSOR PAGINATION ---
@app.get("/orders")
async def get_orders(
    limit: int = Query(10, ge=1), 
    cursor: Optional[str] = Query(None)
):
    # Determine the starting index
    # If no cursor is passed, we start at index 0. If passed, convert back to integer.
    start_index = 0
    if cursor:
        try:
            start_index = int(cursor)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor format")
            
    # Slice the fixed catalog array up to the requested limit
    end_index = start_index + limit
    page_items = ORDERS_CATALOG[start_index:end_index]
    
    # Calculate the next cursor (if there are more items left)
    next_cursor = None
    if end_index < len(ORDERS_CATALOG):
        next_cursor = str(end_index)
        
    return {
        "items": page_items,
        "next_cursor": next_cursor
    }


# --- 3. PER-CLIENT RATE LIMITING (MIDDLEWARE-STYLE CHECK) ---
@app.middleware("http")
async def rate_limiting_middleware(request, call_next):
    # Only rate-limit our specific API routes, skip docs/openapi
    if request.url.path not in ["/orders"]:
        return await call_next(request)
        
    client_id = request.headers.get("X-Client-Id")
    if not client_id:
        return await call_next(request) # Or return 400 if client-id is mandatory
        
    current_time = time.time()
    
    # Initialize list if first time seeing this client
    if client_id not in rate_limit_store:
        rate_limit_store[client_id] = []
        
    # Filter out timestamps older than our 10-second window
    timestamps = [t for t in rate_limit_store[client_id] if current_time - t < RATE_LIMIT_WINDOW]
    
    if len(timestamps) >= RATE_LIMIT_MAX:
        # Calculate how long until the oldest request falls out of the window
        oldest_request = timestamps[0]
        retry_after = int(RATE_LIMIT_WINDOW - (current_time - oldest_request)) + 1
        
        return Response(
            content="Rate limit exceeded. Too many requests.", 
            status_code=429, 
            headers={"Retry-After": str(retry_after)}
        )
        
    # Track this valid request timestamp
    timestamps.append(current_time)
    rate_limit_store[client_id] = timestamps
    
    # Proceed to the actual endpoint
    response = await call_next(request)
    return response