"""Web Dashboard — minimal async HTTP server for bot observability.

Single-page dashboard with auto-refresh. No external JS/CSS dependencies,
no build step. Uses only the Python stdlib ``asyncio`` module for HTTP.

Routes:
  GET /           → HTML dashboard (auto-refresh every 5s)
  GET /api/status → JSON bot_snapshot()
  GET /api/lots   → JSON open lots
  GET /api/metrics→ Prometheus text
  POST /api/pause → Force risk pause
  POST /api/resume→ Force resume

Config:
  [web]
  enabled = true
  port = 8080
  host = "127.0.0.1"
  username = ""
  password = ""
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from icryptotrader.metrics import MetricsRegistry
    from icryptotrader.risk.risk_manager import RiskManager
    from icryptotrader.strategy.strategy_loop import StrategyLoop

logger = logging.getLogger(__name__)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: object) -> Any:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class WebDashboard:
    """Async HTTP dashboard server.

    Usage:
        dash = WebDashboard(strategy_loop=loop, port=8080)
        await dash.start()
        # ... later ...
        await dash.stop()
    """

    def __init__(
        self,
        strategy_loop: StrategyLoop | None = None,
        risk_manager: RiskManager | None = None,
        metrics_registry: MetricsRegistry | None = None,
        host: str = "127.0.0.1",
        port: int = 8080,
        username: str = "",
        password: str = "",
    ) -> None:
        self._loop = strategy_loop
        self._risk = risk_manager
        self._metrics = metrics_registry
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._server: asyncio.Server | None = None

    def set_loop(self, strategy_loop: StrategyLoop) -> None:
        """Set the strategy loop reference (for deferred construction)."""
        self._loop = strategy_loop

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port,
        )
        logger.info("Web dashboard on http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Web dashboard stopped")

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return

            line = request_line.decode("utf-8", errors="replace").strip()
            parts = line.split()
            method = parts[0] if parts else "GET"
            path = parts[1] if len(parts) > 1 else "/"

            # Read headers
            headers: dict[str, str] = {}
            while True:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if header_line in (b"\r\n", b"\n", b""):
                    break
                decoded = header_line.decode("utf-8", errors="replace").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            # Auth check
            if self._username and self._password and not self._check_auth(headers):
                self._send(writer, 401, "Unauthorized", extra_headers={
                    "WWW-Authenticate": 'Basic realm="iCryptoTrader"',
                })
                return

            # Route
            if path == "/" and method == "GET":
                self._send(writer, 200, _DASHBOARD_HTML, content_type="text/html")
            elif path == "/api/status" and method == "GET":
                if self._loop is None:
                    self._send(writer, 503, '{"error": "not ready"}',
                               content_type="application/json")
                else:
                    snap = self._loop.bot_snapshot()
                    body = json.dumps(snap.__dict__, cls=_DecimalEncoder, indent=2)
                    self._send(writer, 200, body, content_type="application/json")
            elif path == "/api/lots" and method == "GET":
                if self._loop is None:
                    self._send(writer, 503, '{"error": "not ready"}',
                               content_type="application/json")
                else:
                    lots = self._loop._ledger.open_lots()
                data = [
                    {"lot_id": lot.lot_id[:8], "qty": str(lot.remaining_qty_btc),
                     "days_held": lot.days_held, "tax_free": lot.is_tax_free}
                    for lot in lots
                ]
                self._send(writer, 200, json.dumps(data, indent=2),
                           content_type="application/json")
            elif path == "/api/metrics" and method == "GET":
                if self._metrics:
                    self._send(writer, 200, self._metrics.format_prometheus(),
                               content_type="text/plain")
                else:
                    self._send(writer, 200, "# no metrics\n",
                               content_type="text/plain")
            elif path == "/api/pause" and method == "POST":
                if self._risk:
                    self._risk.force_risk_pause()
                self._send(writer, 200, '{"status":"paused"}',
                           content_type="application/json")
            elif path == "/api/resume" and method == "POST":
                if self._risk:
                    self._risk.force_active()
                self._send(writer, 200, '{"status":"active"}',
                           content_type="application/json")
            else:
                self._send(writer, 404, "Not Found")
        except (TimeoutError, ConnectionError):
            pass
        except Exception:
            logger.warning("Dashboard request error", exc_info=True)
            self._send(writer, 500, "Internal Server Error")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: str,
        content_type: str = "text/plain",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        status_text = {200: "OK", 401: "Unauthorized", 404: "Not Found",
                       500: "Internal Server Error"}.get(status, "")
        encoded = body.encode()
        lines = [
            f"HTTP/1.1 {status} {status_text}",
            f"Content-Type: {content_type}; charset=utf-8",
            f"Content-Length: {len(encoded)}",
            "Connection: close",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}")
        header = "\r\n".join(lines) + "\r\n\r\n"
        writer.write(header.encode() + encoded)

    def _check_auth(self, headers: dict[str, str]) -> bool:
        auth = headers.get("authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, pw = decoded.split(":", 1)
            return user == self._username and pw == self._password
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Embedded HTML dashboard — no build step, no dependencies
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iCryptoTrader Dashboard</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:system-ui,-apple-system,sans-serif; background:#0d1117; color:#c9d1d9; padding:1rem; }
h1 { color:#58a6ff; margin-bottom:1rem; font-size:1.4rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:1rem; }
.card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:1rem; }
.card h2 { font-size:1rem; color:#8b949e; margin-bottom:.5rem; border-bottom:1px solid #21262d; padding-bottom:.3rem; }
.row { display:flex; justify-content:space-between; padding:.2rem 0; }
.label { color:#8b949e; }
.value { color:#f0f6fc; font-weight:600; font-variant-numeric:tabular-nums; }
.status-active { color:#3fb950; } .status-paused { color:#f85149; }
.status-tax { color:#d29922; } .status-emergency { color:#f85149; font-weight:bold; }
.bar { background:#21262d; border-radius:4px; height:8px; margin-top:4px; }
.bar-fill { background:#58a6ff; height:100%; border-radius:4px; transition:width .3s; }
.btn { background:#21262d; color:#c9d1d9; border:1px solid #30363d; border-radius:4px;
  padding:.4rem .8rem; cursor:pointer; margin:.2rem; }
.btn:hover { background:#30363d; }
.updated { color:#484f58; font-size:.8rem; text-align:right; margin-top:.5rem; }
</style>
</head>
<body>
<h1>iCryptoTrader</h1>
<div class="grid" id="app">Loading...</div>
<div class="updated" id="ts"></div>
<script>
async function refresh(){
  try{
    const r=await fetch('/api/status');
    const s=await r.json();
    const pc=s.pause_state||'ACTIVE_TRADING';
    const cls=pc==='ACTIVE_TRADING'?'status-active':
      pc.includes('TAX')?'status-tax':
      pc.includes('EMERGENCY')?'status-emergency':'status-paused';
    const ddPct=(s.drawdown_pct*100).toFixed(1);
    const allocPct=(s.btc_allocation_pct*100).toFixed(1);
    const sellRatio=(s.sellable_ratio*100).toFixed(0);
    document.getElementById('app').innerHTML=`
    <div class="card"><h2>Portfolio</h2>
      <div class="row"><span class="label">Value</span><span class="value">$${num(s.portfolio_value_usd)}</span></div>
      <div class="row"><span class="label">BTC</span><span class="value">${Number(s.btc_balance).toFixed(8)}</span></div>
      <div class="row"><span class="label">USD</span><span class="value">$${num(s.usd_balance)}</span></div>
      <div class="row"><span class="label">Allocation</span><span class="value">${allocPct}% BTC</span></div>
    </div>
    <div class="card"><h2>Risk</h2>
      <div class="row"><span class="label">Status</span><span class="value ${cls}">${pc}</span></div>
      <div class="row"><span class="label">Drawdown</span><span class="value">${ddPct}%</span></div>
      <div class="bar"><div class="bar-fill" style="width:${Math.min(ddPct*5,100)}%;background:${ddPct>10?'#f85149':ddPct>5?'#d29922':'#3fb950'}"></div></div>
      <div class="row"><span class="label">HWM</span><span class="value">$${num(s.high_water_mark_usd)}</span></div>
      <div style="margin-top:.5rem">
        <button class="btn" onclick="post('/api/pause')">Pause</button>
        <button class="btn" onclick="post('/api/resume')">Resume</button>
      </div>
    </div>
    <div class="card"><h2>Trading</h2>
      <div class="row"><span class="label">Regime</span><span class="value">${s.regime}</span></div>
      <div class="row"><span class="label">Orders</span><span class="value">${s.active_orders}/${s.grid_levels}</span></div>
      <div class="row"><span class="label">Ticks</span><span class="value">${s.ticks?.toLocaleString()}</span></div>
      <div class="row"><span class="label">Latency</span><span class="value">${s.last_tick_ms?.toFixed(1)}ms</span></div>
      <div class="row"><span class="label">Fills today</span><span class="value">${s.fills_today}</span></div>
      <div class="row"><span class="label">P&L today</span><span class="value">$${num(s.profit_today_usd)}</span></div>
    </div>
    <div class="card"><h2>Tax (DE &sect;23)</h2>
      <div class="row"><span class="label">YTD Gain</span><span class="value">&euro;${num(s.ytd_taxable_gain_eur)}</span></div>
      <div class="row"><span class="label">Tax-free BTC</span><span class="value">${Number(s.tax_free_btc).toFixed(8)}</span></div>
      <div class="row"><span class="label">Locked BTC</span><span class="value">${Number(s.locked_btc).toFixed(8)}</span></div>
      <div class="row"><span class="label">Sellable</span><span class="value">${sellRatio}%</span></div>
      <div class="bar"><div class="bar-fill" style="width:${sellRatio}%"></div></div>
      <div class="row"><span class="label">Open lots</span><span class="value">${s.open_lots}</span></div>
      <div class="row"><span class="label">Next unlock</span><span class="value">${s.days_until_unlock??'N/A'}d</span></div>
    </div>
    <div class="card"><h2>AI Signal</h2>
      <div class="row"><span class="label">Direction</span><span class="value">${s.ai_direction||'NEUTRAL'}</span></div>
      <div class="row"><span class="label">Confidence</span><span class="value">${((s.ai_confidence||0)*100).toFixed(0)}%</span></div>
      <div class="row"><span class="label">Provider</span><span class="value">${s.ai_provider||'disabled'}</span></div>
      <div class="row"><span class="label">Calls/Errors</span><span class="value">${s.ai_call_count||0}/${s.ai_error_count||0}</span></div>
    </div>`;
    document.getElementById('ts').textContent='Updated: '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('ts').textContent='Error: '+e.message;}
}
function num(v){return Number(v||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})}
async function post(url){await fetch(url,{method:'POST'});refresh();}
refresh();setInterval(refresh,5000);
</script>
</body>
</html>"""
