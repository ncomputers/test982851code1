import time
import json
import redis
import logging
from order_manager import OrderManager
from trade_manager import TradeManager
import config

logger = logging.getLogger(__name__)

def fetch_signal_from_redis(redis_client, key='signal'):
    try:
        data = redis_client.get(key)
        if not data:
            return None
        return json.loads(data)
    except Exception as e:
        logger.error("Error fetching signal from Redis: %s", e)
        return None

def adjust_price(price, offset):
    try:
        return float(price) + offset
    except Exception:
        return price

def cancel_conflicting_pending_orders_api(order_manager, symbol, new_side):
    try:
        orders = order_manager.client.exchange.fetch_open_orders(symbol)
        if not orders:
            logger.info("No pending orders found via API for %s", symbol)
            return
        for order in orders:
            if order.get('status', '').lower() != 'open':
                continue
            order_side = order.get('side', '').lower()
            if new_side == "" or order_side != new_side.lower():
                try:
                    order_manager.client.cancel_order(order['id'], symbol)
                    logger.info("Canceled pending order: %s", order['id'])
                except Exception as e:
                    logger.error("Error canceling order %s: %s", order['id'], e)
    except Exception as e:
        logger.error("Error fetching pending orders via API: %s", e)

def cancel_same_side_pending_orders(order_manager, symbol, side):
    try:
        pending_orders = order_manager.client.exchange.fetch_open_orders(symbol)
        for order in pending_orders:
            if order.get('side', '').lower() == side.lower() and order.get('status', '').lower() == 'open':
                try:
                    order_manager.client.cancel_order(order['id'], symbol)
                    logger.info("Canceled same-side pending order: %s", order['id'])
                except Exception as e:
                    logger.error("Error canceling same-side order %s: %s", order['id'], e)
    except Exception as e:
        logger.error("Error fetching pending orders for same-side cancellation: %s", e)

def open_pending_order_exists(order_manager, symbol, side):
    try:
        orders = order_manager.client.exchange.fetch_open_orders(symbol)
        for order in orders:
            if order.get('side', '').lower() == side.lower() and order.get('status', '').lower() == 'open':
                return True
        return False
    except Exception as e:
        logger.error("Error checking for pending orders: %s", e)
        return False

def process_signal(signal_data, order_manager, trade_manager):
    if not signal_data:
        return None

    last_signal = signal_data.get("last_signal", {})
    supply_zone = signal_data.get("supply_zone", {})
    demand_zone = signal_data.get("demand_zone", {})

    fixed_offset = config.FIXED_OFFSET
    signal_text = last_signal.get("text", "").lower()
    raw_price = signal_data.get("last_signal", {}).get("price")
    raw_supply = supply_zone.get("min")
    raw_demand = demand_zone.get("min")

    import binance_ws  # Ensure this is imported at the top

    if not raw_price:
        live_price = binance_ws.current_price
        if live_price is None:
            logger.error("No price in signal and live price unavailable.")
            return None
        raw_price = live_price
        logger.info("Using live price as fallback: %.2f", raw_price)


    if "take profit" in signal_text or "tp" in signal_text:
        new_side = ""
    elif "short" in signal_text:
        new_side = "sell"
    elif "buy" in signal_text:
        new_side = "buy"
    else:
        logger.warning("Signal text does not indicate 'buy', 'short', or 'take profit': %s", signal_text)
        return None

    cancel_conflicting_pending_orders_api(order_manager, "BTCUSD", new_side)
    if new_side:
        cancel_same_side_pending_orders(order_manager, "BTCUSD", new_side)

    time.sleep(2)

    if new_side and open_pending_order_exists(order_manager, "BTCUSD", new_side):
        logger.info("A pending %s order still exists for BTCUSD. Skipping new order.", new_side)
        return None

    if "take profit" in signal_text or "tp" in signal_text:
        logger.info("Take profit signal detected. Attempting to close open positions for BTCUSD.")
        try:
            positions = order_manager.client.fetch_positions()
            for pos in positions:
                pos_symbol = pos.get('info', {}).get('product_symbol') or pos.get('symbol')
                if pos_symbol and "BTCUSD" in pos_symbol:
                    pos_size = pos.get('size') or pos.get('contracts') or "0"
                    try:
                        pos_amount = float(pos_size)
                    except Exception:
                        pos_amount = 0.0
                    if pos_amount < 0:
                        qty = abs(pos_amount)
                        logger.info("Closing short position of size %s", qty)
                        trade_manager.place_market_order("BTCUSD", "buy", qty, params={"time_in_force": "ioc"})
                    elif pos_amount > 0:
                        qty = pos_amount
                        logger.info("Closing long position of size %s", qty)
                        trade_manager.place_market_order("BTCUSD", "sell", qty, params={"time_in_force": "ioc"})
        except Exception as e:
            logger.error("Error closing positions on take profit signal: %s", e)
        return None

    if raw_supply is None or raw_demand is None:
        logger.error("Incomplete signal data (supply/demand missing): %s", signal_data)
        return None

    if new_side == "buy":
        entry_price = float(raw_price) - 50
        sl_price = float(raw_price) - 500
        tp_price = float(raw_price) + 3000
    elif new_side == "sell":
        entry_price = float(raw_price) + 50
        sl_price = float(raw_price) + 500
        tp_price = float(raw_price) - 3000

    else:
        logger.warning("Unable to determine side for signal: %s", signal_text)
        return None

    logger.info("Signal: %s | Entry: %.2f | SL: %.2f | TP: %.2f",
                last_signal.get("text"), entry_price, sl_price, tp_price)

    # --- NEW LOGIC: Auto-close opposite position ---
    try:
        positions = order_manager.client.fetch_positions()
        for pos in positions:
            pos_symbol = pos.get('info', {}).get('product_symbol') or pos.get('symbol')
            if pos_symbol and "BTCUSD" in pos_symbol:
                pos_size = pos.get('size') or pos.get('contracts') or 0
                try:
                    pos_amount = float(pos_size)
                except Exception:
                    pos_amount = 0.0
                if new_side == "buy" and pos_amount < 0:
                    logger.info("Opposite short position exists. Closing it before buying.")
                    trade_manager.place_market_order("BTCUSD", "buy", abs(pos_amount), params={"time_in_force": "ioc"})
                    time.sleep(2)
                elif new_side == "sell" and pos_amount > 0:
                    logger.info("Opposite long position exists. Closing it before selling.")
                    trade_manager.place_market_order("BTCUSD", "sell", pos_amount, params={"time_in_force": "ioc"})
                    time.sleep(2)
    except Exception as e:
        logger.error("Error checking/closing opposite position: %s", e)

    if order_manager.has_open_position("BTCUSD", new_side):
        logger.info("An open %s position already exists for BTCUSD. Skipping new order.", new_side)
        return None

    try:
        limit_order = order_manager.place_order("BTCUSD", new_side, 1, entry_price, params={"time_in_force": "gtc"})
        logger.info("Limit order placed: %s", limit_order)
    except Exception as e:
        logger.error("Failed to place limit order: %s", e)
        return None

    bracket_params = {
        "bracket_stop_loss_limit_price": str(sl_price),
        "bracket_stop_loss_price": str(sl_price),
        "bracket_take_profit_limit_price": str(tp_price),
        "bracket_take_profit_price": str(tp_price),
        "bracket_stop_trigger_method": "last_traded_price"
    }
    try:
        updated_order = order_manager.attach_bracket_to_order(
            order_id=limit_order['id'],
            product_id=27,
            product_symbol="BTCUSD",
            bracket_params=bracket_params
        )
        logger.info("Bracket attached, updated order: %s", updated_order)
        return updated_order
    except Exception as e:
        logger.error("Failed to attach bracket to order: %s", e)
        return None

def signals_are_different(new_signal, old_signal):
    if not old_signal:
        return True
    new_text = new_signal.get("last_signal", {}).get("text")
    old_text = old_signal.get("last_signal", {}).get("text")
    return new_text != old_text

def start_signal_processing_loop():
    from order_manager import OrderManager
    from trade_manager import TradeManager

    order_manager = OrderManager()
    trade_manager = TradeManager()
    redis_client = redis.Redis(host=config.REDIS_HOST, port=config.REDIS_PORT, db=config.REDIS_DB)

    last_signal = None
    logger.info("Starting signal processing loop...")
    while True:
        signal_data = fetch_signal_from_redis(redis_client, key="signal")
        if signal_data and signals_are_different(signal_data, last_signal):
            logger.info("New signal detected.")
            updated_order = process_signal(signal_data, order_manager, trade_manager)
            if updated_order:
                logger.info("Order processed successfully: %s", updated_order)
            else:
                logger.info("Order processing skipped or failed for this signal.")
            last_signal = signal_data
        else:
            logger.debug("No new signal or signal is identical to the last one.")
        time.sleep(5)

if __name__ == '__main__':
    start_signal_processing_loop()