# Run:
# uvicorn services.safety_scoring.main:app --host 0.0.0.0 --port 20003 --reload
# Docs: http://127.0.0.1:20003/docs

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Literal, Dict
from datetime import datetime

app = FastAPI(title="Safety Scoring Service", version="1.0.0",
              description="Safety scoring, factors, and weights APIs.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

class Point(BaseModel): lat: float; lon: float
class SafetySegmentInput(BaseModel): start_lat: float; start_lon: float; end_lat: float; end_lon: float
class ScoreRouteRequest(BaseModel):
    route_geometry: str; segments: List[SafetySegmentInput]; time_of_day: datetime
    weather_conditions: Optional[Literal["clear","rain","fog"]] = None
class RiskFactor(BaseModel): type: str; severity: str
class SafetySegmentScore(BaseModel):
    segment_id: str; start_lat: float; start_lon: float; end_lat: float; end_lon: float
    score: float; risk_factors: List[RiskFactor] = []
class SafetyAlert(BaseModel): type: str; location: Point; severity: str; message: str
class ScoreRouteResponse(BaseModel):
    overall_score: float; scoring_breakdown: Dict[str, float]
    segments: List[SafetySegmentScore]; alerts: List[SafetyAlert]; calculated_at: datetime

class SafetyFactorsRequest(BaseModel): lat: float; lon: float; radius_m: int = 50
class SafetyFactorsResponse(BaseModel):
    location: Point; radius_m: int; factors: Dict[str, object]; composite_score: float; queried_at: datetime

class SafetyWeights(BaseModel):
    cctv_coverage: float; street_lighting: float; business_activity: float; crime_rate: float; pedestrian_traffic: float
class SafetyWeightsRequest(BaseModel): user_id: str; weights: SafetyWeights
class SafetyWeightsResponse(BaseModel):
    status: Literal["updated"]; user_id: str; weights: SafetyWeights; weights_sum: float; updated_at: datetime

@app.get("/")
<<<<<<< HEAD
async def root(): return {"service": "safety_scoring", "status": "running"}

@app.get("/health")
async def health(): return {"status": "ok", "service": "safety_scoring"}

@app.post("/v1/safety/score-route", response_model=ScoreRouteResponse)
async def score_route(body: ScoreRouteRequest):
    segs = [SafetySegmentScore(segment_id=f"seg_{i+1:03d}",
                               start_lat=s.start_lat,start_lon=s.start_lon,end_lat=s.end_lat,end_lon=s.end_lon,
                               score=85+i) for i, s in enumerate(body.segments)]
    return ScoreRouteResponse(
        overall_score=87.5, scoring_breakdown={"cctv_coverage":90,"street_lighting":85,"crime_rate":82},
        segments=segs, alerts=[], calculated_at=datetime.utcnow()
    )

@app.post("/v1/safety/factors", response_model=SafetyFactorsResponse)
async def get_factors(body: SafetyFactorsRequest):
    return SafetyFactorsResponse(
        location=Point(lat=body.lat, lon=body.lon), radius_m=body.radius_m,
        factors={"cctv_cameras":3,"street_lights":5,"foot_traffic_level":"medium"},
        composite_score=88.0, queried_at=datetime.utcnow()
    )

@app.put("/v1/safety/weights", response_model=SafetyWeightsResponse)
async def update_weights(body: SafetyWeightsRequest):
    w = body.weights
    total = w.cctv_coverage + w.street_lighting + w.business_activity + w.crime_rate + w.pedestrian_traffic
    return SafetyWeightsResponse(status="updated", user_id=body.user_id, weights=w,
                                 weights_sum=total, updated_at=datetime.utcnow())
=======
def data_cleaner():
    return {"message": "safety scoring service"}
>>>>>>> refs/remotes/origin/main
