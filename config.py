"""Minimal configuration used by browser_register.py.

Derived from the original CTF-reg/config.py with card, billing, Stripe,
captcha, and other payment-related fields removed. Only the proxy setting
required during registration remains.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    """Minimal ChatGPT registration configuration."""
    # Egress proxy URL, for example socks5://user:pass@host:port or
    # socks5://127.0.0.1:18899. Leave as None for a direct connection.
    proxy: Optional[str] = None
