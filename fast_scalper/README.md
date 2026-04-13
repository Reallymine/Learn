# Fast Scalper (合约快进快出示例)

> 这是一个可直接运行的**最小可用项目**，实现你提出的逻辑：
> - 市价开多
> - 达到保本线（含手续费 + 滑点缓冲）后进入保护状态
> - 后续若出现亏损秒K（close < open）则立即市价止盈
> - 每个 `independent_trade_id` 独立状态
> - 支持运行中热更新 `symbol / capital / leverage`
> - 中断后重启可恢复持仓（依赖交易所接口能力）
> - SQLite 记录状态和订单日志

## 1. 安装

```bash
cd fast_scalper
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yml config.yml
```

填写 `config.yml` 中的 API Key。

## 2. 启动

```bash
python main.py
```

## 3. 运行中修改参数

直接编辑 `config.yml`，程序默认每 5 秒热加载一次，可修改：
- `trade.symbol`
- `trade.capital_usdt`
- `trade.leverage`
- `trade.independent_trade_id`

## 4. 数据与日志

- 状态 + 订单日志数据库：`fast_scalper_state.db`
- 主要表：
  - `bot_state`
  - `order_log`

你可以直接查询历史订单：

```bash
sqlite3 fast_scalper_state.db "select id,trade_id,side,symbol,amount,price,status,created_at from order_log order by id desc limit 20;"
```

## 5. 注意事项（务必阅读）

1. 此项目是工程模板，不构成投资建议。
2. 秒K策略不等于毫秒级优势，真实延迟主要取决于机房位置、网络和交易所撮合。
3. 不同交易所对 `reduceOnly` / `fetch_positions` / `1s K线` 支持差异很大，请先用 testnet。
4. 推荐先做 dry-run 或极小仓位验证。

## 6. 下一步可扩展

- 把 `fetch_ticker + fetch_ohlcv` 替换为 WebSocket 流式行情
- 加入订单簿不平衡过滤（减少假突破）
- 增加风控（连续亏损熔断、日亏损上限）
- 增加 Web 面板（动态改参数 + 查看历史）
