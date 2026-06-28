"""
从配置解析 Polymarket 市场窗口：用户友好的 market_window + market_interval_sec。
"""


def apply_market_window_settings(cfg: dict) -> None:
    """
    就地修改 cfg：设置 data_sources.polymarket.market_interval_sec。

    优先级：
    1. market_window："5m" 或 "15m"（也接受 5min、15min、5、15）
    2. 已有的 market_interval_sec（例如 300 或 900）
    3. 默认 900（15m）
    """
    ds = cfg.get("data_sources")
    if not isinstance(ds, dict):
        return
    pm = ds.get("polymarket")
    if not isinstance(pm, dict):
        return

    mw = str(pm.get("market_window", "")).strip().lower()
    if mw in ("5m", "5min", "5"):
        pm["market_interval_sec"] = 300
        return
    if mw in ("15m", "15min", "15"):
        pm["market_interval_sec"] = 900
        return

    sec = pm.get("market_interval_sec")
    if sec is not None:
        try:
            pm["market_interval_sec"] = int(sec)
        except (TypeError, ValueError):
            pm["market_interval_sec"] = 900
        return

    pm["market_interval_sec"] = 900
