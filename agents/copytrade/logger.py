import json
import logging
import os
from datetime import datetime, timezone


def setup_audit_logger(log_file: str = "copytrade_audit.log") -> logging.Logger:
    logger = logging.getLogger("copytrade_audit")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler.setFormatter(fmt)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


def log_trade(
    logger: logging.Logger,
    action: str,
    target_address: str,
    market_question: str,
    token_id: str,
    side: str,
    amount: float,
    price: float,
    usdc_balance: float,
    result: str,
    error: str = "",
) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "target_address": target_address,
        "market_question": market_question,
        "token_id": token_id,
        "side": side,
        "amount": amount,
        "price": price,
        "usdc_balance": usdc_balance,
        "result": result,
        "error": error,
    }
    logger.info(json.dumps(entry))


def log_event(logger: logging.Logger, event_type: str, details: str) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "details": details,
    }
    logger.info(json.dumps(entry))
