from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routers import ceo, mt_gt, nhanh, shopee, tiktok

app = FastAPI(
    title="LPM Master API",
    description="API đa sàn dùng schema production DuLieu",
    version="4.15.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", tags=["Hệ thống"])
def root():
    return {"message": "Master API Gateway đang hoạt động với DB DuLieu production schema"}

app.include_router(nhanh.router)
app.include_router(shopee.router)
app.include_router(tiktok.router)
app.include_router(ceo.router)
app.include_router(mt_gt.router)
