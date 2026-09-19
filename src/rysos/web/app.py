"""FastAPI Web Application entry point for rysOS."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from rysos.config import settings
from rysos.core import rysos_core
from rysos.scheduler.agent_scheduler import agent_scheduler
from rysos.telegram import telegram_bot
from rysos.web.routes import router as api_router

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager for startup and shutdown."""
    # uvicorn only configures its own loggers; without this the root logger has no
    # handler and every rysos.* logger.info() is swallowed by the WARNING-only last
    # resort handler (the [AdaptiveClassifier] / [Draft] lines never reach journald).
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    # Startup: Ensure DB, Vault structure and start scheduler
    await rysos_core.initialize()
    agent_scheduler.start()

    # Drop any note-draft sessions left stale by a previous run so they don't hijack
    # the next message (see rysos.telegram.handlers).
    try:
        from rysos.telegram.handlers import sweep_stale_drafts
        await sweep_stale_drafts()
    except Exception as e:  # pragma: no cover - best-effort housekeeping
        print(f"[Startup] stale-draft sweep skipped: {e}")

    # Start Telegram Bot long-polling in background if configured
    polling_task = None
    if telegram_bot.is_configured():
        print(f"[Telegram Bot] Token detectado ({telegram_bot.token[:10]}...). Iniciando polling...")
        try:
            from rysos.telegram.handlers import register_bot_commands
            await register_bot_commands()
        except Exception as e:  # pragma: no cover - non-critical menu registration
            print(f"[Telegram Bot] Registro do menu de comandos falhou: {e}")
        polling_task = asyncio.create_task(telegram_bot.start_polling())
    else:
        print("[Telegram Bot] Token não configurado ou desabilitado.")

    yield

    # Shutdown
    if polling_task:
        telegram_bot.stop()
        polling_task.cancel()
    await telegram_bot.aclose()
    agent_scheduler.stop()


app = FastAPI(
    title="rysOS Executive Cockpit",
    description="Obsidian Executive Governance & Decision OS powered by Gemini AI",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS Middleware (allows multi-device access from local network or tunnels)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(api_router)

# Mount static folder
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_index():
    """Serves the primary PWA Single Page Application."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "rysOS Web App is initializing..."}


@app.get("/manifest.json")
async def serve_manifest():
    """Serves PWA manifest."""
    manifest_file = STATIC_DIR / "manifest.json"
    if manifest_file.exists():
        return FileResponse(str(manifest_file), media_type="application/manifest+json")
    return {}


@app.get("/sw.js")
async def serve_sw():
    """Serves PWA Service Worker."""
    sw_file = STATIC_DIR / "sw.js"
    if sw_file.exists():
        return FileResponse(str(sw_file), media_type="application/javascript")
    return {}
