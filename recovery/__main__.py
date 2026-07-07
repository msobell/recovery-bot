"""CLI entry point: `recovery <command>`"""
import click
from datetime import date
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
    # daemon=True: on timeout the thread is still blocked in accept(); a
    # non-daemon thread would keep the process alive after SystemExit
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    console.print(f"Opening browser for Strava authorization...")
    webbrowser.open(auth_url)
    server_done.wait(timeout=120)

    if "code" not in code_holder:
        server.server_close()
        console.print("[red]Authorization timed out.[/red]")
        raise SystemExit(1)
    thread.join(timeout=5)
    server.server_close()

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


@cli.command("sync-missing")
@click.option("--days", default=None, type=int, help="Only look back N days (default: full history).")
def sync_missing(days):
    """Fill gaps in the database and refresh recent data.

    Scans for missing Garmin daily records (HRV, sleep, RHR, stress, steps)
    and strength activities, fetches any new Strava activities, and pulls the
    latest weight data from TrendWeight. Always re-syncs yesterday and today
    since Garmin finalises overnight data late.

    Examples:

      recovery sync-missing           # full history scan\n
      recovery sync-missing --days 7  # only look back 7 days
    """
    from recovery.db.session import get_session, init_db
    from recovery.db.models import GarminDaily, StravaActivity
    from recovery.ingest import garmin, strava
    from recovery.ingest.sync import (
        _upsert_garmin, _upsert_garmin_activity, _upsert_strength, _upsert_strava, _upsert_weight,
    )
    from recovery.ingest.trendweight import fetch_measurements
    from recovery import config as cfg_mod
    from sqlalchemy import select, func
    from datetime import timedelta

    cfg = cfg_mod.get()
    session = get_session(init_db())
    today = date.today()
    yesterday = today - timedelta(days=1)

    try:
        earliest = session.execute(select(func.min(GarminDaily.date))).scalar()
        if not earliest:
            console.print("[red]No existing data found. Run `recovery backfill` first.[/red]")
            return

        if days:
            # --days forces a re-sync of the entire window, not just gaps
            scan_from = today - timedelta(days=days)
            to_sync = sorted(scan_from + timedelta(n) for n in range((today - scan_from).days + 1))
            console.print(f"[bold]Garmin:[/bold] re-syncing {len(to_sync)} day(s) from {scan_from}")
        else:
            scan_from = earliest
            existing = set(session.execute(select(GarminDaily.date)).scalars().all())
            missing = [
                scan_from + timedelta(n)
                for n in range((yesterday - scan_from).days + 1)
                if (scan_from + timedelta(n)) not in existing
            ]
            to_sync = sorted(set(missing) | {yesterday, today})
            console.print(f"[bold]Garmin:[/bold] {len(missing)} missing day(s) + refreshing yesterday/today")
        api = garmin.load_session()
        garmin_ok = garmin_err = 0
        for d in to_sync:
            try:
                data = garmin.fetch_day(d, api=api, delay=1.1)
                _upsert_garmin(session, data)
                session.commit()
                for act in garmin.fetch_strength_activities(api, d):
                    _upsert_strength(session, act)
                for act in garmin.fetch_cardio_activities(api, d):
                    _upsert_garmin_activity(session, act)
                session.commit()
                garmin_ok += 1
            except Exception as e:
                session.rollback()
                console.print(f"  [yellow]  {d}: {e}[/yellow]")
                garmin_err += 1
        console.print(f"  [green]Done. {garmin_ok} day(s) synced" + (f", {garmin_err} error(s).[/green]" if garmin_err else ".[/green]"))

        console.print("[bold]Strava:[/bold] fetching new activities...")
        try:
            last_strava = session.execute(select(func.max(StravaActivity.date))).scalar()
            # Overlap by a day so activities recorded later on the last-synced
            # day aren't skipped forever (upsert is idempotent)
            strava_after = (last_strava - timedelta(days=1)) if last_strava else scan_from
            activities = strava.fetch_activities(cfg.strava.client_id, cfg.strava.client_secret, after=strava_after)
            for act in activities:
                _upsert_strava(session, act)
            session.commit()
            console.print(f"  [green]Done. {len(activities)} activity(s) synced.[/green]")
        except Exception as e:
            session.rollback()
            console.print(f"  [yellow]Strava failed: {e}[/yellow]")

        if cfg.trendweight.share_url:
            console.print("[bold]TrendWeight:[/bold] fetching weight data...")
            try:
                measurements = fetch_measurements(cfg.trendweight.share_url)
                for m in measurements:
                    _upsert_weight(session, m)
                session.commit()
                console.print(f"  [green]Done. {len(measurements)} entry(s) synced.[/green]")
            except Exception as e:
                session.rollback()
                console.print(f"  [yellow]TrendWeight failed: {e}[/yellow]")

    finally:
        session.close()


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=None, type=int)
def serve(host, port):
    """Start the web dashboard."""
    import socket
    import uvicorn
    from recovery import config as cfg_mod
    from recovery.db.session import init_db

    cfg = cfg_mod.get()
    p = port or cfg.ui.port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", p)) == 0:
            raise click.ClickException(f"Port {p} is already in use. Is recovery-bot already running?")
    init_db()
    uvicorn.run("recovery.api.app:app", host=host, port=p, reload=False)


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
    import sys
    from pathlib import Path

    config_path = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    # sys.executable, not which("python"): the PATH python may be a different
    # interpreter without fastmcp installed, giving a server that fails to start
    python = sys.executable
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


@cli.command("import-weight")
@click.argument("csv_path", type=click.Path(exists=True))
def import_weight(csv_path):
    """Import TrendWeight CSV export into the database."""
    from datetime import datetime
    from recovery.db.session import get_session, init_db

    session = get_session(init_db())
    try:
        _do_import_weight(session, csv_path, datetime.now())
    finally:
        session.close()


def _do_import_weight(session, csv_path, now):
    import csv
    from datetime import datetime
    from recovery.db.models import WeightEntry

    added = updated = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_date = row["Date"].strip()
            try:
                d = datetime.strptime(raw_date, "%Y-%m-%d").date()
            except ValueError:
                try:
                    d = datetime.strptime(raw_date, "%m/%d/%Y").date()
                except ValueError:
                    continue

            def _float(val):
                v = val.strip() if val else ""
                return float(v) if v else None

            def _bool(val):
                v = val.strip().lower() if val else ""
                return v in ("true", "1", "yes") if v else None

            entry = session.get(WeightEntry, d)
            if entry:
                entry.actual_weight_lbs = _float(row.get("Actual Weight", ""))
                entry.weight_is_interpolated = _bool(row.get("Weight Is Interpolated", ""))
                entry.trend_weight_lbs = _float(row.get("Trend Weight", ""))
                entry.actual_fat_pct = _float(row.get("Actual Fat %", ""))
                entry.fat_is_interpolated = _bool(row.get("Fat Is Interpolated", ""))
                entry.trend_fat_pct = _float(row.get("Trend Fat %", ""))
                entry.imported_at = now
                updated += 1
            else:
                session.add(WeightEntry(
                    date=d,
                    actual_weight_lbs=_float(row.get("Actual Weight", "")),
                    weight_is_interpolated=_bool(row.get("Weight Is Interpolated", "")),
                    trend_weight_lbs=_float(row.get("Trend Weight", "")),
                    actual_fat_pct=_float(row.get("Actual Fat %", "")),
                    fat_is_interpolated=_bool(row.get("Fat Is Interpolated", "")),
                    trend_fat_pct=_float(row.get("Trend Fat %", "")),
                    imported_at=now,
                ))
                added += 1

    session.commit()
    console.print(f"[green]Weight import done. {added} added, {updated} updated.[/green]")


@cli.command()
@click.argument("action", type=click.Choice(["install", "uninstall", "status"]))
def schedule(action):
    """Manage the launchd daily sync job."""
    import subprocess
    from pathlib import Path

    plist_src = Path(__file__).parent.parent / "launchd" / "com.recoverybot.sync.plist"
    plist_dst = Path.home() / "Library" / "LaunchAgents" / "com.recoverybot.sync.plist"

    if action == "install":
        import sys

        plist_dst.parent.mkdir(parents=True, exist_ok=True)
        content = plist_src.read_text()
        content = content.replace("PYTHON_PATH", sys.executable)
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
