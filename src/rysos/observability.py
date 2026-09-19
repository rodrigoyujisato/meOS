"""Observability, Token Tracking, and Cost Governance for rysOS AI operations."""

import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Dict, List
from rysos.config import settings

logger = logging.getLogger("rysos.observability")

# Pricing per 1,000,000 tokens (USD) based on official Google Gemini API pricing
# (ai.google.dev/gemini-api/docs/pricing). NOTE: gemini-3.8-flash carries an
# introductory rate (0.75 / 3.75) valid through 2026-12-31; standard rate from
# 2027-01-01 is 1.50 / 7.50 — bump this row then.
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gemini-3.8-flash": {"prompt_per_m": 0.75, "candidate_per_m": 3.75},
    "gemini-3.5-flash-lite": {"prompt_per_m": 0.075, "candidate_per_m": 0.30},
    "gemini-2.5-flash-lite": {"prompt_per_m": 0.075, "candidate_per_m": 0.30},
    "gemini-1.5-flash-8b": {"prompt_per_m": 0.0375, "candidate_per_m": 0.15},
    "gemini-2.5-flash": {"prompt_per_m": 0.15, "candidate_per_m": 0.60},
    "gemini-1.5-flash": {"prompt_per_m": 0.075, "candidate_per_m": 0.30},
    "gemini-2.5-pro": {"prompt_per_m": 1.25, "candidate_per_m": 5.00},
    "gemini-1.5-pro": {"prompt_per_m": 1.25, "candidate_per_m": 5.00},
}

DEFAULT_PRICING = {"prompt_per_m": 0.075, "candidate_per_m": 0.30}
USD_TO_BRL_ESTIMATE = 5.50


def calculate_token_cost(model_name: str, prompt_tokens: int, candidate_tokens: int) -> float:
    """Calculates estimated cost in USD based on model and token counts."""
    pricing = DEFAULT_PRICING
    m_lower = (model_name or "").lower()
    for key, p in MODEL_PRICING.items():
        if key in m_lower:
            pricing = p
            break

    prompt_cost = (prompt_tokens / 1_000_000.0) * pricing["prompt_per_m"]
    candidate_cost = (candidate_tokens / 1_000_000.0) * pricing["candidate_per_m"]
    return round(prompt_cost + candidate_cost, 6)


def _get_sqlite_db_path() -> str:
    """Extracts local SQLite filepath from settings.DATABASE_URL."""
    url = settings.DATABASE_URL
    if "sqlite+aiosqlite:///" in url:
        return url.replace("sqlite+aiosqlite:///", "")
    elif "sqlite:///" in url:
        return url.replace("sqlite:///", "")
    return str(settings.DATA_DIR / "rysos.db")


def record_ai_token_usage(
    model: str,
    operation: str,
    prompt_tokens: int,
    candidate_tokens: int,
    total_tokens: Optional[int] = None,
    cached_tokens: int = 0,
    details: Optional[str] = None,
) -> Dict[str, Any]:
    """Records AI token consumption synchronously into SQLite database."""
    total = total_tokens if total_tokens is not None else (prompt_tokens + candidate_tokens)
    cost_usd = calculate_token_cost(model, prompt_tokens, candidate_tokens)
    now_utc = datetime.now(timezone.utc).isoformat()

    db_path = _get_sqlite_db_path()
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS token_usage_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model VARCHAR(100) NOT NULL,
                operation VARCHAR(100) NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                candidate_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                cost_usd FLOAT DEFAULT 0.0,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO token_usage_logs 
            (model, operation, prompt_tokens, candidate_tokens, total_tokens, cached_tokens, cost_usd, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model,
                operation,
                prompt_tokens,
                candidate_tokens,
                total,
                cached_tokens,
                cost_usd,
                details,
                now_utc,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Erro ao salvar métrica de tokens: {e}")

    logger.info(
        f"[OBSERVABILITY - AI TOKENS] Op: {operation} | Model: {model} | "
        f"Prompt: {prompt_tokens} | Output: {candidate_tokens} | Total: {total} | "
        f"Custo: ${cost_usd:.6f} (~R$ {cost_usd * USD_TO_BRL_ESTIMATE:.4f})"
    )

    return {
        "model": model,
        "operation": operation,
        "prompt_tokens": prompt_tokens,
        "candidate_tokens": candidate_tokens,
        "total_tokens": total,
        "cached_tokens": cached_tokens,
        "cost_usd": cost_usd,
        "cost_brl": round(cost_usd * USD_TO_BRL_ESTIMATE, 4),
    }


def get_token_usage_stats(days: int = 30) -> Dict[str, Any]:
    """Aggregates real-time token metrics and costs across all operations."""
    db_path = _get_sqlite_db_path()
    empty_res = {
        "today": {"prompt_tokens": 0, "candidate_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "cost_brl": 0.0, "calls_count": 0},
        "last_7_days": {"prompt_tokens": 0, "candidate_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "cost_brl": 0.0, "calls_count": 0},
        "total": {"prompt_tokens": 0, "candidate_tokens": 0, "total_tokens": 0, "cost_usd": 0.0, "cost_brl": 0.0, "calls_count": 0},
        "by_operation": [],
        "by_model": [],
        "recent_logs": [],
        "current_model": settings.GEMINI_MODEL,
        "usd_to_brl": USD_TO_BRL_ESTIMATE,
    }

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage_logs'")
        if not cursor.fetchone():
            conn.close()
            return empty_res

        now = datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        week_ago_str = (now - timedelta(days=7)).isoformat()
        month_ago_str = (now - timedelta(days=days)).isoformat()

        # 1. Today Stats
        cursor.execute(
            """
            SELECT 
                COUNT(*) as calls_count,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(candidate_tokens), 0) as candidate_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd
            FROM token_usage_logs
            WHERE created_at LIKE ?
            """,
            (f"{today_str}%",),
        )
        row_today = cursor.fetchone()
        cost_today = float(row_today["cost_usd"]) if row_today else 0.0
        today_data = {
            "calls_count": row_today["calls_count"] if row_today else 0,
            "prompt_tokens": int(row_today["prompt_tokens"]) if row_today else 0,
            "candidate_tokens": int(row_today["candidate_tokens"]) if row_today else 0,
            "total_tokens": int(row_today["total_tokens"]) if row_today else 0,
            "cost_usd": round(cost_today, 6),
            "cost_brl": round(cost_today * USD_TO_BRL_ESTIMATE, 4),
        }

        # 2. Last 7 Days Stats
        cursor.execute(
            """
            SELECT 
                COUNT(*) as calls_count,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(candidate_tokens), 0) as candidate_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd
            FROM token_usage_logs
            WHERE created_at >= ?
            """,
            (week_ago_str,),
        )
        row_7d = cursor.fetchone()
        cost_7d = float(row_7d["cost_usd"]) if row_7d else 0.0
        last_7d_data = {
            "calls_count": row_7d["calls_count"] if row_7d else 0,
            "prompt_tokens": int(row_7d["prompt_tokens"]) if row_7d else 0,
            "candidate_tokens": int(row_7d["candidate_tokens"]) if row_7d else 0,
            "total_tokens": int(row_7d["total_tokens"]) if row_7d else 0,
            "cost_usd": round(cost_7d, 6),
            "cost_brl": round(cost_7d * USD_TO_BRL_ESTIMATE, 4),
        }

        # 3. Total Stats (All time)
        cursor.execute(
            """
            SELECT 
                COUNT(*) as calls_count,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(candidate_tokens), 0) as candidate_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd
            FROM token_usage_logs
            """
        )
        row_total = cursor.fetchone()
        cost_total = float(row_total["cost_usd"]) if row_total else 0.0
        total_data = {
            "calls_count": row_total["calls_count"] if row_total else 0,
            "prompt_tokens": int(row_total["prompt_tokens"]) if row_total else 0,
            "candidate_tokens": int(row_total["candidate_tokens"]) if row_total else 0,
            "total_tokens": int(row_total["total_tokens"]) if row_total else 0,
            "cost_usd": round(cost_total, 6),
            "cost_brl": round(cost_total * USD_TO_BRL_ESTIMATE, 4),
        }

        # 4. Breakdown by Operation
        cursor.execute(
            """
            SELECT 
                operation,
                COUNT(*) as calls_count,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(candidate_tokens), 0) as candidate_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd
            FROM token_usage_logs
            GROUP BY operation
            ORDER BY total_tokens DESC
            """
        )
        by_op = []
        for r in cursor.fetchall():
            c_usd = float(r["cost_usd"])
            by_op.append({
                "operation": r["operation"],
                "calls_count": r["calls_count"],
                "prompt_tokens": int(r["prompt_tokens"]),
                "candidate_tokens": int(r["candidate_tokens"]),
                "total_tokens": int(r["total_tokens"]),
                "cost_usd": round(c_usd, 6),
                "cost_brl": round(c_usd * USD_TO_BRL_ESTIMATE, 4),
            })

        # 5. Breakdown by Model
        cursor.execute(
            """
            SELECT 
                model,
                COUNT(*) as calls_count,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd
            FROM token_usage_logs
            GROUP BY model
            ORDER BY total_tokens DESC
            """
        )
        by_model = []
        for r in cursor.fetchall():
            c_usd = float(r["cost_usd"])
            by_model.append({
                "model": r["model"],
                "calls_count": r["calls_count"],
                "total_tokens": int(r["total_tokens"]),
                "cost_usd": round(c_usd, 6),
            })

        # 6. Recent Logs (last 15)
        cursor.execute(
            """
            SELECT id, model, operation, prompt_tokens, candidate_tokens, total_tokens, cost_usd, created_at
            FROM token_usage_logs
            ORDER BY id DESC
            LIMIT 15
            """
        )
        recent_logs = []
        for r in cursor.fetchall():
            c_usd = float(r["cost_usd"])
            recent_logs.append({
                "id": r["id"],
                "model": r["model"],
                "operation": r["operation"],
                "prompt_tokens": int(r["prompt_tokens"]),
                "candidate_tokens": int(r["candidate_tokens"]),
                "total_tokens": int(r["total_tokens"]),
                "cost_usd": round(c_usd, 6),
                "created_at": r["created_at"],
            })

        conn.close()

        return {
            "today": today_data,
            "last_7_days": last_7d_data,
            "total": total_data,
            "by_operation": by_op,
            "by_model": by_model,
            "recent_logs": recent_logs,
            "current_model": settings.GEMINI_MODEL,
            "usd_to_brl": USD_TO_BRL_ESTIMATE,
        }

    except Exception as e:
        logger.error(f"Erro ao agregar métricas de observabilidade: {e}")
        return empty_res
