"""
Market symbols seed data and lookup functions.

Data is stored in PostgreSQL table `qd_market_symbols` (initialized via migrations/init.sql).
This module provides helper functions to query hot symbols, search, and get symbol names.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)


def _get_db_connection():
    """Get database connection, returns None if not available."""
    try:
        from app.utils.db import get_db_connection
        return get_db_connection()
    except Exception:
        return None


def get_hot_symbols(market: str, limit: int = 10) -> List[Dict]:
    """
    Get hot symbols for a market.
    
    Args:
        market: Market name (e.g., 'Crypto', 'USStock', 'Forex')
        limit: Maximum number of results
        
    Returns:
        List of {market, symbol, name} dicts
    """
    market = (market or '').strip()
    if not market:
        return []
    
    # CNStock: return curated hot A-share symbols from PG stocks table
    if market == 'CNStock':
        return _hot_cn_stocks(limit)
    
    try:
        with _get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT market, symbol, name FROM qd_market_symbols
                WHERE market = ? AND is_active = 1 AND is_hot = 1
                ORDER BY sort_order DESC
                LIMIT ?
                """,
                (market, max(limit, 0))
            )
            rows = cur.fetchall() or []
            cur.close()
            return [{'market': r['market'], 'symbol': r['symbol'], 'name': r.get('name') or ''} for r in rows]
    except Exception as e:
        logger.debug(f"get_hot_symbols from DB failed: {e}")
        return []


def search_symbols(market: str, keyword: str, limit: int = 20) -> List[Dict]:
    """
    Search symbols by keyword.
    
    Args:
        market: Market name
        keyword: Search keyword (matches symbol or name)
        limit: Maximum number of results
        
    Returns:
        List of {market, symbol, name} dicts
    """
    market = (market or '').strip()
    kw = (keyword or '').strip()
    if not market or not kw:
        return []
    
    # For CNStock / HKStock, query the quant_sys PostgreSQL stocks table directly.
    # This avoids needing to pre-seed 8,234 A-share symbols into qd_market_symbols.
    if market in ('CNStock', 'HKStock'):
        return _search_from_quant_stocks(market, kw, limit)
    
    # Use ILIKE for case-insensitive search in PostgreSQL
    pattern = f'%{kw}%'
    
    try:
        with _get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT market, symbol, name FROM qd_market_symbols
                WHERE market = ? AND is_active = 1
                  AND (UPPER(symbol) LIKE UPPER(?) OR UPPER(name) LIKE UPPER(?))
                ORDER BY sort_order DESC
                LIMIT ?
                """,
                (market, pattern, pattern, max(limit, 0))
            )
            rows = cur.fetchall() or []
            cur.close()
            return [{'market': r['market'], 'symbol': r['symbol'], 'name': r.get('name') or ''} for r in rows]
    except Exception as e:
        logger.debug(f"search_symbols from DB failed: {e}")
        return []


def _search_from_quant_stocks(market: str, keyword: str, limit: int) -> List[Dict]:
    """Search CNStock/HKStock symbols in PostgreSQL quant_sys stocks table."""
    import psycopg2
    from app.config.settings import Config
    
    dsn = getattr(Config, 'QUANT_SYS_DATABASE_URL', None) or \
          'postgresql://james@localhost:5432/investassist'
    
    pg_table = 'stocks' if market == 'CNStock' else 'hk_basic'
    kw_upper = keyword.upper()
    kw_no_suffix = kw_upper.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    
    try:
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        
        if market == 'CNStock':
            cur.execute("""
                SELECT ts_code, symbol, name, area, industry, list_status
                FROM stocks
                WHERE symbol LIKE %s OR UPPER(name) LIKE %s OR ts_code LIKE %s
                ORDER BY 
                    CASE WHEN symbol = %s THEN 0
                         WHEN symbol LIKE %s THEN 1
                         ELSE 2 END,
                    symbol
                LIMIT %s
            """, (f'{kw_no_suffix}%', f'%{kw_upper}%', f'%{kw_no_suffix}%',
                  kw_no_suffix, f'{kw_no_suffix}%', limit))
        else:
            cur.execute("""
                SELECT ts_code, symbol, name FROM hk_basic
                WHERE symbol LIKE %s OR UPPER(name) LIKE %s OR ts_code LIKE %s
                ORDER BY symbol
                LIMIT %s
            """, (f'{kw_no_suffix}%', f'%{kw_upper}%', f'%{kw_no_suffix}%', limit))
        
        rows = cur.fetchall()
        results = []
        for row in rows:
            if market == 'CNStock':
                ts_code, symbol, name = row[0], row[1], row[2]
            else:
                ts_code, symbol, name = row[0], row[1], row[2]
            results.append({
                'market': market,
                'symbol': symbol if market == 'CNStock' else ts_code,
                'name': name,
            })
        
        cur.close()
        conn.close()
        return results
    except Exception as e:
        logger.warning(f"_search_from_quant_stocks failed: {e}")
        return []


def _hot_cn_stocks(limit: int) -> List[Dict]:
    """Return curated hot A-share symbols."""
    hot_codes = [
        ('600519', '贵州茅台'), ('000858', '五粮液'), ('601318', '中国平安'),
        ('000333', '美的集团'), ('600036', '招商银行'), ('002415', '海康威视'),
        ('601012', '隆基绿能'), ('300750', '宁德时代'), ('600276', '恒瑞医药'),
        ('002594', '比亚迪'), ('601398', '工商银行'), ('688981', '中芯国际'),
        ('600900', '长江电力'), ('000001', '平安银行'), ('601857', '中国石油'),
        ('600030', '中信证券'), ('000651', '格力电器'), ('002475', '立讯精密'),
        ('601899', '紫金矿业'), ('603259', '药明康德'),
    ]
    return [
        {'market': 'CNStock', 'symbol': code, 'name': name}
        for code, name in hot_codes[:limit]
    ]


def _normalize_for_match(market: str, symbol: str) -> str:
    """Normalize symbol for matching."""
    m = (market or '').strip()
    s = (symbol or '').strip().upper()
    if not m or not s:
        return s

    return s


def get_symbol_name(market: str, symbol: str) -> Optional[str]:
    """
    Get display name for a symbol.
    
    Args:
        market: Market name
        symbol: Symbol (e.g., 'AAPL', 'BTC/USDT', '600519')
        
    Returns:
        Symbol name or None if not found
    """
    m = (market or '').strip()
    if not m:
        return None

    s = _normalize_for_match(m, symbol)
    if not s:
        return None

    # Crypto: allow user to pass BTC (try BTC/USDT) or full pair
    candidate_symbols = [s]
    if m == 'Crypto' and '/' not in s:
        candidate_symbols.append(f"{s}/USDT")

    # CNStock / HKStock: fall back to PG stocks table when seed table misses
    if m in ('CNStock', 'HKStock'):
        results = _search_from_quant_stocks(m, s, 1)
        if results:
            return results[0].get('name')

    try:
        with _get_db_connection() as db:
            cur = db.cursor()
            for cand in candidate_symbols:
                cur.execute(
                    "SELECT name FROM qd_market_symbols WHERE market = ? AND UPPER(symbol) = ?",
                    (m, cand.upper())
                )
                row = cur.fetchone()
                if row and row.get('name'):
                    cur.close()
                    return str(row['name'])
            cur.close()
    except Exception as e:
        logger.debug(f"get_symbol_name from DB failed: {e}")
    
    return None


def get_all_symbols(market: str = None) -> List[Dict]:
    """
    Get all active symbols, optionally filtered by market.
    
    Args:
        market: Optional market filter
        
    Returns:
        List of symbol records
    """
    try:
        with _get_db_connection() as db:
            cur = db.cursor()
            if market:
                cur.execute(
                    """
                    SELECT market, symbol, name, exchange, currency, is_hot, sort_order
                    FROM qd_market_symbols
                    WHERE market = ? AND is_active = 1
                    ORDER BY sort_order DESC
                    """,
                    (market.strip(),)
                )
            else:
                cur.execute(
                    """
                    SELECT market, symbol, name, exchange, currency, is_hot, sort_order
                    FROM qd_market_symbols
                    WHERE is_active = 1
                    ORDER BY market, sort_order DESC
                    """
                )
            rows = cur.fetchall() or []
            cur.close()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"get_all_symbols from DB failed: {e}")
        return []
