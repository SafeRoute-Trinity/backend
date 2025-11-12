# Run:
# uvicorn services.routing_service.main:app --host 0.0.0.0 --port 20002 --reload
# Docs: http://127.0.0.1:20002/docs

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Literal
from datetime import datetime
import uuid

app = FastAPI(title="Routing Service", version="1.0.0",
              description="Route calculation & navigation session APIs.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

ROUTES = {}; NAV = {}

class Point(BaseModel): lat: float; lon: float
class RoutePreferences(BaseModel):
    optimize_for: Literal["safety","time","distance","balanced"]
    avoid: Optional[List[str]] = None
    transport_mode: Literal["walking","cycling","driving","public_transit"]
class RouteCalculateRequest(BaseModel):
    origin: Point; destination: Point; user_id: str
    preferences: RoutePreferences; time_of_day: Optional[datetime] = None
class Waypoint(BaseModel): lat: float; lon: float; instruction: Optional[str] = None
class RouteOption(BaseModel):
    route_index:int; is_primary:bool; geometry:str; distance_m:int; duration_s:int; safety_score:float
    waypoints:List[Waypoint]=[]
class RouteCalculateResponse(BaseModel):
    route_id:str; routes:List[RouteOption]; alternatives_count:int; calculated_at:datetime

class RecalculateRequest(BaseModel):
    route_id: str; current_location: Point
    reason: Literal["off_track","road_closure","user_request","safety_alert"]

class NavigationStartRequest(BaseModel):
    route_id: str; user_id: str; estimated_arrival: datetime
class NavigationStartResponse(BaseModel):
    session_id:str; status:Literal["active"]; started_at:datetime

@app.get("/")
<<<<<<< HEAD
async def root(): return {"service": "routing_service", "status": "running"}

@app.get("/health")
async def health(): return {"status": "ok", "service": "routing_service"}

@app.post("/v1/routes/calculate", response_model=RouteCalculateResponse)
async def calc(body: RouteCalculateRequest):
    rid = f"rt_{uuid.uuid4().hex[:6]}"; now = datetime.utcnow()
    opt = RouteOption(route_index=0,is_primary=True,geometry="encoded_polyline_demo",
                      distance_m=2450,duration_s=1800,safety_score=87.5,
                      waypoints=[Waypoint(lat=body.origin.lat,lon=body.origin.lon,instruction="Start"),
                                Waypoint(lat=body.destination.lat,lon=body.destination.lon,instruction="Arrive")])
    ROUTES[rid]={"route_id":rid,"routes":[opt],"alternatives_count":1,"calculated_at":now}
    return RouteCalculateResponse(**ROUTES[rid])

@app.post("/v1/routes/{route_id}/recalculate", response_model=RouteCalculateResponse)
async def recalc(route_id: str, body: RecalculateRequest):
    return await calc(RouteCalculateRequest(
        origin=body.current_location, destination=body.current_location,
        user_id="demo", preferences=RoutePreferences(optimize_for="balanced", transport_mode="walking")
    ))

@app.post("/v1/navigation/start", response_model=NavigationStartResponse)
async def nav_start(body: NavigationStartRequest):
    sid=f"nav_{uuid.uuid4().hex[:8]}"; now=datetime.utcnow()
    NAV[sid]={"route_id":body.route_id,"user_id":body.user_id,"started_at":now}
    return NavigationStartResponse(session_id=sid,status="active",started_at=now)
=======
def data_cleaner():
    return {"message": "routing service"}
>>>>>>> refs/remotes/origin/main
