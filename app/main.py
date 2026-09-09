from fastapi import FastAPI
from app.routers import admin_ops

app = FastAPI(title="Acredittia API - Backend")

@app.get("/")
def home():
    return {"mensaje": "¡La API de Acredittia está online!"}

# Esto conecta las rutas de operaciones internas que les pasaron
app.include_router(admin_ops.router)
