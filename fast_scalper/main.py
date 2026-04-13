import asyncio
import json
import logging
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

import ccxt.async_support as ccxt
import yaml
from rich.logging import RichHandler


@dataclass
class TradeConfig:
    symbol: str
    leverage: int
    capital_usdt: float
    fee_open: float
    fee_close: float
    slippage_buffer: float
    min_notional_usdt: float
    independent_trade_id: str


@dataclass
class RuntimeConfig:
    poll_interval_ms: int
    reload_config_seconds: int
    max_retries: int


@dataclass
class ExchangeConfig:
    exchange_id: str
    api_key: str
    secret: str
    password: str
    testnet: bool


@dataclass
class BotConfig:
    exchange: ExchangeConfig
    runtime: RuntimeConfig
    trade: TradeConfig


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT NOT NULL,
                side TEXT NOT NULL,
                symbol TEXT NOT NULL,
                amount REAL NOT NULL,
                price REAL,
                exchange_order_id TEXT,
                status TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    def load_state(self, trade_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT state_json FROM bot_state WHERE id = ?", (trade_id,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def save_state(self, trade_id: str, data: Dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO bot_state(id, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
            """,
            (trade_id, json.dumps(data), int(time.time())),
        )
        self.conn.commit()

    def log_order(self, trade_id: str, side: str, symbol: str, amount: float, price: Optional[float], order: Dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO order_log(trade_id, side, symbol, amount, price, exchange_order_id, status, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                side,
                symbol,
                amount,
                price,
                str(order.get("id", "")),
                order.get("status", "unknown"),
                json.dumps(order, ensure_ascii=False),
                int(time.time()),
            ),
        )
        self.conn.commit()


class FastScalper:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = self._load_config()
        self.store = StateStore(Path("fast_scalper_state.db"))
        self.log = logging.getLogger("fast_scalper")
        self.exchange = self._build_exchange()
        self.should_stop = False
        self.last_reload = 0.0

        self.state = self.store.load_state(self.config.trade.independent_trade_id) or {
            "position_size": 0.0,
            "entry_price": 0.0,
            "breakeven_armed": False,
            "last_action": "init",
            "last_order_id": None,
        }

    def _load_config(self) -> BotConfig:
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        return BotConfig(
            exchange=ExchangeConfig(
                exchange_id=raw["exchange"]["id"],
                api_key=raw["exchange"]["api_key"],
                secret=raw["exchange"]["secret"],
                password=raw["exchange"].get("password", ""),
                testnet=bool(raw["exchange"].get("testnet", True)),
            ),
            runtime=RuntimeConfig(
                poll_interval_ms=int(raw["runtime"]["poll_interval_ms"]),
                reload_config_seconds=int(raw["runtime"]["reload_config_seconds"]),
                max_retries=int(raw["runtime"]["max_retries"]),
            ),
            trade=TradeConfig(
                symbol=raw["trade"]["symbol"],
                leverage=int(raw["trade"]["leverage"]),
                capital_usdt=float(raw["trade"]["capital_usdt"]),
                fee_open=float(raw["trade"]["fee_open"]),
                fee_close=float(raw["trade"]["fee_close"]),
                slippage_buffer=float(raw["trade"]["slippage_buffer"]),
                min_notional_usdt=float(raw["trade"]["min_notional_usdt"]),
                independent_trade_id=raw["trade"]["independent_trade_id"],
            ),
        )

    def _build_exchange(self):
        klass = getattr(ccxt, self.config.exchange.exchange_id)
        exchange = klass(
            {
                "apiKey": self.config.exchange.api_key,
                "secret": self.config.exchange.secret,
                "password": self.config.exchange.password,
                "enableRateLimit": True,
            }
        )
        if self.config.exchange.testnet and hasattr(exchange, "set_sandbox_mode"):
            exchange.set_sandbox_mode(True)
        return exchange

    async def initialize(self):
        markets = await self.exchange.load_markets()
        if self.config.trade.symbol not in markets:
            raise ValueError(f"symbol {self.config.trade.symbol} 不存在")
        await self._set_leverage_safe()
        await self.reconcile_open_position()
        self.log.info("初始化完成，交易对=%s", self.config.trade.symbol)

    async def _set_leverage_safe(self):
        try:
            await self.exchange.set_leverage(self.config.trade.leverage, self.config.trade.symbol)
        except Exception as e:
            self.log.warning("设置杠杆失败（部分交易所不支持统一接口）：%s", e)

    async def reconcile_open_position(self):
        """重启恢复：尽量从交易所恢复当前持仓。"""
        try:
            positions = await self.exchange.fetch_positions([self.config.trade.symbol])
            for p in positions:
                contracts = float(p.get("contracts") or p.get("positionAmt") or 0)
                if abs(contracts) > 0:
                    entry = float(p.get("entryPrice") or p.get("entry_price") or p.get("markPrice") or 0)
                    self.state["position_size"] = abs(contracts)
                    self.state["entry_price"] = entry
                    self.state["last_action"] = "recovered"
                    self.store.save_state(self.config.trade.independent_trade_id, self.state)
                    self.log.info("已恢复持仓 size=%s entry=%s", contracts, entry)
                    return
        except Exception as e:
            self.log.warning("恢复持仓失败，继续本地状态: %s", e)

    async def run(self):
        while not self.should_stop:
            await self._maybe_reload_config()
            try:
                ticker = await self.exchange.fetch_ticker(self.config.trade.symbol)
                last = float(ticker["last"])

                if self.state["position_size"] <= 0:
                    await self.open_long(last)
                else:
                    await self.manage_position(last)
            except Exception as e:
                self.log.error("主循环异常: %s", e)

            await asyncio.sleep(self.config.runtime.poll_interval_ms / 1000)

    async def _maybe_reload_config(self):
        now = time.time()
        if now - self.last_reload < self.config.runtime.reload_config_seconds:
            return
        self.last_reload = now
        old_symbol = self.config.trade.symbol
        new_cfg = self._load_config()
        self.config = new_cfg
        if old_symbol != new_cfg.trade.symbol:
            self.log.info("交易对更新: %s -> %s", old_symbol, new_cfg.trade.symbol)
        await self._set_leverage_safe()

    async def open_long(self, last_price: float):
        notional = max(self.config.trade.capital_usdt * self.config.trade.leverage, self.config.trade.min_notional_usdt)
        amount = notional / last_price

        order = await self.exchange.create_market_buy_order(self.config.trade.symbol, amount)
        self.state.update(
            {
                "position_size": amount,
                "entry_price": last_price,
                "breakeven_armed": False,
                "last_action": "opened_long",
                "last_order_id": order.get("id"),
            }
        )
        self.store.save_state(self.config.trade.independent_trade_id, self.state)
        self.store.log_order(
            self.config.trade.independent_trade_id,
            "buy",
            self.config.trade.symbol,
            amount,
            last_price,
            order,
        )
        self.log.info("开多成功 amount=%.6f price=%.2f", amount, last_price)

    async def manage_position(self, last_price: float):
        entry = self.state["entry_price"]
        breakeven = entry * (
            1
            + self.config.trade.fee_open
            + self.config.trade.fee_close
            + self.config.trade.slippage_buffer
        )

        ohlcv = await self.exchange.fetch_ohlcv(self.config.trade.symbol, timeframe="1s", limit=2)
        current = ohlcv[-1]
        candle_open = float(current[1])
        candle_close = float(current[4])

        if not self.state["breakeven_armed"] and last_price >= breakeven:
            self.state["breakeven_armed"] = True
            self.state["last_action"] = "breakeven_armed"
            self.store.save_state(self.config.trade.independent_trade_id, self.state)
            self.log.info("已触发保本线，entry=%.2f breakeven=%.2f", entry, breakeven)
            return

        if self.state["breakeven_armed"] and candle_close < candle_open:
            await self.close_position(last_price)

    async def close_position(self, last_price: float):
        amount = self.state["position_size"]
        params = {"reduceOnly": True}
        order = await self.exchange.create_market_sell_order(self.config.trade.symbol, amount, params=params)
        self.store.log_order(
            self.config.trade.independent_trade_id,
            "sell",
            self.config.trade.symbol,
            amount,
            last_price,
            order,
        )
        self.log.info("止盈平仓 amount=%.6f price=%.2f", amount, last_price)

        self.state.update(
            {
                "position_size": 0.0,
                "entry_price": 0.0,
                "breakeven_armed": False,
                "last_action": "closed",
                "last_order_id": order.get("id"),
            }
        )
        self.store.save_state(self.config.trade.independent_trade_id, self.state)

    async def shutdown(self):
        self.should_stop = True
        await self.exchange.close()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True)],
    )


async def main() -> None:
    setup_logging()
    config_path = Path("config.yml")
    if not config_path.exists():
        raise FileNotFoundError("请先复制 config.example.yml 为 config.yml 并填写API信息")

    bot = FastScalper(config_path)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _graceful_stop(*_args):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _graceful_stop)

    await bot.initialize()
    runner = asyncio.create_task(bot.run())

    await stop_event.wait()
    await bot.shutdown()
    await runner


if __name__ == "__main__":
    asyncio.run(main())
