import os
import json
import time
import logging
from typing import Dict, Any, List
from pulseflow.config.settings import STORAGE_DIR

logger = logging.getLogger("PulseFlow.Storage")

class ParquetWriter:
    """
    Saves high-frequency microstructure feeds and computed metrics.
    Buffers events in memory and flushes them to disk periodically.
    """
    def __init__(self):
        self.storage_dir = STORAGE_DIR
        os.makedirs(self.storage_dir, exist_ok=True)
        
        self.buffers: Dict[str, List[Dict[str, Any]]] = {
            "trades": [],
            "metrics": [],
            "liquidations": [],
            "signals": [],
            "liq_nodes": []
        }
        
        self.last_flush = time.time()
        self.flush_interval = 10.0 # seconds
        self.has_pyarrow = False
        
        # Check if pyarrow / pandas is available
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            import pandas as pd
            self.has_pyarrow = True
            logger.info("PyArrow and Pandas are available. Storing in compressed Parquet format.")
        except ImportError:
            logger.warning("PyArrow/Pandas not found. Storing in structured JSON Lines format instead.")

    def write_trade(self, symbol: str, price: float, size: float, is_buyer_maker: bool, timestamp: float):
        row = {
            "symbol": symbol,
            "price": price,
            "size": size,
            "is_buyer_maker": is_buyer_maker,
            "timestamp": timestamp
        }
        self.buffers["trades"].append(row)
        self._check_flush()

    def write_metrics(self, symbol: str, metrics: Dict[str, Any], timestamp: float):
        # Flatten metrics for flat storage
        inst = metrics.get("instantaneous", {})
        rel = metrics.get("relative_velocity", {})
        
        row = {
            "symbol": symbol,
            "timestamp": timestamp,
            "aggression_score": metrics.get("aggression_score", 0.0),
            "regime": metrics.get("regime", "normal"),
            "trade_velocity": inst.get("trade_velocity", 0.0),
            "volume_velocity": inst.get("volume_velocity", 0.0),
            "delta_velocity": inst.get("delta_velocity", 0.0),
            "price_velocity": inst.get("price_velocity", 0.0),
            "rel_vol_5s_5m": rel.get("5s_vs_5m", 1.0)
        }
        self.buffers["metrics"].append(row)
        self._check_flush()

    def write_liquidation(self, symbol: str, usd_value: float, side: str, timestamp: float):
        row = {
            "symbol": symbol,
            "usd_value": usd_value,
            "side": side,
            "timestamp": timestamp
        }
        self.buffers["liquidations"].append(row)
        self._check_flush()

    def write_signal(self, symbol: str, signal: Dict[str, Any]):
        row = {
            "symbol": symbol,
            "type": signal.get("type"),
            "priority": signal.get("priority"),
            "direction": signal.get("direction"),
            "confidence": signal.get("confidence"),
            "agg_score": signal.get("agg_score"),
            "regime": signal.get("regime"),
            "state": signal.get("state"),
            "volume_z": signal.get("volume_velocity_z"),
            "delta_z": signal.get("delta_velocity_z"),
            "oi_pct": signal.get("oi_pct_change"),
            "short_liq_usd": signal.get("short_liq_usd"),
            "long_liq_usd": signal.get("long_liq_usd"),
            "message": signal.get("message"),
            "timestamp": signal.get("timestamp"),
        }
        self.buffers["signals"].append(row)
        self._check_flush()

    def write_liquidity_nodes(self, symbol: str, nodes: List[Dict[str, Any]], timestamp: float):
        """Log snapshot fitur LiquidityNode untuk kalibrasi/ML nanti.
        Outcome (tembus/menahan) dilabeli offline via join ke price series."""
        for n in nodes:
            row = {"symbol": symbol, "timestamp": timestamp}
            row.update(n)
            self.buffers["liq_nodes"].append(row)
        self._check_flush()

    def _check_flush(self):
        if time.time() - self.last_flush > self.flush_interval:
            self.flush()

    def flush(self):
        self.last_flush = time.time()
        
        for name, buffer in self.buffers.items():
            if not buffer:
                continue
            
            # Save the contents
            try:
                date_str = time.strftime("%Y-%m-%d")
                if self.has_pyarrow:
                    import pandas as pd
                    import pyarrow as pa
                    import pyarrow.parquet as pq
                    
                    df = pd.DataFrame(buffer)
                    file_path = os.path.join(self.storage_dir, f"{name}_{date_str}.parquet")
                    
                    # Read existing if exists and append
                    table = pa.Table.from_pandas(df)
                    pq.write_table(table, file_path, compression="SNAPPY")
                else:
                    # Fallback JSON Lines format
                    file_path = os.path.join(self.storage_dir, f"{name}_{date_str}.jsonl")
                    with open(file_path, "a") as f:
                        for row in buffer:
                            f.write(json.dumps(row) + "\n")
                            
            except Exception as e:
                logger.error(f"Failed to flush storage buffer {name}: {e}")
            finally:
                buffer.clear()
