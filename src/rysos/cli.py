"""Typer CLI interface for rysOS with multi-account support."""

import sys
import asyncio
from typing import Optional
import typer
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from rysos.config import settings
from rysos.identity import account_type_for
from rysos.core import rysos_core
from rysos.auth.google_auth import google_auth
from rysos.ai.gemini import gemini_ai
from rysos.observability import get_token_usage_stats

# Force UTF-8 on Windows console to prevent CP1252 encoding errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = typer.Typer(
    name="rysos",
    help="rysOS: Executive Operating System for Obsidian governed by Gemini AI",
    add_completion=False,
)
console = Console(force_terminal=True)


@app.command()
def serve(
    host: str = typer.Option(settings.HOST, "--host", "-h", help="Host address"),
    port: int = typer.Option(settings.PORT, "--port", "-p", help="Port number"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
):
    """Start the rysOS Web Server & PWA Dashboard."""
    console.print(
        Panel.fit(
            f"[bold cyan]rysOS Executive OS[/bold cyan]\n"
            f"[dim]Cofre Obsidian:[/dim] [green]{settings.OBSIDIAN_VAULT_PATH}[/green]\n"
            f"[dim]Servidor Web:[/dim] [bold yellow]http://{host}:{port}[/bold yellow]",
            title="[rysOS] Iniciando Servidor",
            border_style="cyan",
        )
    )
    uvicorn.run("rysos.web.app:app", host=host, port=port, reload=reload)


@app.command()
def init_vault():
    """Initialize PARA folders, MECE Area MOCs, and templates in the Obsidian vault."""
    console.print("[yellow]Inicializando cofre Obsidian...[/yellow]")
    res = rysos_core.vault.initialize_vault_structure()
    console.print(f"[bold green][OK] Cofre inicializado com sucesso em:[/bold green] {res['vault_path']}")
    if res["created_folders"]:
        console.print(f"Pastas criadas: {', '.join(res['created_folders'])}")


@app.command()
def sync():
    """Run full workspace synchronization (Google Calendar + Gmail + Obsidian Cockpit)."""
    console.print("[cyan]Sincronizando Workspace com Obsidian e Gemini...[/cyan]")
    res = asyncio.run(rysos_core.run_daily_sync())
    console.print(
        Panel.fit(
            f"[bold green][OK] Sincronizacao Concluida![/bold green]\n"
            f"Eventos Processados: [bold]{res['events_count']}[/bold]\n"
            f"E-mails Criticos: [bold]{res['actionable_emails_count']}[/bold]\n"
            f"Decisoes no Radar: [bold]{res['pending_decisions_count']}[/bold]\n"
            f"Cockpit Gerado: [yellow]{res['cockpit_file']}[/yellow]",
            title="Status do Sync",
            border_style="green",
        )
    )


@app.command()
def status():
    """Check rysOS health, connected Google accounts, and Gemini AI status."""
    table = Table(title="rysOS Status do Sistema", border_style="cyan")
    table.add_column("Componente", style="bold")
    table.add_column("Status")
    table.add_column("Detalhes", style="dim")

    # Vault
    vault_ok = settings.OBSIDIAN_VAULT_PATH.exists()
    table.add_row(
        "Cofre Obsidian (Google Drive)",
        "[green]Conectado[/green]" if vault_ok else "[red]Nao Encontrado[/red]",
        str(settings.OBSIDIAN_VAULT_PATH),
    )

    # Accounts
    accounts = google_auth.get_all_accounts()
    if accounts:
        acc_list = ", ".join([a.email for a in accounts])
        table.add_row(
            f"Google Workspace ({len(accounts)} conta(s))",
            "[green]Autenticado[/green]",
            acc_list,
        )
    else:
        table.add_row(
            "Google Workspace",
            "[yellow]Pendente (execute 'rysos auth')[/yellow]",
            "Nenhuma conta conectada",
        )

    # Gemini
    gemini_ok = gemini_ai.is_available()
    table.add_row(
        "Google Gemini AI",
        "[green]Pronto[/green]" if gemini_ok else "[yellow]Chave API Ausente[/yellow]",
        f"Modelo: {settings.GEMINI_MODEL}",
    )

    # Observability / Tokens
    token_stats = get_token_usage_stats()
    today_tok = token_stats.get("today", {})
    total_tok = token_stats.get("total", {})
    table.add_row(
        "Observabilidade (Tokens IA)",
        f"[cyan]{today_tok.get('total_tokens', 0):,} hoje[/cyan]",
        f"Custo Hoje: ${today_tok.get('cost_usd', 0.0):.4f} (~R$ {today_tok.get('cost_brl', 0.0):.2f}) | Total: {total_tok.get('total_tokens', 0):,} tokens",
    )

    console.print(table)


@app.command(name="tokens")
def tokens_command(
    days: int = typer.Option(30, "--days", "-d", help="Number of days to aggregate"),
):
    """View detailed real-time token usage, cost governance, and AI operation breakdown."""
    stats = get_token_usage_stats(days=days)
    today = stats.get("today", {})
    last_7d = stats.get("last_7_days", {})
    total = stats.get("total", {})

    console.print(
        Panel.fit(
            f"[bold cyan]rysOS Observabilidade & Governança de Tokens (Gemini AI)[/bold cyan]\n"
            f"[dim]Modelo Principal:[/dim] [green]{stats.get('current_model')}[/green]\n\n"
            f"📅 [bold]Hoje:[/bold] [bold yellow]{today.get('total_tokens', 0):,}[/bold yellow] tokens "
            f"([dim]Prompt:[/dim] {today.get('prompt_tokens', 0):,} | [dim]Output:[/dim] {today.get('candidate_tokens', 0):,}) "
            f"➜ [bold green]${today.get('cost_usd', 0.0):.6f}[/bold green] (~R$ {today.get('cost_brl', 0.0):.4f}) across [bold]{today.get('calls_count', 0)}[/bold] calls\n"
            f"📊 [bold]Últimos 7 Dias:[/bold] [bold yellow]{last_7d.get('total_tokens', 0):,}[/bold yellow] tokens "
            f"➜ [bold green]${last_7d.get('cost_usd', 0.0):.6f}[/bold green] (~R$ {last_7d.get('cost_brl', 0.0):.4f})\n"
            f"🏛️ [bold]Total Histórico:[/bold] [bold yellow]{total.get('total_tokens', 0):,}[/bold yellow] tokens "
            f"➜ [bold green]${total.get('cost_usd', 0.0):.6f}[/bold green] (~R$ {total.get('cost_brl', 0.0):.4f})",
            title="Consumo Real de Tokens",
            border_style="cyan",
        )
    )

    by_op = stats.get("by_operation", [])
    if by_op:
        t_op = Table(title="Consumo por Operação de IA", border_style="cyan")
        t_op.add_column("Operação", style="bold")
        t_op.add_column("Chamadas", justify="right")
        t_op.add_column("Prompt Tokens", justify="right", style="dim")
        t_op.add_column("Output Tokens", justify="right", style="dim")
        t_op.add_column("Total Tokens", justify="right", style="bold yellow")
        t_op.add_column("Custo Est. (USD)", justify="right", style="green")
        t_op.add_column("Custo Est. (BRL)", justify="right", style="green")

        for op in by_op:
            t_op.add_row(
                op["operation"],
                str(op["calls_count"]),
                f"{op['prompt_tokens']:,}",
                f"{op['candidate_tokens']:,}",
                f"{op['total_tokens']:,}",
                f"${op['cost_usd']:.6f}",
                f"R$ {op['cost_brl']:.4f}",
            )
        console.print(t_op)

    recent = stats.get("recent_logs", [])
    if recent:
        t_rec = Table(title="Últimas Chamadas de IA", border_style="dim")
        t_rec.add_column("Data/Hora (UTC)", style="dim")
        t_rec.add_column("Operação", style="cyan")
        t_rec.add_column("Modelo", style="dim")
        t_rec.add_column("Prompt", justify="right")
        t_rec.add_column("Output", justify="right")
        t_rec.add_column("Total", justify="right", style="bold")
        t_rec.add_column("Custo (USD)", justify="right", style="green")

        for r in recent[:10]:
            t_rec.add_row(
                str(r["created_at"])[:19].replace("T", " "),
                r["operation"],
                r["model"],
                f"{r['prompt_tokens']:,}",
                f"{r['candidate_tokens']:,}",
                f"{r['total_tokens']:,}",
                f"${r['cost_usd']:.6f}",
            )
        console.print(t_rec)


@app.command(name="metrics")
def metrics_command(
    days: int = typer.Option(30, "--days", "-d", help="Number of days to aggregate"),
):
    """Alias for 'rysos tokens'."""
    tokens_command(days=days)


@app.command()
def accounts():
    """List all connected Google accounts."""
    accs = google_auth.get_all_accounts()
    if not accs:
        console.print("[yellow]Nenhuma conta Google conectada. Execute 'rysos auth' para conectar.[/yellow]")
        return

    table = Table(title="Contas Google Conectadas", border_style="cyan")
    table.add_column("E-mail", style="bold green")
    table.add_column("Tipo", style="cyan")
    table.add_column("Arquivo de Token", style="dim")

    for a in accs:
        acc_type = "💼 Corporativo" if account_type_for(a.email) == "corporativo" else "🏠 Pessoal"
        table.add_row(a.email, acc_type, str(a.token_path.name))

    console.print(table)


@app.command()
def auth(
    url_or_code: Optional[str] = typer.Option(None, "--code", "-c", help="Authorization Code or full redirect URL"),
):
    """Connect a Google Account (run again anytime to add multiple accounts)."""
    if url_or_code:
        try:
            acc = google_auth.complete_auth_with_code(url_or_code)
            console.print(f"[bold green][OK] Conta conectada com sucesso:[/bold green] {acc.email}")
            return
        except Exception as e:
            console.print(f"[bold red]Erro ao processar codigo:[/bold red] {e}")
            return

    console.print("[cyan]Iniciando autenticacao... Selecione a conta no navegador.[/cyan]")
    try:
        acc = google_auth.add_new_account_interactive(open_browser=True)
        console.print(f"[bold green][OK] Conta conectada com sucesso:[/bold green] {acc.email}")
    except Exception as e:
        console.print(f"[bold red]Erro na autenticacao:[/bold red] {e}")


if __name__ == "__main__":
    app()
