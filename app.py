from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List
import logging, traceback
from logic import bro_guess, program_search

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Takarazuka Link Finder", version="1.0.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null", "http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/ping")
def ping():
    return {"ok": True, "msg": "pong"}

@app.get("/api/program")
def api_program(year: int = 2025,
                q: List[str] = Query(default=[]),
                delay_min: float = 0.6, delay_max: float = 1.5,
                timeout: float = 15.0):
    try:
        rows = program_search(year, q, delay_min, delay_max, timeout)
        return JSONResponse({"year": year, "queries": q, "results": rows})
    except Exception as e:
        logging.error("PROGRAM API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "program_search_failed", "message": str(e), "queries": q, "results": []}, status_code=200)

@app.get("/api/bro")
def api_bro(prefix: str,
            ss_min: int = 1, ss_max: int = 40,
            delay_min: float = 0.6, delay_max: float = 1.5,
            timeout: float = 15.0):
    try:
        rows = bro_guess(prefix, ss_min, ss_max, delay_min, delay_max, timeout)
        return JSONResponse({"prefix": prefix, "results": rows})
    except Exception as e:
        logging.error("BRO API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "bro_failed", "message": str(e), "results": []}, status_code=200)

@app.get("/api/goethe")
def api_goethe(ss_min: int = 1, ss_max: int = 40,
               delay_min: float = 0.6, delay_max: float = 1.6,
               timeout: float = 15.0):
    try:
        forum = bro_guess("2511161", ss_min, ss_max, delay_min, delay_max, timeout)
        umeda = bro_guess("2512011", ss_min, ss_max, delay_min, delay_max, timeout)
        pro = program_search(2025, ["Goethe", "花組"], delay_min, delay_max, timeout)
        return JSONResponse({"forum_prefix": "2511161", "umeda_prefix": "2512011",
                             "forum": forum, "umeda": umeda, "program": pro})
    except Exception as e:
        logging.error("GOETHE API ERROR: %s", e)
        traceback.print_exc()
        return JSONResponse({"error": "goethe_failed", "message": str(e), "forum": [], "umeda": [], "program": []}, status_code=200)
