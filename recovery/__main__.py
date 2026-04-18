"""CLI entry point: `recovery <command>`"""
import click
from rich.console import Console

console = Console()


@click.group()
def cli():
    """Recovery Bot — Garmin + Strava recovery tracking."""
    pass


@cli.group()
def auth():
    """Authenticate with data sources."""
    pass


@auth.command("garmin")
def auth_garmin():
    """Authenticate with Garmin Connect (stores tokens in ~/.recovery-bot/garmin_tokens/)."""
    from garminconnect import Garmin
    from recovery.ingest.garmin import _TOKEN_DIR
    from recovery import config as cfg_mod

    cfg = cfg_mod.get()
    email = cfg.garmin.email or click.prompt("Garmin email")
    password = click.prompt("Garmin password", hide_input=True)

    console.print("Logging in to Garmin Connect...")
    try:
        api = Garmin(email=email, password=password)
        mfa_status, _ = api.login(tokenstore=str(_TOKEN_DIR))
        if mfa_status:
            mfa_code = click.prompt("MFA code")
            api.resume_login(client_state=mfa_status, mfa_code=mfa_code)
            api.login(tokenstore=str(_TOKEN_DIR))
        console.print(f"[green]Garmin authentication successful. Tokens saved to {_TOKEN_DIR}[/green]")
    except Exception as e:
        console.print(f"[red]Authentication failed: {e}[/red]")
        raise SystemExit(1)


@auth.command("strava")
def auth_strava():
    """Authenticate with Strava (opens browser for OAuth)."""
    import json
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    from recovery import config as cfg_mod
    from recovery.ingest.strava import exchange_code, get_auth_url

    cfg = cfg_mod.get()
    if not cfg.strava.client_id or not cfg.strava.client_secret:
        console.print("[red]Set strava.client_id and strava.client_secret in config.toml first.[/red]")
        raise SystemExit(1)

    auth_url = get_auth_url(cfg.strava.client_id)
    code_holder: dict = {}
    server_done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<h2>Strava connected! You can close this tab.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization failed.</h2>")
            server_done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", 8081), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()

    console.print(f"Opening browser for Strava authorization...")
    webbrowser.open(auth_url)
    server_done.wait(timeout=120)

    if "code" not in code_holder:
        console.print("[red]Authorization timed out.[/red]")
        raise SystemExit(1)

    try:
        exchange_code(cfg.strava.client_id, cfg.strava.client_secret, code_holder["code"])
        console.print("[green]Strava authentication successful. Token saved.[/green]")
    except Exception as e:
        console.print(f"[red]Token exchange failed: {e}[/red]")
        raise SystemExit(1)


@cli.command()
@click.option("--days", default=None, type=int, help="Override backfill_days from config")
def backfill(days):
    """Backfill historical Garmin + Strava data (resumes if interrupted)."""
    from recovery.ingest.sync import backfill as do_backfill
    do_backfill(days=days)


@cli.command()
def sync():
    """Run the daily sync (yesterday's Garmin + new Strava activities)."""
    from recovery.ingest.sync import daily_sync
    daily_sync()


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=None, type=int)
def serve(host, port):
    """Start the web dashboard."""
    import uvicorn
    from recovery import config as cfg_mod
    from recovery.db.session import init_db

    cfg = cfg_mod.get()
    init_db()
    uvicorn.run(
        "recovery.api.app:app",
        host=host,
        port=port or cfg.ui.port,
        reload=False,
    )


@cli.command("mcp")
@click.argument("action", type=click.Choice(["run", "install"]))
def mcp_cmd(action):
    """Run or install the MCP server for Claude Desktop."""
    if action == "run":
        from recovery.mcp.server import run_mcp
        run_mcp()
    elif action == "install":
        _install_mcp()


def _install_mcp():
    import json
    import shutil
    import sys
    from pathlib import Path

    config_path = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    python = shutil.which("python") or sys.executable
    recovery_path = str(Path(__file__).parent.parent.resolve())

    entry = {
        "command": python,
        "args": ["-m", "recovery", "mcp", "run"],
        "env": {"PYTHONPATH": recovery_path},
    }

    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {}

    config.setdefault("mcpServers", {})["recovery-bot"] = entry

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    console.print(f"[green]MCP server registered at {config_path}[/green]")
    console.print("Restart Claude Desktop to load the server.")


@cli.command()
@click.argument("action", type=click.Choice(["install", "uninstall", "status"]))
def schedule(action):
    """Manage the launchd daily sync job."""
    import subprocess
    from pathlib import Path

    plist_src = Path(__file__).parent.parent / "launchd" / "com.recoverybot.sync.plist"
    plist_dst = Path.home() / "Library" / "LaunchAgents" / "com.recoverybot.sync.plist"

    if action == "install":
        import shutil
        import sys

        plist_dst.parent.mkdir(parents=True, exist_ok=True)
        content = plist_src.read_text()
        python = shutil.which("python") or sys.executable
        content = content.replace("PYTHON_PATH", python)
        plist_dst.write_text(content)
        subprocess.run(["launchctl", "load", str(plist_dst)], check=True)
        console.print(f"[green]launchd job installed and loaded.[/green]")

    elif action == "uninstall":
        subprocess.run(["launchctl", "unload", str(plist_dst)], check=False)
        plist_dst.unlink(missing_ok=True)
        console.print("[green]launchd job removed.[/green]")

    elif action == "status":
        result = subprocess.run(
            ["launchctl", "list", "com.recoverybot.sync"],
            capture_output=True, text=True
        )
        console.print(result.stdout or result.stderr)


if __name__ == "__main__":
    cli()
