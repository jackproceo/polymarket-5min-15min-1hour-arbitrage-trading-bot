"""
Polymarket API 集成——用于市场结果验证
"""

import requests
import json
from typing import Optional, Dict

GAMMA_API = "https://gamma-api.polymarket.com"

def get_market_outcome(slug: str, timeout: int = 10) -> Dict:
    """
    从 Polymarket API 获取市场结果
    
    返回：
        {
            "success": bool,
            "winner": "UP" | "DOWN" | None,
            "resolved": bool,
            "closed": bool,
            "error": str (if success=False)
        }
    """
    try:
        url = f"{GAMMA_API}/events?slug={slug}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        
        events = resp.json()
        if not events or len(events) == 0:
            return {
                "success": False,
                "error": f"Market not found in API: {slug}"
            }
        
        event = events[0]
        markets = event.get("markets", [])
        
        if not markets:
            return {
                "success": False,
                "error": f"No markets in event: {slug}"
            }
        
        market = markets[0]
        
        # 解析结果和价格
        outcomes = market.get("outcomes", [])
        prices = market.get("outcomePrices", [])
        
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        
        # 获取状态
        closed = market.get("closed", False)
        resolved = market.get("resolved", False)
        
        # 通过价格确定赢家（赢家价格为 $1.00）
        winner = None
        if prices and len(prices) >= 2:
            price_up = float(prices[0])
            price_down = float(prices[1])
            
            if price_up > 0.99:
                winner = "UP"
            elif price_down > 0.99:
                winner = "DOWN"
        
        return {
            "success": True,
            "winner": winner,
            "resolved": resolved,
            "closed": closed,
            "outcomes": outcomes,
            "prices": prices
        }
        
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": f"API timeout for {slug}"
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "error": f"API request failed: {str(e)}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }
