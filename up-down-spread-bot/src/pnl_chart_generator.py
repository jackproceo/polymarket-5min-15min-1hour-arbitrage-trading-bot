"""
盈亏图表生成器——为所有 4 个币种生成累计盈亏图表
"""
import matplotlib
matplotlib.use('Agg')  # 非交互式后端
import matplotlib.pyplot as plt
import json
from pathlib import Path
from typing import Dict, List
from datetime import datetime

def load_trades(log_dir: str, coins: List[str]) -> Dict[str, List[Dict]]:
    """从每个币种的 JSONL 文件加载所有交易"""
    all_trades = {}
    
    # 调试：也写入文件
    debug_file = "/root/4coins_live/logs/chart_debug.log"
    with open(debug_file, 'a') as f:
        f.write(f"\n{'='*80}\n")
        f.write(f"[CHART DEBUG] {datetime.now()} load_trades 已调用\n")
        f.write(f"[CHART DEBUG] log_dir = {log_dir}\n")
        f.write(f"[CHART DEBUG] coins = {coins}\n")
    
    print(f"[CHART DEBUG] load_trades 已调用")
    print(f"[CHART DEBUG] log_dir = {log_dir}")
    print(f"[CHART DEBUG] coins = {coins}")
    
    for coin in coins:
        trades_file = Path(log_dir) / f"late_v3_{coin}" / "trades.jsonl"
        trades = []
        
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] 查找：{trades_file}\n")
            f.write(f"[CHART DEBUG] 文件存在：{trades_file.exists()}\n")
        
        print(f"[CHART DEBUG] 查找：{trades_file}")
        print(f"[CHART DEBUG] 文件存在：{trades_file.exists()}")
        
        if trades_file.exists():
            with open(trades_file, 'r') as f:
                for line in f:
                    try:
                        trade = json.loads(line.strip())
                        trades.append(trade)
                    except Exception as e:
                        with open(debug_file, 'a') as df:
                            df.write(f"[CHART DEBUG] 解析行失败：{e}\n")
                        print(f"[CHART DEBUG] 解析行失败：{e}")
            
            with open(debug_file, 'a') as f:
                f.write(f"[CHART DEBUG] 从 {coin} 加载了 {len(trades)} 笔交易\n")
            print(f"[CHART DEBUG] 从 {coin} 加载了 {len(trades)} 笔交易")
        else:
            with open(debug_file, 'a') as f:
                f.write(f"[CHART DEBUG] 未找到文件：{trades_file}\n")
            print(f"[CHART DEBUG] 未找到文件：{trades_file}")
        
        all_trades[coin] = trades
    
    total = sum(len(t) for t in all_trades.values())
    with open(debug_file, 'a') as f:
        f.write(f"[CHART DEBUG] 总加载交易数：{total}\n")
    print(f"[CHART DEBUG] 总加载交易数：{total}")
    
    return all_trades

def generate_pnl_chart(log_dir: str, coins: List[str], output_path: str) -> bool:
    """
    生成所有币种 + 组合的累计盈亏图表
    所有线条使用相同的 X 轴（唯一的市场收盘时间戳）
    
    参数：
        log_dir: 日志目录路径
        coins: 币种名称列表（例如 ['btc', 'eth', 'sol', 'xrp']）
        output_path: 图表保存路径
    
    返回：
        成功创建图表返回 True
    """
    try:
        # 加载所有币种的交易
        all_trades = load_trades(log_dir, coins)
        
        # 检查是否有任何交易
        total_trades = sum(len(trades) for trades in all_trades.values())
        if total_trades == 0:
            print("[CHART] 未找到交易，跳过图表生成")
            return False
        
        # 🔥 关键：去重！避免重复计算预估 + 实际盈亏
        # 每笔交易写入两次：
        # 1. 预估盈亏（无 "updated" 字段）
        # 2. 实际盈亏（有 "updated": true 字段）
        # 只取最终的实际盈亏条目！
        trade_map = {}  # {coin_market_slug: trade_data}
        
        debug_file = "/root/4coins_live/logs/chart_debug.log"
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] 开始去重...\n")
        
        for coin in coins:
            with open(debug_file, 'a') as f:
                f.write(f"[CHART DEBUG] 处理 {len(all_trades[coin])} 笔 {coin} 的交易\n")
            
            for trade in all_trades[coin]:
                market_slug = trade.get('market_slug', '')
                key = f"{coin}_{market_slug}"
                has_updated = trade.get('updated', False)
                
                # 如果条目有 "updated": true——这是最终条目（实际盈亏）
                # 始终用其替换之前的预估条目
                if has_updated:
                    trade_map[key] = {
                        'coin': coin,
                        'close_time': trade.get('close_time', 0),
                        'pnl': trade.get('pnl', 0)
                    }
                # 如果没有 "updated" 且此类条目不存在——添加它
                # （用于没有双重日志记录的旧条目或实际条目未到达的情况）
                elif key not in trade_map:
                    trade_map[key] = {
                        'coin': coin,
                        'close_time': trade.get('close_time', 0),
                        'pnl': trade.get('pnl', 0)
                    }
        
        # 转换为唯一条目列表
        all_trades_timed = list(trade_map.values())
        
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] 去重后：{len(all_trades_timed)} 笔交易\n")
            f.write(f"[CHART DEBUG] trade_map 键示例：{list(trade_map.keys())[:5]}\n")
        
        # 按关闭时间排序
        all_trades_timed.sort(key=lambda x: x['close_time'])
        
        debug_file = "/root/4coins_live/logs/chart_debug.log"
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] 已排序 {len(all_trades_timed)} 笔交易\n")
        
        # 按关闭时间分组（相同时间戳 = 同一点）
        time_groups = {}
        for trade in all_trades_timed:
            close_time = trade['close_time']
            if close_time not in time_groups:
                time_groups[close_time] = []
            time_groups[close_time].append(trade)
        
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] 分组为 {len(time_groups)} 个时间点\n")
        
        # 创建统一时间线
        unique_times = sorted(time_groups.keys())
        
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] unique_times 计数：{len(unique_times)}\n")
        
        # 计算组合的累计盈亏（使用分组时间戳）
        combined_pnl = []
        current_combined = 0
        for time in unique_times:
            # 汇总此时刻的所有盈亏变化
            time_pnl = sum(t['pnl'] for t in time_groups[time])
            current_combined += time_pnl
            combined_pnl.append(current_combined)
        
        # 组合的 X 轴（1 到 N 个唯一时间戳）
        combined_indices = list(range(1, len(combined_pnl) + 1))
        
        # 在相同时间线上计算每个币种的累计盈亏
        # 🔥 修复：使用 trade_map 中去重后的交易，而不是原始的 all_trades！
        coin_cumulative = {}
        coin_indices = {}
        
        for coin in coins:
            # 从 all_trades_timed 获取此币种去重后的交易
            coin_trades = [t for t in all_trades_timed if t['coin'] == coin]
            if not coin_trades:
                continue
            
            # 按关闭时间排序
            coin_trades.sort(key=lambda x: x['close_time'])
            
            # 将币种交易映射到统一时间线
            cumulative = []
            coin_times = []
            current_pnl = 0
            
            for trade in coin_trades:
                current_pnl += trade['pnl']
                close_time = trade['close_time']
                
                # 在统一时间线中查找位置
                try:
                    timeline_index = unique_times.index(close_time) + 1
                    cumulative.append(current_pnl)
                    coin_times.append(timeline_index)
                except ValueError:
                    # close_time 不在 unique_times 中（不应发生，但做安全检查）
                    pass
            
            coin_cumulative[coin] = cumulative
            coin_indices[coin] = coin_times
        
        # 创建图形
        fig, ax = plt.subplots(figsize=(14, 8))
        
        # 每个币种的颜色
        colors = {
            'btc': '#F7931A',  # 比特币橙色
            'eth': '#627EEA',  # 以太坊蓝色
            'sol': '#9945FF',  # 索拉纳紫色
            'xrp': '#23292F',  # XRP 黑色
        }
        
        # 先绘制组合线（更粗，作为背景）
        if combined_pnl:
            combined_color = '#2ecc71' if combined_pnl[-1] >= 0 else '#e74c3c'
            ax.plot(combined_indices, combined_pnl,
                   label=f'组合（${combined_pnl[-1]:+.0f}）',
                   color=combined_color,
                   linewidth=4,
                   marker='s',
                   markersize=6,
                   alpha=0.9,
                   zorder=10)
        
        # 绘制每个币种的线（使用时间线索引）
        for coin in coins:
            if coin not in coin_cumulative:
                continue
            
            cumulative = coin_cumulative[coin]
            indices = coin_indices[coin]
            
            ax.plot(indices, cumulative, 
                   label=f'{coin.upper()}（${cumulative[-1]:+.0f}）',
                   color=colors.get(coin, '#888888'),
                   linewidth=2,
                   marker='o',
                   markersize=4,
                   alpha=0.7,
                   zorder=5)
        
        # 样式
        ax.axhline(y=0, color='gray', linestyle='--', linewidth=1, alpha=0.5)
        ax.grid(True, alpha=0.3, linestyle=':', linewidth=0.5)
        ax.set_xlabel('市场收盘事件', fontsize=12, fontweight='bold')
        ax.set_ylabel('累计盈亏（$）', fontsize=12, fontweight='bold')
        
        # 设置 X 轴范围到统一时间线
        ax.set_xlim(0.5, len(unique_times) + 0.5)
        
        # 带时间戳的标题
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        ax.set_title(f'Meridian — 投资组合表现\n{now}', 
                    fontsize=16, fontweight='bold', pad=20)
        
        # 图例
        ax.legend(loc='best', fontsize=11, framealpha=0.95, shadow=True)
        
        # 在底部添加统计文本
        # 🔥 修复：对统计使用去重后的交易，而不是原始的 all_trades！
        stats_lines = []
        stats_lines.append(f"总市场数：{len(all_trades_timed)}  •  事件数：{len(unique_times)}")
        
        for coin in coins:
            # 获取此币种去重后的交易
            coin_trades = [t for t in all_trades_timed if t['coin'] == coin]
            if coin_trades:
                wins = sum(1 for t in coin_trades if t.get('pnl', 0) > 0)
                wr = (wins / len(coin_trades) * 100) if coin_trades else 0
                final_pnl = coin_cumulative.get(coin, [0])[-1] if coin in coin_cumulative else 0
                # 使用 USD 而非 $ 以避免 matplotlib LaTeX 解析
                stats_lines.append(f"{coin.upper()}：{len(coin_trades)} 市场 | {final_pnl:+.0f} USD | {wr:.0f}% 胜率")
        
        stats_text = "  •  ".join(stats_lines)
        
        ax.text(0.5, 0.02, stats_text, 
               transform=ax.transAxes,
               ha='center',
               fontsize=9,
               bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))
        
        # 紧凑布局并保存
        plt.tight_layout()
        
        debug_file = "/root/4coins_live/logs/chart_debug.log"
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] 即将保存图表到：{output_path}\n")
        
        plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor='white')
        plt.close()
        
        with open(debug_file, 'a') as f:
            f.write(f"[CHART DEBUG] 图表保存成功！\n")
        
        print(f"[CHART] ✓ 已生成盈亏图表：{output_path}")
        return True
        
    except Exception as e:
        debug_file = "/root/4coins_live/logs/chart_debug.log"
        with open(debug_file, 'a') as f:
            f.write(f"[CHART ERROR] 异常：{str(e)}\n")
            import traceback
            f.write(f"[CHART ERROR] 回溯：\n")
            f.write(traceback.format_exc())
        
        print(f"[CHART] ✗ 生成图表时出错：{e}")
        import traceback
        traceback.print_exc()
        return False
